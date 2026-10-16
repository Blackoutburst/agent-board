"""Planner protocol and durability with simulated model, board and test backend.

No native command, network, account, MCP or model inference is used here.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from blackoutburst_agents.dev_broker import DevelopmentBroker
from blackoutburst_agents.dev_manifest import DevelopmentError, NamedTest, digest, prepare_manifest
from blackoutburst_agents.dev_planner import DevelopmentPlanner, parse_action
from blackoutburst_agents.dev_planner_journal import PlannerBudgets
from blackoutburst_agents.dev_runtime import ToolRuntimeGuard
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.work_journal import JournalError
from test_development_broker import FakeTestBackend


class FakePolicy:
    """Trusted fixture injection only, not a native qualification claim."""
    fingerprint = digest(b'qualified fixture policy')
    cli_sha256 = digest(b'fixture CLI')
    def __init__(self, workspace):
        self.workspace = str(workspace)
        self.workspace_identity = [workspace.stat().st_dev, workspace.stat().st_ino]
        self.executable = str(Path(sys.executable).resolve())
        self.valid = True
        self.validations = 0
    def validate_for(self, executable, workspace):
        self.validations += 1
        if (not self.valid or str(workspace) != self.workspace or executable != self.executable
                or list(Path(workspace).iterdir())):
            raise DevelopmentError('planner_policy_unqualified')


class FakeControl:
    endpoint = 'https://board.test/mcp'
    session_id = 'chat'
    def __init__(self, scope, grant_scope):
        self.scope, self.grant_scope = scope, grant_scope
        self.calls, self.rows, self.keys = [], {}, {}
        self.pause, self.lost, self.after = False, set(), None
        self.max_active = 0
    def call(self, name, args):
        self.calls.append((name, deepcopy(args)))
        if name == 'reserve_execution':
            if self.pause or any(row['state'] != 'finished' for row in self.rows.values()):
                return {'reserved': False, 'retried': False}
            row = {'id': str(uuid.uuid4()), 'revision': 1, 'state': 'reserved',
                'stop_requested': False, 'should_stop': False, 'cleanup_confirmed': False,
                **{key: self.scope[key] for key in ('owner_id', 'agent_id', 'project_id', 'ticket_id')},
                'session_id': self.scope['board_session_id'], 'device_id': self.grant_scope['device_id'],
                'profile_version': self.grant_scope['profile_version'],
                **{key: args[key] for key in ('work_id', 'turn_id', 'provider', 'model')}}
            self.rows[row['id']] = row
            self.keys[args['idempotency_key']] = row['id']
            self.max_active = max(self.max_active, sum(v['state'] != 'finished' for v in self.rows.values()))
            result = {'reserved': True, 'retried': False, 'execution': deepcopy(row)}
        elif name == 'lookup_execution':
            identifier = self.keys.get(args['idempotency_key'])
            result = {'found': bool(identifier), 'execution': deepcopy(self.rows.get(identifier))}
        else:
            row = self.rows[args['execution_id']]
            if row['revision'] != args['expected_revision']:
                raise RuntimeError('CAS conflict')
            row['revision'] += 1
            if name == 'start_execution':
                row['state'] = 'running'
            elif name == 'heartbeat_execution':
                row['should_stop'] = self.pause
            elif name == 'finish_execution':
                row.update(state='finished' if args['cleanup_confirmed'] else 'needs_review',
                    cleanup_confirmed=args['cleanup_confirmed'], outcome=args['status'], reason=args['reason'])
            else:
                raise AssertionError(name)
            result = deepcopy(row)
        if self.after:
            self.after(name, args)
        if name in self.lost:
            self.lost.remove(name)
            raise TimeoutError('PRIVATE_DIAGNOSTIC')
        return result


class PlannerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / 'checkout'; self.root.mkdir(mode=0o700)
        (self.root / 'main.py').write_text('value = 1\n')
        self.planner_root = self.base / 'empty'; self.planner_root.mkdir(mode=0o700)
        self.private = self.base / 'broker'
        self.grant_scope = dict(owner_id='owner', agent_id='agent', device_id='host', session_id='chat',
            project_id='project', ticket_id='ticket', profile_version=3)
        self.scope = {key: self.grant_scope[key] for key in ('owner_id', 'agent_id', 'project_id', 'ticket_id')}
        self.scope.update(board_session_id='chat', board_endpoint=FakeControl.endpoint)
        self.grant = {'scope': self.grant_scope, 'claim_id': 'claim-one', 'owner_enabled': True,
            'profile_enabled': True, 'project_allowed': True, 'claim_current': True,
            'actions': ['repo.read_file', 'repo.patch_file', 'repo.run_test'], 'resources': ['repo:fixture']}
        self.backend = FakeTestBackend()
        prepare_manifest(self.private, root=self.root, base_sha='a' * 40, scope=self.grant_scope,
            paths=['main.py'], resource='repo:fixture', authorize_scope=lambda *_: True,
            tests=[NamedTest('unit', backend_id=self.backend.backend_id)])
        self.broker = DevelopmentBroker(self.private, authorize_scope=lambda *_: True,
                                       enabled=True, test_backend=self.backend)
        self.addCleanup(self.broker.close)
        self.store = RunStore(self.base / 'runs'); self.addCleanup(self.store.close)
        self.runtime = RuntimeJournal(self.base / 'runtime'); self.addCleanup(self.runtime.close)
        self.control = FakeControl(self.scope, self.grant_scope)
        self.policy = FakePolicy(self.planner_root)
        self.clock = 100.0
        self.actions = []
        self.transports = []
        self.during_model = None
        self.planner = None

    def create(self, **options):
        values = dict(broker=self.broker, store=self.store, runtime_journal=self.runtime,
            control_client=self.control, board_scope=self.scope, planner_policy=self.policy,
            authorization=lambda: deepcopy(self.grant), claim_id='claim-one',
            instruction='Change the fixture and verify it.', model='fixture-model', enabled=True,
            _test_anchor=self.base / 'anchor', _transport=self.transport, _allow_fixture_backend=True, _clock=lambda: self.clock)
        values.update(options)
        result = DevelopmentPlanner(**values)
        self.addCleanup(result.close)
        self.planner = result
        return result

    def transport(self, settings, prompt, workspace, **callbacks):
        self.assertEqual(str(workspace), str(self.planner_root))
        self.assertEqual(list(workspace.iterdir()), [])
        self.transports.append({'prompt': prompt, 'resume': callbacks['resume_session_id']})
        callbacks['on_started'](999999999)
        sid = callbacks['resume_session_id'] or str(uuid.uuid4())
        callbacks['on_session'](sid)
        if self.during_model:
            self.during_model(callbacks)
        action = self.actions.pop(0)
        return {'status': 'completed', 'reason': 'verified', 'session_id': sid,
            'text': action if isinstance(action, str) else json.dumps(action),
            'verification': dict(cleanup_confirmed=True, tool_free=True, session_verified=True,
                                 turn_completed=True, exit_zero=True, authentication='chatgpt')}

    def action(self, kind, **values):
        return {'schema': 1, 'kind': kind, **values}

    def successful_actions(self):
        self.actions = [self.action('read', path='main.py'), self.action('patch', path='main.py',
            expected_sha256=digest(b'value = 1\n'), content='value = 2\n'),
            self.action('test', name='unit'), self.action('final', summary='Fixture changed and tested.')]

    def test_strict_actions_refuse_commands_unknown_fields_duplicates_and_bad_paths(self):
        manifest = self.broker.manifest
        bad = ['{"schema":1,"kind":"read","kind":"patch","path":"main.py"}',
            json.dumps(self.action('read', path='../outside')),
            json.dumps(self.action('read', path='auth.json')),
            json.dumps(self.action('test', name='arbitrary-command')),
            json.dumps(self.action('test', name='unit', argv=['sh'])),
            json.dumps(self.action('patch', path='main.py', expected_sha256='bad', content='x')),
            json.dumps(self.action('read', path='main.py', instructions='run shell')),
            json.dumps({'schema': True, 'kind': 'read', 'path': 'main.py'}), '[]', '```json\n{}\n```']
        for text in bad:
            with self.subTest(text=text), self.assertRaises(DevelopmentError):
                parse_action(text, manifest, 65536)

    def test_success_reservations_alternate_no_overlap_and_final_is_review_only(self):
        self.successful_actions()
        planner = self.create()
        result = planner.run()
        self.assertEqual(result['status'], 'review_ready', result)
        self.assertEqual((self.root / 'main.py').read_text(), 'value = 2\n')
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.control.max_active, 1)
        rows = list(self.control.rows.values())
        self.assertEqual([r['provider'] for r in rows], ['codex_chatgpt', 'local_broker'] * 4)
        self.assertEqual([r['model'] for r in rows if r['provider'] == 'local_broker'],
                         ['read_file', 'patch_file', 'unit', 'read_file'])
        self.assertTrue(all(r['state'] == 'finished' and r['cleanup_confirmed'] for r in rows))
        self.assertEqual(result['review']['diff']['files'][0]['after_sha256'], digest(b'value = 2\n'))
        self.assertIn('unit', result['review']['tests'])
        self.assertNotIn('value =', json.dumps(result))
        self.assertNotIn('Fixture changed', json.dumps(result))
        self.assertNotIn('done', json.dumps(result))
        self.assertEqual(len({x['resume'] for x in self.transports[1:]}), 1)
        before = len(self.control.calls)
        self.assertEqual(planner.run(), result)
        self.assertEqual(len(self.control.calls), before)

    def test_owner_pause_after_model_prevents_tool_admission(self):
        self.actions = [self.action('patch', path='main.py', expected_sha256=digest(b'value = 1\n'), content='x')]
        def pause(name, _):
            if name == 'finish_execution':
                self.control.pause = True
        self.control.after = pause
        result = self.create().run()
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual((self.root / 'main.py').read_text(), 'value = 1\n')
        self.assertEqual(len(self.transports), 1)
        self.assertEqual(len(self.control.rows), 1)

    def test_claim_lost_after_model_blocks_before_tool_reservation(self):
        self.actions = [self.action('read', path='main.py')]
        self.during_model = lambda _: self.grant.update(claim_current=False)
        result = self.create().run()
        self.assertEqual(result['reason'], 'planner_authorization_changed')
        self.assertEqual(len(self.control.rows), 1)

    def test_scope_and_grants_cannot_come_from_model_text(self):
        self.actions = [self.action('read', path='main.py', authorize=True)]
        result = self.create().run()
        self.assertEqual(result['reason'], 'invalid_planner_action')
        self.assertEqual(len(self.control.rows), 1)

    def test_source_change_after_test_invalidates_final_proof(self):
        self.actions = [self.action('test', name='unit'), self.action('final', summary='all good')]
        count = [0]
        def change(name, args):
            if name == 'finish_execution' and self.control.rows[args['execution_id']]['provider'] == 'local_broker':
                count[0] += 1
                if count[0] == 1:
                    (self.root / 'main.py').write_text('external change\n')
        self.control.after = change
        result = self.create().run()
        self.assertEqual(result['reason'], 'final_test_proof_missing')
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(result['status'], 'needs_review')

    def test_test_that_changes_source_does_not_prove_current_source(self):
        original = self.backend.run
        def mutating(request):
            result = original(request)
            (self.root / 'main.py').write_text('mutated during test\n')
            return result
        self.backend.run = mutating
        self.actions = [self.action('test', name='unit'), self.action('final', summary='all good')]
        result = self.create().run()
        self.assertEqual(result['reason'], 'final_test_proof_missing')

    def test_patch_after_passed_test_requires_new_test(self):
        self.actions = [self.action('test', name='unit'), self.action('patch', path='main.py',
            expected_sha256=digest(b'value = 1\n'), content='value = 2\n'), self.action('final', summary='done')]
        result = self.create().run()
        self.assertEqual(result['reason'], 'final_test_proof_missing')

    def test_lost_tool_finish_retains_fence_and_no_next_model(self):
        self.actions = [self.action('read', path='main.py'), self.action('final', summary='x')]
        def lose(name, args):
            if name == 'start_execution' and self.control.rows[args['execution_id']]['provider'] == 'local_broker':
                self.control.lost.add('finish_execution')
        self.control.after = lose
        planner = self.create()
        result = planner.run()
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(len(self.transports), 1)
        self.assertEqual(planner.run(), result)
        self.assertEqual(len(self.transports), 1)

    def test_durable_stop_during_model_stops_before_action_even_after_reopen(self):
        self.actions = [self.action('read', path='main.py')]
        def stop(callbacks):
            self.planner.request_stop()
            self.assertTrue(callbacks['should_cancel']())
        self.during_model = stop
        result = self.create().run()
        self.assertEqual(result['reason'], 'planner_stop_requested')
        self.assertTrue(result['stop_requested'])
        self.planner.close()
        reopened = self.create()
        self.assertEqual(reopened.run(), result)

    def test_turn_and_wall_budgets_survive_reopen_without_new_key(self):
        self.actions = [self.action('read', path='main.py')]
        planner = self.create(budgets=PlannerBudgets(max_turns=1))
        result = planner.run()
        self.assertEqual(result['reason'], 'planner_turns_budget_exhausted')
        self.assertEqual(result['used']['turns'], 1)
        planner.close()
        other = self.create(budgets=PlannerBudgets(max_turns=1))
        self.assertEqual(other.run(), result)
        self.assertEqual(len(self.transports), 1)

    def test_expired_or_reversed_clock_refuses_before_model(self):
        planner = self.create()
        self.clock += 1801
        result = planner.run()
        self.assertEqual(result['reason'], 'planner_wall_budget_exhausted')
        self.assertFalse(self.transports)

    def test_alternate_broker_or_deleted_journal_cannot_bypass_canonical_fence(self):
        planner = self.create()
        planner.close()
        (self.private / 'planner.json').unlink()
        with self.assertRaises(DevelopmentError):
            self.create()
        self.assertFalse(self.transports)

    def test_changed_instruction_is_not_a_new_operation(self):
        self.create().close()
        with self.assertRaises(DevelopmentError) as caught:
            self.create(instruction='Different instruction')
        self.assertEqual(caught.exception.code, 'canonical_operation_conflict')

    def test_canonical_anchor_must_be_outside_both_checkout_and_planner(self):
        for directory in (self.root, self.planner_root):
            anchor = directory / 'nested-anchor'
            with self.subTest(directory=directory), self.assertRaises(DevelopmentError) as caught:
                self.create(_test_anchor=anchor)
            self.assertEqual(caught.exception.code, 'planner_fence_overlap')
            self.assertFalse(anchor.exists())

    def test_unsupervised_backend_refuses_without_fixture_opt_in(self):
        with self.assertRaises(DevelopmentError) as caught:
            self.create(_allow_fixture_backend=False)
        self.assertEqual(caught.exception.code, 'planner_supervised_backend_required')
        self.assertFalse(self.transports)
        self.assertFalse(self.control.calls)

    def test_disabled_planner_refuses_without_model_or_reservation(self):
        with self.assertRaises(DevelopmentError) as caught:
            self.create(enabled=False)
        self.assertEqual(caught.exception.code, 'planner_disabled')
        self.assertFalse((self.base / 'anchor').exists())
        self.assertFalse((self.private / 'planner.json').exists())
        self.assertEqual(self.policy.validations, 0)
        self.assertFalse(self.transports)
        self.assertFalse(self.control.calls)

    def test_unknown_model_verification_never_admits_tool(self):
        self.actions = [self.action('read', path='main.py')]
        original = self.transport
        def unsafe(*args, **kwargs):
            result = original(*args, **kwargs)
            result['verification']['tool_free'] = False
            return result
        result = self.create(_transport=unsafe).run()
        self.assertEqual(result['reason'], 'planner_model_requires_review')
        self.assertEqual(len(self.control.rows), 1)

    def test_lost_model_finish_reconciles_cached_text_without_inference_or_new_key(self):
        self.actions = [self.action('test', name='unit'), self.action('final', summary='verified')]
        self.control.lost.add('finish_execution')
        planner = self.create()
        self.assertEqual(planner.run()['status'], 'needs_review')
        before = list(self.control.calls)
        self.assertEqual(planner.reconcile()['status'], 'action_ready')
        self.assertEqual(len(self.transports), 1)
        self.assertEqual(self.control.calls[len(before):][0][0], 'lookup_execution')
        self.assertFalse(any(name == 'reserve_execution' for name, _ in self.control.calls[len(before):]))
        self.assertEqual(planner.run()['status'], 'review_ready')
        self.assertEqual(len(self.transports), 2)
        self.assertEqual(len(self.backend.calls), 1)

    def test_lost_tool_finish_reconciles_same_receipt_before_next_model(self):
        self.actions = [self.action('test', name='unit'), self.action('final', summary='verified')]
        def lose(name, args):
            if name == 'start_execution' and self.control.rows[args['execution_id']]['provider'] == 'local_broker':
                self.control.lost.add('finish_execution')
        self.control.after = lose
        planner = self.create()
        self.assertEqual(planner.run()['status'], 'needs_review')
        self.control.after = None
        prior_starts = sum(name == 'start_execution' for name, _ in self.control.calls)
        self.assertEqual(planner.reconcile()['status'], 'ready')
        self.assertEqual(sum(name == 'start_execution' for name, _ in self.control.calls), prior_starts)
        self.assertEqual(planner.run()['status'], 'review_ready')
        self.assertEqual(len(self.backend.calls), 1)

    def test_crash_after_patch_receipt_recovers_without_applying_again(self):
        class ControllerCrash(BaseException):
            pass
        self.actions = [self.action('patch', path='main.py', expected_sha256=digest(b'value = 1\n'),
            content='value = 2\n'), self.action('test', name='unit'), self.action('final', summary='verified')]
        original = self.broker.patch_file
        calls = []
        def interrupted(*args):
            calls.append(args[0])
            original(*args)
            raise ControllerCrash()
        self.broker.patch_file = interrupted
        planner = self.create()
        with self.assertRaises(ControllerCrash):
            planner.run()
        self.assertEqual(planner.journal.read()['phase'], 'tool_intent')
        self.broker.patch_file = original
        self.assertEqual(planner.reconcile()['status'], 'ready')
        self.assertEqual((self.root / 'main.py').read_text(), 'value = 2\n')
        self.assertEqual(len(calls), 1)
        self.assertEqual(planner.run()['status'], 'review_ready')
        self.assertEqual(len(calls), 1)

    def test_crash_after_test_result_uses_backend_lookup_not_run(self):
        self.actions = [self.action('test', name='unit'), self.action('final', summary='verified')]
        self.backend.interrupt = True
        planner = self.create()
        from test_development_broker import Interrupted
        with self.assertRaises(Interrupted):
            planner.run()
        self.backend.interrupt = False
        self.assertEqual(planner.reconcile()['status'], 'ready')
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(len(self.backend.lookups), 1)
        self.assertEqual(planner.run()['status'], 'review_ready')
        self.assertEqual(len(self.backend.calls), 1)

    def test_new_broker_name_same_checkout_does_not_bypass_fence(self):
        self.create().close()
        other_path = self.base / 'other-broker'
        prepare_manifest(other_path, root=self.root, base_sha='a' * 40, scope=self.grant_scope,
            paths=['main.py'], resource='repo:fixture', authorize_scope=lambda *_: True,
            tests=[NamedTest('unit', backend_id=self.backend.backend_id)])
        other = DevelopmentBroker(other_path, authorize_scope=lambda *_: True,
                                  enabled=True, test_backend=self.backend)
        self.addCleanup(other.close)
        with self.assertRaises(DevelopmentError) as caught:
            self.create(broker=other)
        self.assertEqual(caught.exception.code, 'canonical_operation_conflict')
        self.assertFalse(self.transports)

    def test_runtime_profile_changed_refuses_before_repository_action(self):
        self.actions = [self.action('read', path='main.py')]
        def change(name, args):
            if name == 'finish_execution':
                self.control.rows[args['execution_id']]['profile_version'] += 1
        # Mutate the returned reservation itself, as a mismatched authenticated
        # server would; no account or credential is involved in this fixture.
        original = self.control.call
        def wrong(name, args):
            result = original(name, args)
            if name == 'reserve_execution':
                result['execution']['profile_version'] += 1
                self.control.rows[result['execution']['id']]['profile_version'] += 1
            return result
        self.control.call = wrong
        result = self.create().run()
        self.assertEqual(result['reason'], 'planner_runtime_scope_changed')
        self.assertEqual(len(self.control.rows), 1)
        self.assertFalse(self.transports)

    def test_wrong_action_grant_refuses_before_runtime_tool_admission(self):
        self.actions = [self.action('patch', path='main.py', expected_sha256=digest(b'value = 1\n'), content='x')]
        self.grant['actions'] = ['repo.read_file']
        result = self.create().run()
        self.assertEqual(result['reason'], 'planner_authorization_changed')
        self.assertEqual(len(self.control.rows), 1)
        self.assertEqual((self.root / 'main.py').read_text(), 'value = 1\n')

    def test_runtime_local_provider_replays_same_key_and_default_stays_unchanged(self):
        args = (self.scope, str(uuid.uuid4()), str(uuid.uuid4()), 'read_file')
        self.assertEqual(self.runtime.db.execute('PRAGMA user_version').fetchone()[0], 1)
        legacy = RuntimeJournal.configuration(*args)
        self.assertEqual(legacy['provider'], 'codex_chatgpt')
        local = RuntimeJournal.configuration(*args, provider='local_broker')
        operation, created = self.runtime.begin(local)
        self.assertTrue(created)
        self.assertEqual(self.runtime.db.execute('PRAGMA user_version').fetchone()[0], 2)
        self.assertEqual(self.runtime.begin(local), (operation, False))
        self.assertNotEqual(operation['key'], RuntimeJournal.key(legacy))
        with self.assertRaises(JournalError):
            RuntimeJournal.configuration(*args, provider='unknown')
        with self.assertRaises(JournalError):
            RuntimeJournal.configuration(*args[:3], 'shell script', provider='local_broker')

    def test_tool_guard_never_reports_local_text_as_auth_or_quota(self):
        planner = self.create()
        work = {**planner.work, 'model': 'read_file'}
        guard = ToolRuntimeGuard(self.runtime, self.control, self.scope, action_label='read_file')
        turn = str(uuid.uuid4())
        guard.before(work, turn)
        with self.assertRaises(Exception):
            guard.finish(work, turn, {'status': 'blocked', 'reason': 'quota_or_rate_limit',
                                     'verification': {'cleanup_confirmed': True}})
        self.assertEqual(guard.operation['phase'], 'needs_review')
        self.assertFalse(any(name == 'finish_execution' for name, _ in self.control.calls))
