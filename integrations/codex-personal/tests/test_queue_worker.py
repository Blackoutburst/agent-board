"""Queue integration with a real temporary board and a fake model only."""
from copy import deepcopy
import json
import sys
import unittest

import test_board_worker as fixtures
from blackoutburst_agents.queue_journal import QueueJournal
from blackoutburst_agents.queue_worker import QueueWorker


class Client(fixtures.Client):
    def register_for_selection(self):
        value = self.board.register_agent(self.raw, self.session_id, 'Worker', [], 'codex', True, 'worker')
        self.actor = self.board.actor_session(self.raw, self.session_id)
        self.registration = value
        return value


@unittest.skipIf(fixtures.Board is None, 'Repository Board not bundled')
class QueueWorkerTests(unittest.TestCase):
    make_worker = fixtures.BoardWorkerTests.make_worker
    transport = fixtures.BoardWorkerTests.transport
    read = fixtures.BoardWorkerTests.read
    change = fixtures.BoardWorkerTests.change

    def setUp(self):
        fixtures.BoardWorkerTests.setUp(self)
        self.client = Client(self.board, self.raw)
        self.client.register()
        self.worker = self.make_worker(client=self.client)
        self.queue_journal = QueueJournal(self.root/'queue')
        self.addCleanup(self.queue_journal.close)
        self.queue = QueueWorker(self.queue_journal, self.worker, self.client)
        self.queue_scope = {k:v for k,v in self.scope.items() if k not in ('project_id','ticket_id')}
        self.next_ticket = self.board.create_ticket(self.human, self.project['id'], 'Second ticket', idempotency_key='second')

    def run_queue(self, **changes):
        options = dict(scope=self.queue_scope, request_id='queue-request', project_id=self.project['id'],
                       instruction='Draft a short answer.', workspace=self.workspace, executable=sys.executable,
                       model='fixture-model', timeout=10)
        options.update(changes)
        return self.queue.run(**options)

    def selections(self): return [item for item in self.client.calls if item[0]=='claim_next_ticket']
    def publications(self): return [item for item in self.client.calls if item[0]=='update_ticket']

    def test_one_ticket_only_cached_duplicate_and_explicit_next_request(self):
        first=self.run_queue()
        self.assertEqual(first['status'],'complete')
        self.assertEqual(first['ticket_id'],self.ticket['id'])
        self.assertEqual(len(self.inferences),1)
        self.assertEqual(self.board.get_ticket(self.human,self.next_ticket['id'])['ticket']['status'],'todo')
        self.assertEqual(len(self.selections()),1)
        second=self.run_queue()
        self.assertEqual(first['operation_id'],second['operation_id'])
        self.assertTrue(second['cached'])
        self.assertEqual(len(self.inferences),1)
        third=self.run_queue(request_id='explicit-second-request')
        self.assertEqual(third['ticket_id'],self.next_ticket['id'])
        self.assertEqual(len(self.inferences),2)
        for private in ('PRIVATE_MODEL_RESULT','Draft a short answer','fixture-model','https://board.test'):
            self.assertNotIn(private,json.dumps(first))

    def test_lost_claim_reply_lookup_recovers_same_ticket_no_second_selection(self):
        self.client.lost.add('claim_next_ticket')
        result=self.run_queue()
        self.assertEqual(result['status'],'complete')
        self.assertEqual(len(self.selections()),1)
        self.assertEqual(len(self.inferences),1)
        self.assertGreaterEqual(len([x for x in self.client.calls if x[0]=='lookup_ticket_selection']),2)

    def test_lookup_unavailable_before_any_send_never_selects(self):
        call=self.client.call
        self.client.call=lambda name,args: (_ for _ in ()).throw(TimeoutError()) if name=='lookup_ticket_selection' else call(name,args)
        result=self.run_queue()
        self.assertEqual(result['status'],'pending')
        self.assertEqual(self.selections(),[])
        self.assertEqual(self.inferences,[])

    def test_ambiguous_absent_lookup_stops_without_resending(self):
        call=self.client.call
        def lost(name,args):
            if name=='claim_next_ticket':
                self.client.calls.append((name,deepcopy(args)))
                raise TimeoutError('PRIVATE no-response')
            return call(name,args)
        self.client.call=lost
        result=self.run_queue()
        self.assertEqual((result['status'],result['reason']),('needs_review','selection_unconfirmed'))
        self.assertEqual(self.run_queue()['status'],'needs_review')
        self.assertEqual(self.run_queue(request_id='escape')['reason'],'scope_has_unfinished_operation')
        self.assertEqual(len(self.selections()),1)
        self.assertEqual(self.inferences,[])

    def test_crash_after_selection_intent_before_send_never_retries_blindly(self):
        original=self.client.register_for_selection
        self.client.register_for_selection=lambda: (_ for _ in ()).throw(TimeoutError())
        first=self.run_queue()
        self.assertEqual(first['status'],'pending')
        self.assertEqual(self.selections(),[])
        self.client.register_for_selection=original
        second=self.run_queue()
        self.assertEqual(second['reason'],'selection_unconfirmed')
        self.assertEqual(self.selections(),[])

    def test_negative_selection_is_durable_when_new_ticket_later_appears(self):
        for t in (self.ticket,self.next_ticket):
            self.board.update_ticket(self.human,t['id'],t['version'],status='blocked')
        first=self.run_queue()
        self.assertEqual(first['status'],'idle')
        self.assertEqual(len(self.selections()),1)
        self.board.create_ticket(self.human,self.project['id'],'New ready ticket',idempotency_key='later')
        self.assertEqual(self.run_queue()['status'],'idle')
        self.assertEqual(len(self.selections()),1)
        self.assertEqual(self.inferences,[])

    def test_selection_response_persist_failure_reconciles_lookup_after_restart(self):
        original=self.queue_journal.update
        failures=[True]
        def update(identifier,revision,**kwargs):
            if kwargs['stage']=='selected' and failures:
                failures.pop(); raise OSError('PRIVATE disk')
            return original(identifier,revision,**kwargs)
        self.queue_journal.update=update
        first=self.run_queue()
        self.assertEqual(first['status'],'pending')
        second=self.run_queue()
        self.assertEqual(second['status'],'complete')
        self.assertEqual(first['operation_id'],second['operation_id'])
        self.assertEqual(len(self.selections()),1)
        self.assertEqual(len(self.inferences),1)

    def test_failure_after_child_complete_reuses_same_child_without_publication(self):
        original=self.queue_journal.finish_worker
        failures=[True]
        def finish(*args,**kwargs):
            if failures: failures.pop(); raise OSError('PRIVATE queue save')
            return original(*args,**kwargs)
        self.queue_journal.finish_worker=finish
        first=self.run_queue()
        self.assertEqual(first['status'],'pending')
        second=self.run_queue()
        self.assertEqual(second['status'],'complete')
        self.assertEqual(first['operation_id'],second['operation_id'])
        self.assertEqual(len(self.inferences),1)
        self.assertEqual(len(self.publications()),1)

    def test_profile_changed_after_selection_prevents_model(self):
        call=self.client.call
        def changed(name,args):
            result=call(name,args)
            if name=='claim_next_ticket':
                profile=self.board.get_agent_profile(self.human,self.scope['agent_id'])
                self.board.update_agent_profile(self.human,profile['id'],profile['version'],description='Changed')
            return result
        self.client.call=changed
        first=self.run_queue()
        self.assertIn(first['status'],('needs_review','pending'))
        self.assertEqual(self.inferences,[])
        self.assertEqual(len(self.selections()),1)
        self.assertEqual(self.run_queue(request_id='escape')['reason'],'scope_has_unfinished_operation')

    def test_project_changed_after_selection_prevents_model(self):
        call=self.client.call
        def changed(name,args):
            result=call(name,args)
            if name=='claim_next_ticket':
                self.board.update_project(self.human,self.project['id'],self.project['version'],description='Changed')
            return result
        self.client.call=changed
        result=self.run_queue()
        self.assertEqual(result['reason'],'project_changed')
        self.assertEqual(self.inferences,[])

    def test_expired_selected_claim_never_recovers_or_takes_another(self):
        call=self.client.call
        def expired(name,args):
            result=call(name,args)
            if name=='claim_next_ticket':
                with self.board._tx() as db:
                    db.execute('UPDATE tickets SET lease_expires_at=0 WHERE id=?',(self.ticket['id'],))
            return result
        self.client.call=expired
        result=self.run_queue()
        self.assertEqual(result['reason'],'claim_lost_or_expired')
        self.assertEqual(self.inferences,[])
        self.assertEqual(len(self.selections()),1)
        self.assertFalse(any(x[0]=='recover_ticket' for x in self.client.calls))

    def test_cancelled_or_blocked_model_holds_queue_for_review(self):
        def transport(settings,prompt,workspace,**callbacks):
            self.inferences.append({})
            return {'status':'blocked','text':None,'reason':'quota','verification':{'cleanup_confirmed':True}}
        self.worker.runner_factory=lambda state:fixtures.PersonalRunner(state,transport)
        result=self.run_queue()
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(self.run_queue(request_id='escape')['reason'],'scope_has_unfinished_operation')
        self.assertEqual(len(self.selections()),1)

    def test_cancellation_before_selection_creates_no_external_effect(self):
        result=self.run_queue(should_cancel=lambda:True)
        self.assertEqual(result['reason'],'cancel_requested')
        self.assertEqual(self.client.calls,[])
        self.assertEqual(self.inferences,[])

    def test_model_and_recipient_cannot_change_during_retries(self):
        self.run_queue(should_cancel=lambda:True)
        for kwargs in ({'model':'other'},{'human_owner_id':'owner'},{'project_id':'another'}):
            self.assertEqual(self.run_queue(**kwargs)['reason'],'request_configuration_conflict')
        self.assertEqual(self.inferences,[])

    def test_lookup_only_reconciliation_confirms_delayed_selection_without_model(self):
        call=self.client.call
        delayed=[]
        def lost(name,args):
            if name=='claim_next_ticket':
                delayed.append(deepcopy(args))
                raise TimeoutError()
            return call(name,args)
        self.client.call=lost
        first=self.run_queue()
        self.assertEqual(first['reason'],'selection_unconfirmed')
        self.assertEqual(self.queue.reconcile_selection(first['operation_id'])['reason'],'selection_unconfirmed')
        self.client.register_for_selection()
        self.board.claim_next_ticket(self.client.actor,**delayed[0])
        before=len(self.selections())
        result=self.queue.reconcile_selection(first['operation_id'])
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(result['reason'],'selection_reconciled')
        op=self.queue_journal.get(first['operation_id'])
        self.assertEqual(op['payload']['ticket_id'],self.ticket['id'])
        self.assertTrue(op['payload']['selection_confirmed'])
        self.assertEqual(len(self.selections()),before)
        self.assertEqual(self.inferences,[])
        self.assertEqual(self.run_queue(request_id='escape')['reason'],'scope_has_unfinished_operation')

    def test_child_identity_is_durable_before_worker_is_invoked(self):
        run=self.worker.run
        def checked(**kwargs):
            with self.queue_journal._db as db:
                identifier=db.execute('SELECT id FROM operations').fetchone()[0]
            queueop=self.queue_journal.get(identifier)
            self.assertEqual(queueop['stage'],'dispatched')
            child=self.journal.get(queueop['payload']['worker_operation_id'])
            self.assertEqual(child['stage'],'new')
            self.assertEqual(child['request_id'],kwargs['request_id'])
            return run(**kwargs)
        self.worker.run=checked
        self.assertEqual(self.run_queue()['status'],'complete')

    def test_precreated_child_is_reused_after_queue_id_save_failure(self):
        original=self.queue_journal.update
        failures=[True]
        def update(identifier,revision,**kwargs):
            if kwargs['stage']=='dispatched' and failures:
                failures.pop(); raise OSError('private child id save failure')
            return original(identifier,revision,**kwargs)
        self.queue_journal.update=update
        first=self.run_queue()
        self.assertEqual(first['status'],'pending')
        self.assertEqual(self.inferences,[])
        op=self.queue_journal.get(first['operation_id'])
        self.assertTrue(op['payload']['worker_preparing'])
        self.assertNotIn('worker_operation_id',op['payload'])
        second=self.run_queue()
        self.assertEqual(second['status'],'complete')
        self.assertEqual(self.journal._db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],1)
        self.assertEqual(len(self.inferences),1)


if __name__=='__main__': unittest.main()
