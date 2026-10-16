"""A scoped old decision reaches a simulated worker; reset never starts a new turn."""
import json
import unittest

from blackoutburst_agents.context import ContextScope
from blackoutburst_agents.decision_memory import DecisionMemory,memory_scope
from blackoutburst_agents.board_worker import BoardWorker
from blackoutburst_agents.runner import PersonalRunner
import test_board_worker as fixtures
import test_human_worker as human_fixtures


def attach_memory(case):
    case.memory=DecisionMemory((case.root/'decisions').resolve());case.addCleanup(case.memory.close)
    case.memory_scope=memory_scope(case.client.endpoint,ContextScope(**{k:case.scope[k] for k in
        ('owner_id','agent_id','project_id','ticket_id')}))
    ticket=case.read()
    source=case.board.post_message(case.human,ticket['thread_id'],'The owner approved local storage for this task.',idempotency_key='old-decision')
    case.source=source
    case.memory.pin(case.memory_scope,expected_revision=0,summary='Old decision: use local storage only.',
        thread_id=ticket['thread_id'],message_id=source['id'],call=case.client.call,registration=case.client.registration)
    for number in range(70):case.board.post_message(case.human,ticket['thread_id'],f'Later discussion {number}',idempotency_key=f'later-{number}')
    case.worker=BoardWorker(case.journal,case.store,case.client,
        runner_factory=lambda state:PersonalRunner(state,case.transport),decision_memory=case.memory)


@unittest.skipIf(fixtures.Board is None,'Repository backend required')
class DecisionWorkerTests(unittest.TestCase):
    make_worker=fixtures.BoardWorkerTests.make_worker
    run_worker=fixtures.BoardWorkerTests.run_worker
    transport=fixtures.BoardWorkerTests.transport
    read=fixtures.BoardWorkerTests.read
    change=fixtures.BoardWorkerTests.change
    def setUp(self):fixtures.BoardWorkerTests.setUp(self);attach_memory(self)

    def test_old_decision_is_used_and_duplicate_neither_infers_nor_republishes(self):
        first=self.run_worker();self.assertEqual(first['status'],'complete')
        self.assertEqual(len(self.inferences),1)
        self.assertIn('Old decision: use local storage only.',self.inferences[0]['prompt'])
        self.assertIn(self.source['id'],self.inferences[0]['prompt'])
        self.assertNotIn(str(self.memory.directory),self.inferences[0]['prompt'])
        publications=len([n for n,_ in self.client.calls if n=='update_ticket'])
        second=self.run_worker();self.assertTrue(second['cached'])
        self.assertEqual(len(self.inferences),1)
        self.assertEqual(len([n for n,_ in self.client.calls if n=='update_ticket']),publications)

    def test_new_operation_after_reset_uses_a_fresh_provider_session(self):
        first=self.run_worker();self.assertEqual(first['status'],'complete')
        original=self.journal.get(first['operation_id'])
        self.memory.forget(self.memory_scope,expected_revision=1)
        self.change(status='todo')
        second=self.run_worker(request_id='after-reset');self.assertEqual(second['status'],'complete')
        self.assertEqual(len(self.inferences),2)
        self.assertIsNone(self.inferences[1]['resume'])
        self.assertNotIn('Old decision: use local storage only.',self.inferences[1]['prompt'])
        self.assertNotEqual(original['payload']['work_id'],self.journal.get(second['operation_id'])['payload']['work_id'])

    def test_reset_between_context_and_turn_blocks_inference(self):
        original=self.client.call;reads=[]
        def reset(name,args):
            result=original(name,args)
            if name=='get_message':
                reads.append(name)
                if len(reads)==2:self.memory.forget(self.memory_scope,expected_revision=1)
            return result
        self.client.call=reset
        result=self.run_worker()
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(self.inferences,[])
        self.assertFalse(any(n=='update_ticket' for n,_ in self.client.calls))

    def test_reset_after_model_preserves_result_but_blocks_old_publication(self):
        self.after_model=lambda:self.memory.forget(self.memory_scope,expected_revision=1)
        result=self.run_worker()
        self.assertEqual(result['status'],'needs_review');self.assertEqual(len(self.inferences),1)
        operation=self.journal.get(result['operation_id'])
        self.assertEqual(operation['payload']['result']['text'],'PRIVATE_MODEL_RESULT')
        self.assertFalse(any(n=='update_ticket' for n,_ in self.client.calls))


@unittest.skipIf(fixtures.Board is None,'Repository backend required')
class DecisionHumanResumeTests(unittest.TestCase):
    make_worker=fixtures.BoardWorkerTests.make_worker
    read=fixtures.BoardWorkerTests.read
    change=fixtures.BoardWorkerTests.change
    run_worker=human_fixtures.HumanWorkerTests.run_worker
    transport=human_fixtures.HumanWorkerTests.transport
    requests=human_fixtures.HumanWorkerTests.requests
    answer=human_fixtures.HumanWorkerTests.answer
    def setUp(self):human_fixtures.HumanWorkerTests.setUp(self);attach_memory(self)

    def test_same_decision_is_revalidated_before_exact_human_resume(self):
        first=self.run_worker();self.assertEqual(first['stage'],'waiting_human')
        original=self.journal.get(first['operation_id'])
        provider_session=self.store.get_work(original['payload']['work_id'])['provider_session_id']
        self.answer();second=self.run_worker()
        self.assertEqual(second['status'],'complete');self.assertEqual(len(self.inferences),2)
        self.assertEqual(self.inferences[1]['resume'],provider_session)
        self.assertIn('Old decision: use local storage only.',self.inferences[1]['prompt'])

    def test_reset_during_wait_cannot_resurrect_cached_memory_or_start_second_turn(self):
        first=self.run_worker();self.assertEqual(first['stage'],'waiting_human')
        self.memory.forget(self.memory_scope,expected_revision=1);self.answer()
        second=self.run_worker()
        self.assertEqual(second['status'],'needs_review');self.assertEqual(len(self.inferences),1)
        self.assertFalse(any(n=='update_ticket' for n,_ in self.client.calls))

    def test_old_source_edit_during_wait_blocks_second_turn(self):
        first=self.run_worker();self.assertEqual(first['stage'],'waiting_human')
        self.answer()
        with self.board._tx() as db:db.execute('UPDATE messages SET body=? WHERE id=?',('The source decision changed.',self.source['id']))
        second=self.run_worker()
        self.assertEqual(second['status'],'needs_review');self.assertEqual(len(self.inferences),1)
        self.assertFalse(any(n=='update_ticket' for n,_ in self.client.calls))
