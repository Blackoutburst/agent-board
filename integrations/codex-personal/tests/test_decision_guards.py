"""Guard persistence lives in the same RunStore DB, never an optional side file."""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch
import uuid

from blackoutburst_agents.run_store import RunStore,StoreError
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents.runtime_guard import RuntimeGuard
from blackoutburst_agents.context import ContextScope
from blackoutburst_agents.decision_memory import DecisionMemory,memory_scope
import test_runner as runner_fixtures
import test_runtime_integration as runtime_fixtures


def guard():return {'instance_id':str(uuid.uuid4()),'scope_key':'a'*64,'generation':1,'revision':1,'sha256':'b'*64}


class DecisionRunStoreGuardTests(unittest.TestCase):
    create_work=runner_fixtures.RunnerTests.create_work
    transport=runner_fixtures.RunnerTests.transport
    def setUp(self):runner_fixtures.RunnerTests.setUp(self);self.addCleanup(self.store.close)

    def test_omitting_or_changing_memory_guard_cannot_resume_or_replay_guarded_work(self):
        binding=guard();runner=PersonalRunner(self.store,self.transport)
        first=runner.run(self.work['id'],'first','original',context_guard=binding)
        for request,prompt,options in [('next','continue',{}),('first','original',{}),
                ('next','continue',{'context_guard':{**binding,'generation':2}})]:
            with self.assertRaises(StoreError):runner.run(self.work['id'],request,prompt,**options)
        self.assertEqual(len(self.calls),1)
        self.store.close();self.store=RunStore(self.state)
        with self.assertRaises(StoreError):PersonalRunner(self.store,self.transport).run(self.work['id'],'restart','continue')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.store.get_context_guard(self.work['id']),binding)
        self.assertTrue(PersonalRunner(self.store,self.transport).run(self.work['id'],'first','original',context_guard=binding)['cached'])
        self.assertEqual(first['result']['session_id'],self.store.get_work(self.work['id'])['provider_session_id'])

    def test_existing_unbound_provider_session_cannot_silently_adopt_memory(self):
        runner=PersonalRunner(self.store,self.transport)
        runner.run(self.work['id'],'old','legacy')
        with self.assertRaises(StoreError) as error:runner.run(self.work['id'],'memory','new',context_guard=guard())
        self.assertEqual(error.exception.code,'context_guard_session_unbound');self.assertEqual(len(self.calls),1)
        self.assertIsNone(self.store.get_context_guard(self.work['id']))

    def test_format_one_upgrade_preserves_provider_turns_and_adds_guard_without_reinterpretation(self):
        first=PersonalRunner(self.store,self.transport).run(self.work['id'],'legacy','old')
        self.store.close()
        with sqlite3.connect(self.state/'runs.sqlite3') as db:
            db.execute('DROP TABLE context_guards');db.execute('PRAGMA user_version=1')
            old=list(db.execute('SELECT * FROM works'));turns=list(db.execute('SELECT * FROM turns'))
        self.store=RunStore(self.state)
        self.assertIsNone(self.store.get_context_guard(self.work['id']))
        with sqlite3.connect(self.state/'runs.sqlite3') as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)
            self.assertEqual(list(db.execute('SELECT * FROM works')),old);self.assertEqual(list(db.execute('SELECT * FROM turns')),turns)
        second=PersonalRunner(self.store,self.transport).run(self.work['id'],'later','legacy continuation')
        self.assertEqual(second['result']['session_id'],first['result']['session_id'])

    def test_missing_format_two_guard_table_is_corruption_and_never_recreated(self):
        PersonalRunner(self.store,self.transport).run(self.work['id'],'first','original',context_guard=guard())
        self.store.close()
        with sqlite3.connect(self.state/'runs.sqlite3') as db:db.execute('DROP TABLE context_guards')
        with self.assertRaises(StoreError) as error:RunStore(self.state)
        self.assertEqual(error.exception.code,'context_guard_invalid')
        with sqlite3.connect(self.state/'runs.sqlite3') as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='context_guards'").fetchone())

    def test_guard_limit_is_transactional_and_does_not_create_a_turn_on_failure(self):
        binding=guard()
        for number in range(256):
            work=self.create_work(f'guarded-{number}')
            with self.store.work_lock(work['id']):self.store.check_context_guard(work['id'],binding)
        with self.assertRaises(StoreError) as error:PersonalRunner(self.store,self.transport).run(self.work['id'],'overflow','no model',context_guard=binding)
        self.assertEqual(error.exception.code,'context_guard_capacity');self.assertEqual(self.calls,[])
        self.assertIsNone(self.store.get_context_guard(self.work['id']))


class ManagedDecisionTests(unittest.TestCase):
    transport=runtime_fixtures.RuntimeIntegrationTests.transport
    run_worker=runtime_fixtures.RuntimeIntegrationTests.run_worker
    execution=runtime_fixtures.RuntimeIntegrationTests.execution
    def setUp(self):
        runtime_fixtures.RuntimeIntegrationTests.setUp(self)
        self.memory=DecisionMemory((self.root/'memory').resolve());self.addCleanup(self.memory.close)
        self.selected_scope=memory_scope(self.client.endpoint,ContextScope(**{k:self.scope[k] for k in ('owner_id','agent_id','project_id','ticket_id')}))
        source=self.board.post_message(self.human,self.ticket['thread_id'],'Approved local-only plan',idempotency_key='old-decision')
        self.memory.pin(self.selected_scope,expected_revision=0,summary='Use the approved local-only plan.',
            thread_id=self.ticket['thread_id'],message_id=source['id'],call=self.client.call,registration=self.client.registration)
        self.worker.decision_memory=self.memory

    def test_managed_memory_turn_and_replay_keep_one_reservation_and_correct_alias(self):
        result=self.run_worker();self.assertEqual(result['status'],'complete')
        self.assertEqual(self.execution()['state'],'finished');self.assertTrue(self.execution()['cleanup_confirmed'])
        self.assertEqual(self.calls,1);self.assertEqual(self.control.calls.count('reserve_execution'),1)
        self.assertTrue(self.run_worker()['cached']);self.assertEqual(self.calls,1)

    def test_reset_during_managed_turn_requests_cancel_and_releases_clean_slot_without_publication(self):
        def reset(callbacks):
            self.memory.forget(self.selected_scope,expected_revision=1)
            self.assertTrue(callbacks['should_cancel']())
        self.during=reset;self.result_status='cancelled'
        result=self.run_worker();self.assertEqual(result['status'],'needs_review')
        self.assertEqual(self.calls,1);self.assertEqual(self.execution()['state'],'finished')
        self.assertTrue(self.execution()['cleanup_confirmed']);self.assertEqual(self.board.get_runtime(self.human)['active_count'],0)
        self.assertFalse(any(n=='update_ticket' for n,_ in self.client.calls))
