"""Temporary Board and fixed in-process frame transport; no live MCP/model/native."""
from copy import deepcopy
import json
from unittest import TestCase
from unittest.mock import Mock, patch

import test_family_worker as fixture
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.context import ContextError,ContextScope,build_context
from blackoutburst_agents.development_authorization import DevelopmentAuthorization
from blackoutburst_agents.development_frame import FRAME,LEGACY,parse_frame
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical,digest
from blackoutburst_agents.family_client import FamilyBoardClient


class DevelopmentFrameClientTests(TestCase):
    make_manifest=fixture.FamilyWorkerTests.make_manifest
    ready=fixture.FamilyWorkerTests.ready
    transport=fixture.FamilyWorkerTests.transport

    def setUp(self):
        fixture.FamilyWorkerTests.setUp(self)
        from development_frames import get_development_frame
        for client in [worker.client for worker in self.workflows.values()]:
            client.require_development_frame=Mock()
            client.get_development_frame=Mock(side_effect=lambda _client=client,**args:
                get_development_frame(self.board,lambda session:self.board.actor_session(self.raw,session),
                    session_id=_client.session_id,**args))
        self.request['read_protocol']=FRAME
        self.ready()
        self.state=self.journal.read()
        self.client=self.workflows['child_0'].client
        self.child_scope=self.state['payload']['slot_scopes']['child_0']
        response=self.board.get_ticket(self.client.actor,self.child_scope['ticket_id'])
        self.board.claim_ticket(self.client.actor,self.child_scope['ticket_id'],response['ticket']['version'])
        manifest=json.loads((self.base/'child_0-broker/manifest.json').read_text())
        self.guard=self.worker._worker(self.state,'child_0').delegation_guard
        self.authority=DevelopmentAuthorization(self.client,scope=self.child_scope,manifest=manifest,
            registration=self.registrations['child_0'],clock=lambda:self.now,delegation_guard=self.guard)
        for worker in self.workflows.values():worker.client.calls.clear()

    def snapshot(self):return self.authority.snapshot(require_claim=True)

    def no_effects(self):
        self.assertFalse(self.inferences)
        self.assertFalse(any(worker.control.calls for worker in self.workflows.values()))
        self.assertFalse(any(backend.calls for backend in self.backend.values()))

    def test_one_fresh_call_per_boundary_and_exact_existing_context(self):
        first=self.snapshot()
        self.authority.seal(first)
        result=self.authority()
        self.assertEqual(result['claim_id'],self.authority.claim_id)
        self.assertEqual(self.client.get_development_frame.call_count,2)
        self.assertFalse(any(worker.client.calls for worker in self.workflows.values()))
        expected=build_context(self.client.call,scope=ContextScope(**{key:self.child_scope[key]
            for key in ('owner_id','agent_id','project_id','ticket_id')}),registration=self.registrations['child_0'])
        self.assertEqual(first['context'],expected)
        self.assertEqual(self.state['configuration']['read_protocol'],FRAME)
        self.assertEqual(self.state['configuration_sha256'],digest(canonical(self.state['configuration'])))
        self.no_effects()

    def intercept(self,mutate):
        original=self.client.get_development_frame.side_effect
        def collect(**args):
            value=original(**args);mutate(value);return value
        return patch.object(self.client,'get_development_frame',side_effect=collect)

    def test_every_selection_identity_and_protocol_field_is_exact(self):
        keys=['owner_id','agent_id','device_id','profile_version','project_id','ticket_id',
              'session_id','family_id','parent_ticket_id','parent_session_id','terminal']
        for key in keys:
            with self.subTest(key=key),self.intercept(lambda f,k=key:f['selection'].update({k:True})),\
                    self.assertRaises(BoardReadError):self.snapshot()
        for mutate in [lambda f:f.update(extra=1),lambda f:f.update(schema='foreign'),
                       lambda f:f['selected'].update(extra=1),lambda f:f['selected'].update(pages=[]),
                       lambda f:f['parent'].update(pages=[{}]*4),lambda f:f['family'].update(extra={})]:
            with self.intercept(mutate),self.assertRaises(BoardReadError):self.snapshot()
        self.no_effects()

    def test_each_ticket_claim_version_context_and_family_observation_is_rechecked(self):
        for role in ('selected','parent'):
            for phase in ('first','last'):
                for mutate in [lambda r:r['ticket'].update(version=r['ticket']['version']+1),
                               lambda r:r['ticket'].update(claimant=None),
                               lambda r:r['ticket'].update(lease_expires_at=self.now),
                               lambda r:r['project'].update(version=r['project']['version']+1)]:
                    with self.subTest(role=role,phase=phase),self.intercept(
                            lambda f,r=role,p=phase,m=mutate:m(f[r][p])),\
                            self.assertRaises((DevelopmentError,ContextError)):self.snapshot()
        for phase in ('first','last'):
            for key,value in [('should_stop',True),('plan_sha256','0'*64),('revision',True)]:
                with self.intercept(lambda f,p=phase,k=key,v=value:f['family'][p].update({k:v})),\
                        self.assertRaises(DevelopmentError):self.snapshot()
        self.no_effects()

    def test_each_initial_and_terminal_policy_is_validated(self):
        for role in ('selected','parent'):
            for phase in ('policy_first','policy_last'):
                for key,value in [('enabled',False),('allow_read',False),('allow_requests',False),
                                  ('actions',[]),('resources',[])]:
                    with self.subTest(role=role,phase=phase,key=key),self.intercept(
                            lambda f,r=role,p=phase,k=key,v=value:f[r][p]['policy'].update({k:v})),\
                            self.assertRaises(DevelopmentError):self.snapshot()
        self.no_effects()

    def test_real_revocation_after_last_family_view_cannot_pass_late_policy(self):
        original=self.board.get_delegation_family;count=0
        def family(*args,**kw):
            nonlocal count
            value=original(*args,**kw);count+=1
            if count==2:self.board.set_policy(self.human,True,True,True,True,['repo.read_file'],['repo:fixture'])
            return value
        with patch.object(self.board,'get_delegation_family',side_effect=family),\
                self.assertRaises(DevelopmentError):self.snapshot()
        self.no_effects()

    def test_terminal_review_ready_still_requires_current_policies(self):
        # Synthetic terminal fixture only; no model or result is accepted.
        self.guard.terminal=True
        self.state['payload']['finalize_intent']={'snapshot_sha256':'a'*64}
        with self.board._tx() as db:
            db.execute("UPDATE delegation_families SET state='review_ready',final_snapshot_sha256=? WHERE id=?",
                       ('a'*64,self.state['payload']['family_id']))
        self.snapshot()
        self.assertTrue(self.client.get_development_frame.call_args.kwargs['terminal'])
        self.board.set_policy(self.human,True,True,True,True,['repo.read_file'],['repo:fixture'])
        with self.assertRaises(DevelopmentError):self.snapshot()
        self.no_effects()

    def test_new_human_context_after_seal_is_not_hidden_by_a_frame_cache(self):
        self.authority.seal(self.snapshot())
        thread=self.board.get_ticket(self.human,self.child_scope['ticket_id'])['ticket']['thread_id']
        self.board.post_message(self.human,thread,'New source for the selected ticket',idempotency_key='new-context')
        with self.assertRaises(DevelopmentError):self.snapshot()
        self.assertEqual(self.client.get_development_frame.call_count,2)
        self.no_effects()

    def test_local_stop_and_deadline_during_rpc_are_checked_before_composition(self):
        for change in ('stop','deadline'):
            with self.subTest(change=change):
                if change=='stop':mutate=lambda _:self.journal.request_stop()
                else:mutate=lambda _:setattr(self,'now',self.request['limits']['deadline'])
                try:
                    with self.intercept(mutate),self.assertRaises(DevelopmentError):self.snapshot()
                finally:
                    if change=='stop':(self.journal.directory/'stop.json').unlink()
        self.no_effects()

    def test_frame_failure_never_uses_legacy_or_refreshes_registration(self):
        before={slot:worker.client.register_count for slot,worker in self.workflows.items()}
        for error in [BoardReadError('rpc_rejected'),BoardReadError('timeout'),BoardReadError('response_too_large')]:
            with patch.object(self.client,'get_development_frame',side_effect=error),\
                    self.assertRaises(BoardReadError):self.snapshot()
        self.assertFalse(any(worker.client.calls for worker in self.workflows.values()))
        self.assertEqual(before,{slot:worker.client.register_count for slot,worker in self.workflows.items()})
        self.no_effects()

    def test_reopened_configuration_cannot_switch_protocol(self):
        request=deepcopy(self.request);request['read_protocol']=LEGACY
        with self.assertRaises(DevelopmentError):self.worker.prepare(request)
        self.assertEqual(self.journal.read()['configuration']['read_protocol'],FRAME)
        state=deepcopy(self.state);state['configuration']['read_protocol']=LEGACY
        with self.assertRaises(DevelopmentError):self.worker._local_live(state)

    def test_foreign_page_or_extra_consumed_page_is_not_accepted(self):
        for mutate in [lambda f:f['selected']['pages'][0]['arguments'].update(thread_id='foreign'),
                       lambda f:f['selected']['pages'][0]['response']['thread'].update(id='foreign'),
                       lambda f:f['selected']['pages'].append(deepcopy(f['selected']['pages'][0]))]:
            with self.intercept(mutate),self.assertRaises(ContextError):self.snapshot()
        self.no_effects()

    def test_complete_family_uses_frame_mode_with_only_simulated_model_and_backend(self):
        result=self.worker.run()
        self.assertEqual(result['status'],'review_ready',result)
        self.assertEqual(result['children_accepted'],2)
        self.assertTrue(all(worker.client.get_development_frame.called for worker in self.workflows.values()))
        self.assertEqual([slot for slot,_ in self.inferences],['child_0']*3+['child_1']*3+['parent']*4)
        before=len(self.inferences)
        self.assertEqual(self.worker.run(),result)
        self.assertEqual(len(self.inferences),before)


