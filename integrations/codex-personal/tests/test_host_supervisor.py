"""Explicit host -> real Board, durable children and simulated inference only."""
from contextlib import contextmanager
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_host_journal import configuration
import test_runtime_integration as runtime_fixture
Board = runtime_fixture.Board
from blackoutburst_agents.host_config import validate
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.host_supervisor import HostSupervisor, PinnedClient
from blackoutburst_agents.event_drain import EventDrainer
from blackoutburst_agents.event_journal import EventJournal
from blackoutburst_agents.board_worker import BoardWorker
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents.runtime_guard import RuntimeGuard


@unittest.skipIf(Board is None,'Board sources required')
class HostSupervisorTests(unittest.TestCase):
    transport=runtime_fixture.RuntimeIntegrationTests.transport

    def setUp(self):
        runtime_fixture.RuntimeIntegrationTests.setUp(self)
        self.root=self.root.resolve()
        self.host=HostJournal(self.root/'host');self.addCleanup(self.host.close)
        self.events=EventJournal(self.root/'events');self.addCleanup(self.events.close)
        config=configuration(self.root)
        target=config['targets'][0]
        target.update({key:self.scope[key] for key in ('agent_id','project_id','ticket_id','board_session_id')})
        target['profile_version']=self.client.registration['profile_version']
        self.config=validate(config,bind_connections=True);self.host.configure(self.config)
        self.supervisor=HostSupervisor(self.host,resource_factory=self.resources)
        self.current_drainer=None

    @contextmanager
    def resources(self,host,target,scope,cancelled):
        client=PinnedClient(self.client,host,target,scope,cancelled)
        worker=BoardWorker(self.work,self.store,client,
            runner_factory=lambda store,**options:PersonalRunner(store,self.transport,**options),
            runtime_guard_factory=lambda scope:RuntimeGuard(self.runtime,self.control,scope,clock=lambda:self.tick))
        self.current_drainer=EventDrainer(self.events,worker,client)
        yield self.current_drainer

    def tick_host(self,**kwargs):
        with self.host.owner_lock(lock_directory=self.root/'locks'):
            return self.supervisor.tick(**kwargs)

    def enable(self):
        with self.host.owner_lock(lock_directory=self.root/'locks'):
            return self.host.enable(self.host.status()['revision'])

    def test_disabled_is_offline_and_never_opens_child(self):
        with patch.object(self.supervisor,'resource_factory') as resources:
            self.assertEqual(self.tick_host()['status'],'disabled');resources.assert_not_called()
        self.assertEqual(self.calls,0);self.assertEqual(self.client.calls,[])

    def test_thousand_empty_ticks_no_claim_model_publication_or_budget(self):
        self.enable()
        class Empty:
            client=object()
            def drain(_self,**options):
                self.assertFalse(options['execute'])
                return {'status':'idle','reason':'no_pending_events','after':0,'pages':1,'ingested_count':0}
        @contextmanager
        def empty(*args): yield Empty()
        self.supervisor.resource_factory=empty
        with self.host.owner_lock(lock_directory=self.root/'locks'):
            for _ in range(1000):self.assertEqual(self.supervisor.tick()['status'],'idle')
        self.assertEqual(self.host.status()['remaining_dispatches'],1)
        self.assertEqual(self.host.dispatches(),[]);self.assertEqual(self.calls,0);self.assertEqual(self.client.calls,[])

    def test_one_event_managed_then_budget_exhausted_no_new_work(self):
        self.enable();first=self.tick_host()
        self.assertEqual(first['status'],'complete',first)
        self.assertEqual(self.calls,1);self.assertEqual(self.control.calls.count('reserve_execution'),1)
        self.assertEqual(self.host.status()['remaining_dispatches'],0)
        self.assertEqual(self.tick_host()['reason'],'budget_exhausted');self.assertEqual(self.calls,1)
        self.assertEqual(self.board.get_ticket(self.human,self.ticket['id'])['ticket']['status'],'review')

    def test_lost_publication_retries_same_batch_worker_and_no_model(self):
        self.enable();self.client.lost.add('update_ticket')
        first=self.tick_host();self.assertEqual(first['status'],'pending',first)
        second=self.tick_host();self.assertEqual(second['status'],'complete',second)
        self.assertEqual(first['batch_id'],second['batch_id'])
        self.assertEqual(first['worker_operation_id'],second['worker_operation_id'])
        self.assertEqual(self.calls,1)
        self.assertEqual(len([1 for name,_ in self.client.calls if name=='update_ticket']),1)

    def test_crash_after_child_completion_before_host_receipt_reconciles(self):
        self.enable()
        with patch.object(self.host,'finish_dispatch',side_effect=SystemExit('simulated crash')):
            with self.assertRaises(SystemExit):self.tick_host()
        self.assertEqual(self.host.dispatches()[0]['state'],'pending')
        result=self.tick_host();self.assertEqual(result['reason'],'child_reconciled',result)
        self.assertEqual(self.calls,1);self.assertEqual(self.control.calls.count('reserve_execution'),1)

    def test_crash_after_budget_before_child_link_replays_same_empty_child(self):
        self.enable()
        with patch.object(self.host,'link_worker',side_effect=SystemExit('crash')):
            with self.assertRaises(SystemExit):self.tick_host()
        self.assertEqual(self.calls,0);self.assertEqual(self.host.status()['remaining_dispatches'],0)
        self.assertEqual(self.tick_host()['status'],'complete');self.assertEqual(self.calls,1)

    def test_restored_child_with_another_id_is_never_adopted(self):
        self.enable()
        with patch.object(self.host,'link_worker',side_effect=SystemExit('crash')):
            with self.assertRaises(SystemExit):self.tick_host()
        dispatch=self.host.dispatches()[0]
        with self.work.lock(self.scope):
            op=self.work.find(self.scope,dispatch['request_id'])
            # Simulate restoring another local journal under this location.
            import uuid
            self.work._db.execute('UPDATE operations SET id=? WHERE id=?',(str(uuid.uuid4()),op['id']))
        self.assertEqual(self.tick_host()['status'],'needs_review');self.assertEqual(self.calls,0)

    def test_false_profile_or_permission_never_claims(self):
        self.enable()
        profile=self.board.get_agent_profile(self.human,self.scope['agent_id'])
        self.board.update_agent_profile(self.human,profile['id'],profile['version'],description='Changed')
        result=self.tick_host();self.assertEqual(result['status'],'blocked',result)
        self.assertEqual(self.calls,0);self.assertFalse(any(name=='claim_ticket' for name,_ in self.client.calls))

    def test_connection_file_changed_before_tick_is_refused(self):
        self.enable()
        path=Path(self.config['targets'][0]['connection_file']);path.write_text('{}')
        result=self.tick_host();self.assertEqual(result['status'],'blocked',result);self.assertEqual(self.calls,0)

    def test_owner_runtime_pause_is_never_bypassed(self):
        self.enable();limits=self.board.get_runtime(self.human)['limits']
        self.board.update_runtime_limits(self.human,limits['version'],1,True)
        result=self.tick_host();self.assertEqual(result['status'],'needs_review',result)
        self.assertEqual(self.calls,0);self.assertEqual(self.host.status()['status'],'disabled')

    def test_stop_is_durable_during_turn_and_no_second_start(self):
        self.enable()
        def stop(callbacks):
            other=HostJournal(self.root/'host')
            try:other.stop()
            finally:other.close()
            self.assertTrue(callbacks['should_cancel']())
        self.during=stop;self.result_status='cancelled'
        self.assertEqual(self.tick_host()['status'],'needs_review')
        self.assertEqual(self.host.status()['status'],'disabled')
        self.assertEqual(self.host.dispatches()[0]['state'],'needs_review')
        self.assertEqual(self.tick_host()['status'],'disabled');self.assertEqual(self.calls,1)

    def test_agent_events_including_release_do_not_trigger_model(self):
        activity=self.board.list_activity(self.human,project_id=self.project['id'],ticket_id=self.ticket['id'],order='asc')
        after=max(item['id'] for item in activity)
        self.host.close();self.config['targets'][0]['after']=after
        self.host=HostJournal(self.root/'agent-events');self.addCleanup(self.host.close)
        self.host.configure(self.config);self.supervisor=HostSupervisor(self.host,resource_factory=self.resources)
        self.enable();actor=self.client.actor
        claimed=self.board.claim_ticket(actor,self.ticket['id'],self.ticket['version'])
        self.board.release_ticket(actor,self.ticket['id'],claimed['version'],'release','No work performed')
        result=self.tick_host();self.assertEqual(result['status'],'idle',result)
        self.assertEqual(self.calls,0);self.assertEqual(self.host.status()['remaining_dispatches'],1)

    def test_human_answer_reuses_budget_operation_two_turns_only(self):
        self.host.close();self.config['targets'][0]['human_owner_id']='owner'
        self.host=HostJournal(self.root/'human-host');self.addCleanup(self.host.close)
        self.host.configure(self.config);self.supervisor=HostSupervisor(self.host,resource_factory=self.resources)
        original=self.transport
        outputs=[{'kind':'question','text':'Which environment?'},{'kind':'result','text':'Use local fixtures.'}]
        def transport(*args,**kwargs):
            value=original(*args,**kwargs);value['text']=json.dumps(outputs[self.calls-1]);return value
        self.transport=transport;self.enable();first=self.tick_host()
        self.assertEqual(first['status'],'pending',first)
        self.assertEqual(self.host.status()['remaining_dispatches'],0)
        self.assertEqual(self.tick_host()['status'],'pending');self.assertEqual(self.calls,1)
        question=self.board.list_human_requests(self.human,self.project['id'],True)[0]
        self.board.resolve_human_request(self.human,question['id'],'Local fixtures')
        final=self.tick_host();self.assertEqual(final['status'],'complete',final)
        self.assertEqual(first['worker_operation_id'],final['worker_operation_id'])
        self.assertEqual(self.calls,2);self.assertEqual(self.host.status()['remaining_dispatches'],0)
        self.tick_host();self.assertEqual(self.calls,2)

    def test_foreground_wait_can_be_stopped_without_new_model(self):
        self.enable()
        clock=[0.0]
        class Empty:
            client=object()
            def drain(_self,**options):
                return {'status':'idle','reason':'no_pending_events','after':0,'pages':1,'ingested_count':0}
        @contextmanager
        def empty(*args):yield Empty()
        self.supervisor.resource_factory=empty
        self.supervisor.clock=lambda:clock[0]
        def wait(seconds):
            clock[0]+=seconds
            if clock[0]>=2:self.host.stop()
        with self.host.owner_lock(lock_directory=self.root/'locks'):
            result=self.supervisor.run(wait=wait,max_ticks=5)
        self.assertEqual(result['status'],'disabled')
        self.assertLessEqual(clock[0],2)
        self.assertEqual(self.calls,0)

    def test_transient_registration_network_error_does_not_enable_or_infer(self):
        from blackoutburst_agents.board_reader import BoardReadError
        self.enable()
        with patch.object(self.client,'register',side_effect=BoardReadError('connection_failed')):
            result=self.tick_host()
        self.assertEqual(result['reason'],'board_unavailable')
        self.assertEqual(self.host.status()['remaining_dispatches'],1)
        self.assertEqual(self.calls,0)
        self.assertEqual(self.tick_host()['status'],'complete')

    def test_assignment_elsewhere_does_not_consume_budget(self):
        self.enable()
        current=self.board.get_ticket(self.human,self.ticket['id'])['ticket']
        self.board.update_ticket(self.human,current['id'],current['version'],status='blocked')
        result=self.tick_host()
        self.assertEqual(result['status'],'waiting')
        self.assertEqual(self.host.status()['remaining_dispatches'],1)
        self.assertEqual(self.calls,0)
