"""Real temporary Board/feed/Runtime/journals; synthetic inference only."""
from contextlib import contextmanager
import json
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from test_host_journal import configuration
import test_runtime_integration as runtime_fixture
from test_runtime_integration import ControlClient
from test_board_worker import Board, Client
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.host_config import validate
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.host_supervisor import HostSupervisor
from blackoutburst_agents.message_host import MessageHostSupervisor, PinnedMessageClient
from blackoutburst_agents.message_journal import MessageJournal
from blackoutburst_agents.message_worker import MessageWorker
from blackoutburst_agents.message_runtime import MessageRuntimeGuard
from blackoutburst_agents.message_scope import local_agent
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runtime_journal import RuntimeJournal


class MessageFixtureClient(Client):
    def call(self,name,args):
        try:return super().call(name,args)
        except TimeoutError:raise BoardReadError('timeout') from None
        except Exception as error:
            if type(error).__name__=='BoardError':raise BoardReadError('tool_rejected') from None
            raise


@unittest.skipIf(Board is None,'Repository Board sources required')
class MessageHostTests(unittest.TestCase):
    def setUp(self):
        runtime_fixture.RuntimeIntegrationTests.setUp(self)
        self.root=self.root.resolve();self.client=MessageFixtureClient(self.board,self.raw)
        registration=self.client.register()
        self.thread=self.board.create_thread(self.human,'General',['owner'],idempotency_key='general')
        config=configuration(self.root);config['schema_version']=2;target=config['targets'][0]
        for key in ('project_id','ticket_id','after'):del target[key]
        target.update(agent_id=registration['identity']['id'],board_session_id=self.client.session_id,
            profile_version=registration['profile_version'],thread_ids=[self.thread['id']],start_at='beginning')
        self.config=validate(config,bind_connections=True)
        self.host=HostJournal(self.root/'host');self.addCleanup(self.host.close);self.host.configure(self.config)
        self.messages=MessageJournal(self.host.directory/'messages');self.addCleanup(self.messages.close)
        self.message_runs=RunStore(self.host.directory/'message-runs');self.addCleanup(self.message_runs.close)
        self.message_runtime=RuntimeJournal(self.host.directory/'message-runtime');self.addCleanup(self.message_runtime.close)
        self.control=ControlClient(self.board,self.raw,self.client.session_id)
        self.supervisor=MessageHostSupervisor(self.host,resource_factory=self.resources)
        self.calls=0;self.during=None;self.tick=0;self.response='PRIVATE_REPLY';self.source=None
        self.board.set_policy(self.human,True,True,True,False,[],[])

    @contextmanager
    def resources(self,host,target,config,cancelled):
        client=PinnedMessageClient(self.client,host,target,config['owner_id'],cancelled)
        worker=MessageWorker(self.messages,self.message_runs,client,
            runtime_guard_factory=lambda scope:MessageRuntimeGuard(self.message_runtime,self.control,scope,clock=lambda:self.tick),
            runner_factory=lambda runs,**kwargs:PersonalRunner(runs,self.transport,**kwargs))
        yield SimpleNamespace(journal=self.messages,worker=worker,client=client)

    def transport(self,settings,prompt,workspace,**callbacks):
        self.calls+=1;self.assertIsNone(callbacks['resume_session_id']);self.assertIn('PRIVATE_HUMAN',prompt)
        callbacks['on_started'](999999999);session=str(uuid.uuid4());callbacks['on_session'](session)
        if self.during:self.during(callbacks)
        return {'status':'completed','reason':'completed','session_id':session,'text':self.response,
            'verification':{'cleanup_confirmed':True,'tool_free':True}}

    def post(self,thread=None,*,human=True):
        self.source=self.board.post_message(self.human if human else self.client.actor,(thread or self.thread)['id'],
            'PRIVATE_HUMAN',mentions=['agent:'+self.config['targets'][0]['agent_id']],idempotency_key='source-'+uuid.uuid4().hex)
        return self.source

    def enable(self):
        with self.host.owner_lock(lock_directory=self.root/'locks'):self.host.enable(self.host.status()['revision'])

    def tick_host(self,**kwargs):
        with self.host.owner_lock(lock_directory=self.root/'locks'):return self.supervisor.tick(**kwargs)

    def replies(self,thread=None):
        return [m for m in self.board.get_thread(self.human,(thread or self.thread)['id'])['messages']
            if m['author']['kind']=='agent' and m['body']=='PRIVATE_REPLY']

    def operation(self):
        rows=self.messages.db.execute('SELECT id FROM operations ORDER BY rowid').fetchall()
        self.assertEqual(len(rows),1);return self.messages.operation(rows[0]['id'])

    def test_disabled_and_schema1_supervisor_routes_schema2_explicitly(self):
        with patch.object(self.supervisor,'resource_factory') as resources:
            self.assertEqual(self.tick_host()['status'],'disabled');resources.assert_not_called()
        self.post();self.enable();parent=HostSupervisor(self.host);parent.message_resource_factory=self.resources
        with self.host.owner_lock(lock_directory=self.root/'locks'):result=parent.tick()
        self.assertEqual(result['status'],'complete',result);self.assertEqual(self.calls,1)

    def test_general_reply_without_ticket_or_claim_is_managed_and_exact(self):
        source=self.post();self.enable();result=self.tick_host()
        self.assertEqual(result['status'],'complete',result);self.assertEqual(self.calls,1)
        self.assertEqual(len(self.replies()),1);self.assertEqual(self.replies()[0]['reply_to'],source['id'])
        runtime=self.board.get_runtime(self.human)['executions'];self.assertEqual(len(runtime),1)
        self.assertEqual(runtime[0]['scope_kind'],'message');self.assertIsNone(runtime[0]['ticket_id'])
        self.assertEqual(runtime[0]['state'],'finished');self.assertEqual(self.control.calls.count('reserve_message_execution'),1)
        self.assertFalse(any(name in ('claim_ticket','update_ticket','reserve_execution') for name,_ in self.client.calls))
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertEqual(self.host.status()['status'],'disabled')

    def _lost(self,name):
        self.post();self.enable();self.client.lost.add(name)
        first=self.tick_host();self.assertEqual(first['status'],'pending',first)
        second=self.tick_host();self.assertEqual(second['status'],'complete',second)
        self.assertEqual(self.calls,1);self.assertEqual(len(self.replies()),1)
    def test_lost_open(self):self._lost('open_message_feed')
    def test_lost_read(self):self._lost('read_message_feed')
    def test_lost_post(self):self._lost('post_message')
    def test_lost_ack(self):self._lost('checkpoint_message_feed')

    def test_lost_runtime_finish_reconciles_cached_result(self):
        self.post();self.enable();self.control.lost.add('finish_execution')
        self.assertEqual(self.tick_host()['status'],'pending')
        result=self.tick_host();self.assertEqual(result['status'],'complete',result)
        self.assertEqual(self.calls,1);self.assertEqual(len(self.replies()),1)
        self.assertEqual(self.control.calls.count('reserve_message_execution'),1)
        self.assertEqual(self.control.calls.count('start_execution'),1)

    def test_crash_after_budget_preallocation(self):
        self.post();self.enable()
        with patch.object(self.messages,'bind_worker',side_effect=SystemExit('crash')):
            with self.assertRaises(SystemExit):self.tick_host()
        identity=self.host.dispatches()[0]['worker_id'];self.assertEqual(self.calls,0)
        self.assertEqual(self.tick_host()['status'],'complete')
        self.assertEqual(self.operation()['worker_id'],identity);self.assertEqual(self.calls,1)

    def test_crash_after_model_before_receipt(self):
        self.post();self.enable();original=self.messages.update
        def crash(identifier,revision,state,**payload):
            if state=='result_ready':raise SystemExit('crash')
            return original(identifier,revision,state,**payload)
        with patch.object(self.messages,'update',side_effect=crash):
            with self.assertRaises(SystemExit):self.tick_host()
        self.assertEqual(self.calls,1);self.assertEqual(self.operation()['state'],'running')
        self.assertEqual(self.tick_host()['status'],'complete');self.assertEqual(self.calls,1)

    def test_crash_after_reply_before_parent_receipt(self):
        self.post();self.enable()
        with patch.object(self.host,'finish_dispatch',side_effect=SystemExit('crash')):
            with self.assertRaises(SystemExit):self.tick_host()
        self.assertEqual(len(self.replies()),1);result=self.tick_host()
        self.assertEqual(result['status'],'complete',result);self.assertEqual(self.calls,1)
        self.assertEqual(self.host.dispatches()[0]['state'],'complete')

    def test_crash_after_server_ack_before_local_receipt(self):
        self.post();self.enable()
        with patch.object(self.messages,'finish_ack',side_effect=SystemExit('crash')):
            with self.assertRaises(SystemExit):self.tick_host()
        self.assertEqual(self.tick_host()['status'],'complete');self.assertEqual(self.calls,1)

    def test_foreign_worker_id_never_adopted_even_empty(self):
        self.post();self.enable()
        with patch.object(self.messages,'bind_worker',side_effect=SystemExit('crash')):
            with self.assertRaises(SystemExit):self.tick_host()
        self.messages.db.execute('UPDATE operations SET worker_id=?',(str(uuid.uuid4()),))
        self.assertEqual(self.tick_host()['status'],'needs_review');self.assertEqual(self.calls,0)

    def test_foreign_run_store_never_adopted_even_empty(self):
        self.post();self.enable()
        with patch.object(self.message_runs,'create_work',side_effect=SystemExit('crash')):
            with self.assertRaises(SystemExit):self.tick_host()
        op=self.operation();scope=op['payload']['scope'];target=self.config['targets'][0]
        self.message_runs.create_work(owner_id='owner',agent_id=local_agent(scope),project_id=scope['thread_id'],
            ticket_id=scope['message_id'],workspace=target['workspace'],executable=target['executable'],model=target['model'])
        self.assertEqual(self.tick_host()['status'],'needs_review');self.assertEqual(self.calls,0)

    def test_missing_running_work_never_recreated(self):
        self.post();self.enable()
        with patch.object(self.message_runs,'get_work',side_effect=SystemExit('crash')):
            with self.assertRaises(SystemExit):self.tick_host()
        self.message_runs._db.execute('DELETE FROM works')
        self.assertEqual(self.tick_host()['status'],'needs_review');self.assertEqual(self.calls,0)

    def test_bot_and_unselected_messages_do_not_infer(self):
        self.post(human=False);other=self.board.create_thread(self.human,'Other',['owner'],idempotency_key='other')
        self.post(other);self.enable();result=self.tick_host()
        self.assertEqual(result['status'],'idle',result);self.assertEqual(self.calls,0)
        self.assertEqual(self.host.status()['remaining_dispatches'],1)

    def test_withdrawn_acl_after_model_never_posts_or_acks(self):
        self.post();self.enable()
        def revoke(callbacks):
            with self.board._tx() as db:db.execute('DELETE FROM thread_members WHERE thread_id=? AND owner_id=?',(self.thread['id'],'owner'))
        self.during=revoke;self.assertEqual(self.tick_host()['status'],'needs_review');self.assertEqual(self.calls,1)
        self.assertFalse(any(name in ('post_message','checkpoint_message_feed') for name,_ in self.client.calls))

    def test_changed_source_after_model_never_publishes(self):
        self.post();self.enable()
        def edit(callbacks):
            with self.board._tx() as db:db.execute('UPDATE messages SET body=? WHERE id=?',('edited',self.source['id']))
        self.during=edit;self.assertEqual(self.tick_host()['status'],'needs_review')
        self.assertEqual(self.calls,1);self.assertEqual(self.replies(),[])

    def test_budget_one_retains_second_message_until_enable(self):
        self.post();self.post();self.enable();self.assertEqual(self.tick_host()['status'],'complete')
        self.assertEqual(self.calls,1);self.assertEqual(self.host.status()['status'],'disabled')
        self.assertFalse(any(name=='checkpoint_message_feed' for name,_ in self.client.calls))
        self.enable();self.assertEqual(self.tick_host()['status'],'complete');self.assertEqual(self.calls,2)
        self.assertEqual(len(self.replies()),2)

    def test_oversized_result_requires_review_without_post_or_repeat(self):
        self.post();self.response='x'*8001;self.enable();self.assertEqual(self.tick_host()['status'],'needs_review')
        self.assertEqual(self.calls,1);self.assertEqual(self.tick_host()['status'],'disabled')
        self.assertFalse(any(name=='post_message' for name,_ in self.client.calls))


    def test_ticket_reply_preserves_task_and_requires_no_request_grant(self):
        with self.board._tx() as db:
            db.execute('UPDATE threads SET title=? WHERE id=?',('Ticket',self.ticket['thread_id']))
        self.host.close()
        self.config['targets'][0]['thread_ids']=[self.ticket['thread_id']]
        self.host=HostJournal(self.root/'ticket-host');self.addCleanup(self.host.close);self.host.configure(self.config)
        self.supervisor=MessageHostSupervisor(self.host,resource_factory=self.resources)
        thread={'id':self.ticket['thread_id']};self.post(thread);self.enable()
        result=self.tick_host();self.assertEqual(result['status'],'complete',result)
        current=self.board.get_ticket(self.human,self.ticket['id'])['ticket']
        self.assertEqual(current['status'],'todo');self.assertIsNone(current['claimant'])
        self.assertEqual(len(self.replies(thread)),1)
        self.assertEqual(self.board.get_runtime(self.human)['executions'][0]['ticket_id'],self.ticket['id'])

    def test_other_human_participant_is_answered_under_responders_scope(self):
        self.board.ensure_owner('other','Other')
        with self.board._tx() as db:db.execute('INSERT INTO thread_members VALUES(?,?)',(self.thread['id'],'other'))
        self.source=self.board.post_message({'kind':'human','owner_id':'other'},self.thread['id'],'PRIVATE_HUMAN',
            mentions=['agent:'+self.config['targets'][0]['agent_id']],idempotency_key='other-source')
        self.enable();result=self.tick_host();self.assertEqual(result['status'],'complete',result)
        reply=self.replies()[0];self.assertEqual(reply['author']['owner_id'],'owner')
        self.assertEqual(reply['reply_to'],self.source['id'])

    def test_paused_owner_never_calls_model_or_posts(self):
        self.post();limits=self.board.get_runtime(self.human)['limits']
        self.board.update_runtime_limits(self.human,limits['version'],1,True)
        self.enable();self.tick_host();self.assertEqual(self.calls,0);self.assertEqual(self.replies(),[])

    def test_connection_changed_during_model_cancels_without_post(self):
        self.post();self.enable()
        def change(callbacks):
            from pathlib import Path
            Path(self.config['targets'][0]['connection_file']).write_text('{}')
            self.assertTrue(callbacks['should_cancel']())
        self.during=change
        self.assertEqual(self.tick_host()['status'],'needs_review');self.assertEqual(self.calls,1)
        self.assertFalse(any(name=='post_message' for name,_ in self.client.calls))

    def test_runtime_cli_reconciles_message_finish_without_new_turn(self):
        import io
        from contextlib import redirect_stdout
        from blackoutburst_agents.cli import main
        self.post();self.enable();self.control.lost.add('finish_execution');self.tick_host()
        operation=self.message_runtime.list_status()[0]
        output=io.StringIO()
        with patch('blackoutburst_agents.message_runtime.MessageRuntimeClient.from_connection_file',return_value=self.control),redirect_stdout(output):
            code=main(['runtime','--journal',str(self.host.directory/'message-runtime'),'reconcile',
                '--operation',operation['id'],'--state',str(self.host.directory/'message-runs'),
                '--connection-file',self.config['targets'][0]['connection_file']])
        self.assertEqual(code,0,output.getvalue());self.assertNotIn('PRIVATE',output.getvalue())
        self.assertEqual(self.calls,1);self.assertEqual(self.control.calls.count('reserve_message_execution'),1)
        self.assertEqual(self.tick_host()['status'],'complete');self.assertEqual(self.calls,1)


if __name__=='__main__':unittest.main()