class DevelopmentFrameTransportTests(TestCase):
    def setUp(self):
        self.client=FamilyBoardClient({'mcp_url':'https://board.test/mcp','token':'synthetic',
            'client':'codex','identity_key':'fixture','name':'Fixture'},'root',3)
        self.addCleanup(self.client.close)
        self.client.adopt_registration()

    def test_dedicated_fixed_call_injects_only_own_session(self):
        with patch.object(self.client,'_initialize'),patch.object(self.client,'_tool',return_value={}) as tool:
            self.client.get_development_frame(ticket_id='ticket',family_id='family',terminal=False)
            self.assertEqual(tool.call_args.args,('get_development_frame',
                dict(session_id='root',ticket_id='ticket',family_id='family',terminal=False)))
            for changes in [dict(ticket_id=True),dict(family_id='foreign/path'),dict(terminal=1),dict(terminal='false')]:
                with self.subTest(changes=changes),self.assertRaises(BoardReadError):
                    self.client.get_development_frame(**{**dict(ticket_id='ticket',family_id='family',terminal=False),**changes})
            self.assertEqual(tool.call_count,1)

    def test_preparation_requires_advertised_exact_tool_no_fallback(self):
        schema={'properties':{'session_id':{'type':'string'},'ticket_id':{'type':'string'},
            'family_id':{'type':'string'},'terminal':{'type':'boolean'}},
            'required':['session_id','ticket_id','family_id','terminal'],'additionalProperties':False}
        valid={'tools':[{'name':'get_development_frame','inputSchema':schema}]}
        with patch.object(self.client,'_initialize'),patch.object(self.client,'_rpc',return_value=valid) as rpc:
            self.client.require_development_frame()
            self.assertEqual(rpc.call_args.args,('tools/list',{}))
        for value in [{'tools':[]},{'tools':valid['tools']*2},{'tools':True}]:
            with patch.object(self.client,'_initialize'),patch.object(self.client,'_rpc',return_value=value),\
                    self.assertRaises(BoardReadError):self.client.require_development_frame()


