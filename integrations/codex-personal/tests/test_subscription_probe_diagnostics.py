"""Pure synthetic JSONL/parser and mocked transport; no CLI, model or auth."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from blackoutburst_agents.subscription_probe import (
    DEFAULT_DISABLED_FEATURES, ProbeSettings, _ProcessResult, _parse_probe,
    run_subscription_probe,
)

CATEGORIES = ('agent_message','reasoning','command_execution','file_change',
              'mcp_tool_call','web_search','todo_list','unknown')
NONCE = 'PRIVATE_NONCE_SENTINEL_0da274'
SECRET = 'PRIVATE_BODY_COMMAND_AUTH_SENTINEL_6bfc56'


def stream(*events):
    return b'\n'.join(json.dumps(event,ensure_ascii=True).encode('ascii')
                      if not isinstance(event,bytes) else event for event in events)+b'\n'


def success():
    return [{'type':'item.completed','item':{'type':'agent_message','text':NONCE}},
            {'type':'turn.completed'}]


class ProbeDiagnosticTests(unittest.TestCase):
    def parse(self,*events,returncode=0,failure=None,stderr=b''):
        return _parse_probe(_ProcessResult(returncode,stream(*events),stderr,failure),NONCE)

    def diagnostic(self,result):
        self.assertFalse(result['ok'])
        value=result['diagnostic']
        self.assertEqual(set(value),{'schema_version','item_events','saturated'})
        self.assertIs(type(value['schema_version']),int)
        self.assertEqual(value['schema_version'],1)
        self.assertEqual(tuple(value['item_events']),CATEGORIES)
        self.assertIs(type(value['saturated']),bool)
        self.assertTrue(all(type(count) is int and 0 <= count <= 1024 for count in value['item_events'].values()))
        self.assertLess(len(json.dumps(value,ensure_ascii=True).encode('ascii')),384)
        encoded=json.dumps(result,ensure_ascii=True)
        self.assertNotIn(NONCE,encoded)
        self.assertNotIn(SECRET,encoded)
        return value

    def test_each_fixed_category_and_transition_counts_events_only(self):
        for category in CATEGORIES:
            for phase in ('item.started','item.updated','item.completed'):
                with self.subTest(category=category,phase=phase):
                    event={'type':phase,'item':{'type':category,'id':SECRET,'text':SECRET,
                        'body':SECRET,'command':SECRET,'arguments':SECRET}}
                    result=self.parse(event,*success(),b'not-json')
                    self.assertEqual(result['reason'],'protocol_error' if category in CATEGORIES[:2]
                                     else 'unexpected_tool_event')
                    diagnostic=self.diagnostic(result)
                    self.assertEqual(diagnostic['item_events'][category],2 if category=='agent_message' else 1)
                    self.assertFalse(diagnostic['saturated'])

    def test_unknown_names_never_leave_parser_even_as_hashes_or_previews(self):
        for unknown in [SECRET,SECRET+'\U0001f680',SECRET*10000,'', '\ud800']:
            with self.subTest(length=len(unknown)):
                result=self.parse({'type':'item.started','item':{'type':unknown,'id':SECRET,
                    'body':SECRET,'command':SECRET,'url':'https://'+SECRET}},*success())
                diagnostic=self.diagnostic(result)
                self.assertEqual(result['reason'],'unexpected_tool_event')
                self.assertEqual(diagnostic['item_events']['unknown'],1)
                if unknown:self.assertNotIn(unknown,json.dumps(diagnostic))

    def test_malformed_items_do_not_get_a_default_category(self):
        for item in [None,True,3,[],{}, {'type':None},{'type':False},{'type':3},{'type':[]},{'type':{}}]:
            with self.subTest(item=item):
                result=self.parse({'type':'item.started','item':item})
                diagnostic=self.diagnostic(result)
                self.assertEqual(result['reason'],'protocol_error')
                self.assertEqual(sum(diagnostic['item_events'].values()),0)
        malformed=[b'{"type":"item.started","item":{"type":"command_execution","type":"reasoning"}}',
                   b'{"type":"item.started","item":{"type":"reasoning","x":NaN}}',
                   b'{"type":"item.started","item":',b'[]',b'{}',b'garbage '+SECRET.encode()]
        for event in malformed:
            result=self.parse(event)
            self.assertEqual(result['reason'],'protocol_error')
            self.assertEqual(sum(self.diagnostic(result)['item_events'].values()),0)

    def test_repeated_lifecycle_is_three_events_not_one_execution(self):
        events=[{'type':phase,'item':{'type':'command_execution','id':SECRET,'command':SECRET}}
                for phase in ('item.started','item.updated','item.completed')]
        diagnostic=self.diagnostic(self.parse(*events,*success()))
        self.assertEqual(diagnostic['item_events']['command_execution'],3)
        self.assertEqual(diagnostic['item_events']['agent_message'],1)
        self.assertNotIn('executions',diagnostic)

    def test_counts_saturate_without_altering_refusal(self):
        event={'type':'item.started','item':{'type':'todo_list','id':SECRET}}
        at_limit=self.diagnostic(self.parse(*([event]*1024)))
        self.assertEqual(at_limit['item_events']['todo_list'],1024)
        self.assertFalse(at_limit['saturated'])
        clipped=self.parse(*([event]*1025),*success())
        self.assertEqual(clipped['reason'],'unexpected_tool_event')
        diagnostic=self.diagnostic(clipped)
        self.assertEqual(diagnostic['item_events']['todo_list'],1024)
        self.assertTrue(diagnostic['saturated'])

    def test_transport_failures_and_bad_utf8_are_not_reparsed(self):
        body=stream({'type':'item.started','item':{'type':'command_execution','command':SECRET}},*success())
        for failure in ('timeout','cleanup_failed','output_limit','process_io_failed'):
            result=_parse_probe(_ProcessResult(0,body,b'',failure),NONCE)
            self.assertEqual(result['reason'],failure)
            self.assertEqual(sum(self.diagnostic(result)['item_events'].values()),0)
            self.assertEqual(result['verification'],{'exit_zero':True,'turn_completed':False,'nonce_verified':False})
        result=_parse_probe(_ProcessResult(0,body+b'\xff',b''),NONCE)
        self.assertEqual(result['reason'],'protocol_error')
        self.assertEqual(sum(self.diagnostic(result)['item_events'].values()),0)

    def test_refusal_reasons_and_verification_priority_are_unchanged(self):
        cases=[
            ([{'type':'turn.failed','error':{'message':'usage limit '+SECRET}}],0,b'','quota_or_rate_limit'),
            ([{'type':'error','message':'authentication failed '+SECRET}],0,b'','authentication_failed'),
            ([{'type':'turn.failed','error':{'message':SECRET}}],0,b'','execution_failed'),
            (success(),1,b'quota '+SECRET.encode(),'quota_or_rate_limit'),
            ([],0,b'','turn_not_completed'),
            ([{'type':'turn.completed'}],0,b'','nonce_mismatch'),
            ([*success(),*success()],0,b'','protocol_error'),
            ([*success(),{'type':'item.started','item':{'type':'todo_list'}}],0,b'','unexpected_tool_event'),
        ]
        for events,code,stderr,reason in cases:
            result=self.parse(*events,returncode=code,stderr=stderr)
            self.assertEqual(result['reason'],reason)
            self.diagnostic(result)

    def test_success_shape_and_verification_are_exactly_unchanged(self):
        result=self.parse({'type':'item.completed','item':{'type':'reasoning','text':SECRET}},*success())
        self.assertEqual(result,{'ok':True,'reason':'verified','verification':{
            'exit_zero':True,'turn_completed':True,'nonce_verified':True}})
        reasoning={'type':'item.updated','item':{'type':'reasoning','text':SECRET}}
        self.assertEqual(self.parse(*([reasoning]*1025),*success()),result)

    def test_mocked_public_receipt_does_not_retry_or_change_launch_controls(self):
        settings=ProbeSettings('/fixture/codex',model='fixture-model')
        body=stream({'type':'item.started','item':{'type':'todo_list','text':SECRET}},*success())
        with patch('blackoutburst_agents.subscription_probe._inspect_in_directory',return_value={'ok':True}), \
             patch('blackoutburst_agents.subscription_probe.validate_executable',return_value=Path('/fixture/codex')), \
             patch('blackoutburst_agents.subscription_probe._run_bounded',return_value=_ProcessResult(0,body,b'')) as run, \
             patch('blackoutburst_agents.subscription_probe.secrets.token_hex',return_value='0'*32), \
             patch('subprocess.Popen',side_effect=AssertionError('no process')):
            result=run_subscription_probe(settings)
        self.assertEqual(run.call_count,1)
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['reason'],'unexpected_tool_event')
        self.diagnostic(result)
        arguments=run.call_args.args[0]
        self.assertEqual(arguments[:3],['/fixture/codex','exec','--ignore-user-config'])
        for feature in DEFAULT_DISABLED_FEATURES:self.assertIn('features.'+feature+'=false',arguments)
        self.assertIn('read-only',arguments)
        self.assertIn('model_provider="openai"',arguments)