class DevelopmentFrameModeTests(TestCase):
    def fixture(self):
        value=fixture.FamilyWorkerTests('test_prepare_reserves_three_roots_but_opens_no_remote_family_or_runtime')
        value.setUp();self.addCleanup(value.doCleanups)
        return value

    def test_legacy_default_and_unknown_modes_are_explicit_before_sealing(self):
        value=self.fixture()
        for protocol in (None,True,'auto','unrecognized'):
            request={**value.request,'read_protocol':protocol}
            with self.assertRaises(DevelopmentError):value.worker.prepare(request)
            self.assertFalse((value.journal.directory/'family.json').exists())
        value.worker.prepare(value.request)
        self.assertEqual(value.journal.read()['configuration']['read_protocol'],LEGACY)

    def test_missing_server_capability_blocks_frame_preparation_without_fallback(self):
        value=self.fixture();value.request['read_protocol']=FRAME
        value.workflows['parent'].client.require_development_frame=Mock(side_effect=BoardReadError('development_frame_unavailable'))
        with self.assertRaises(BoardReadError):value.worker.prepare(value.request)
        self.assertFalse((value.journal.directory/'family.json').exists())
        self.assertFalse(value.inferences)

    def test_invalid_mode_cli_preflight_precedes_files_and_connections(self):
        from blackoutburst_agents.family_cli import _preflight,_resume_preflight
        from types import SimpleNamespace
        with patch('blackoutburst_agents.family_audit.manifest_document',side_effect=AssertionError('file access')):
            with self.assertRaises(DevelopmentError):
                _preflight(SimpleNamespace(),{'request':{'children':[{},{}],'read_protocol':'auto'}})
        with self.assertRaises(DevelopmentError):
            _resume_preflight(SimpleNamespace(),{'configuration':{'roots':{},'read_protocol':FRAME,
                'request':{'read_protocol':LEGACY}}})
