"""Explicit finite confirmed-message host; no listener/service installation."""
from contextlib import contextmanager, ExitStack
from types import SimpleNamespace
from .board_reader import BoardReadError
from .client_lifecycle import own_client
from .context import _hash
from .host_config import HostError, digest, connection_digest
from .message_client import MessageBoardClient
from .message_context import core, registration_profile
from .message_journal import MessageJournal
from .message_runtime import MessageRuntimeClient, MessageRuntimeGuard
from .message_worker import MessageWorker
from .run_store import RunStore
from .runtime_journal import RuntimeJournal


class PinnedMessageClient:
    def __init__(self,client,host,target,owner,cancelled):
        self.client,self.host,self.target,self.owner,self.cancelled=client,host,target,owner,cancelled
        self.endpoint,self.session_id=client.endpoint,client.session_id
        self.failure=None

    def check_current(self):
        if connection_digest(self.target['connection_file'])!=self.target['connection_sha256']:
            self.failure='authorization_changed';raise BoardReadError('host_authorization_changed')

    def register(self):
        self.check_current()
        try:
            registration=self.client.register()
            profile=registration_profile(registration,self.owner,self.target['agent_id'],self.session_id,self.target['profile_version'])
            self.host.pin_profile(self.target['id'],digest({'profile':profile,'policy':registration['policy'],
                'device_id':registration['identity']['device_id']}))
            return registration
        except BoardReadError as error:
            if error.code in ('timeout','connection_failed'):raise
            self.failure='authorization_changed';raise BoardReadError('host_authorization_changed') from None
        except Exception:
            self.failure='authorization_changed';raise BoardReadError('host_authorization_changed') from None

    def call(self,name,args):
        self.check_current()
        if self.cancelled():raise BoardReadError('host_stopped')
        return self.client.call(name,args)


@contextmanager
def default_message_resources(host,target,config,cancelled):
    with ExitStack() as resources:
        raw=own_client(resources,MessageBoardClient.from_connection_file(target['connection_file'],session_id=target['board_session_id']))
        client=PinnedMessageClient(raw,host,target,config['owner_id'],cancelled)
        journal=resources.enter_context(MessageJournal(host.directory/'messages'))
        runs=RunStore(host.directory/'message-runs');resources.callback(runs.close)
        runtime=resources.enter_context(RuntimeJournal(host.directory/'message-runtime'))
        control=own_client(resources,MessageRuntimeClient.from_worker(raw))
        worker=MessageWorker(journal,runs,client,runtime_guard_factory=lambda scope:MessageRuntimeGuard(runtime,control,scope))
        yield SimpleNamespace(journal=journal,worker=worker,client=client)


class MessageHostSupervisor:
    def __init__(self,host,*,resource_factory=default_message_resources):
        self.host,self.resource_factory=host,resource_factory

    def receipt(self,status,reason,**values):
        return {'status':status,'reason':reason,'remaining_dispatches':self.host.control()['remaining'],**values}

    def _reader(self,journal,target,config,instance):
        return journal.reader(target['id'],digest({'target':target,'owner_id':config['owner_id'],
            'board_endpoint':config['board_endpoint'],'host_instance':instance}),target['start_at'],config['page_limit'])

    @staticmethod
    def _page(response,target):
        if not isinstance(response,dict) or not isinstance(response.get('messages'),list):raise HostError('message_feed_invalid')
        messages=[]
        for item in response['messages']:
            message=core(item)
            if message['author']['kind']!='human':raise HostError('message_feed_bot')
            messages.append({'message_id':message['id'],'thread_id':message['thread_id'],'message_hash':_hash(message)})
        return messages

    def _ack(self,journal,client,delivery):
        delivery=journal.begin_ack(delivery['id'])
        reader=dict(journal.db.execute('SELECT * FROM readers WHERE id=?',(delivery['reader_id'],)).fetchone())
        result=client.call('checkpoint_message_feed',{'feed_id':reader['feed_id'],'delivery_id':delivery['remote_id'],
            'expected_checkpoint':delivery['checkpoint']})
        return journal.finish_ack(delivery['id'],result)

    def tick(self,*,should_cancel=None):
        self.host._require_lock();control=self.host.control()
        if not control['enabled']:return self.receipt('disabled',control['reason'])
        config=control['configuration'];activation=control['activation']
        if config['schema_version']!=2:raise HostError('invalid_host_configuration')
        def cancelled():
            if should_cancel is not None and should_cancel():self.host.stop();return True
            return self.host.cancelled(activation)
        if cancelled():return self.receipt('disabled','stopped')
        unfinished=[d for d in self.host.dispatches() if d['state']!='complete']
        if any(d['state']=='needs_review' for d in unfinished):
            self.host.stop('needs_review');return self.receipt('needs_review','message_needs_review')
        targets=config['targets'][control['next_target']:]+config['targets'][:control['next_target']]
        if unfinished:targets=[t for t in targets if t['id']==unfinished[0]['target_id']]
        active_id=None
        try:
            for target in targets:
                if cancelled():return self.receipt('disabled','stopped')
                if not unfinished and not self.host.target_selected(target['id']):continue
                token=self.host.target_token(target['id'])
                def target_cancelled(target_id=target['id'],target_token=token):
                    return cancelled() or self.host.target_cancelled(target_id,target_token)
                with self.resource_factory(self.host,target,config,target_cancelled) as resources:
                    client,journal,worker=resources.client,resources.journal,resources.worker
                    reader=self._reader(journal,target,config,control['instance_id'])
                    old=unfinished[0] if unfinished else None
                    if self.host.control()['remaining']==0 and not old and journal.pending(reader['id']) is None:
                        self.host.stop('budget_exhausted');return self.receipt('disabled','budget_exhausted')
                    client.register()
                    if not reader['feed_id']:
                        reader=journal.bind_feed(reader['id'],client.call('open_message_feed',{
                            'idempotency_key':reader['opening_key'],'start_at':reader['start_at'],'limit':reader['page_limit']}))
                    for page in range(config['max_pages']):
                        delivery=journal.pending(reader['id'])
                        if delivery is None:
                            response=client.call('read_message_feed',{'feed_id':reader['feed_id']})
                            delivery=journal.ingest(reader['id'],response,self._page(response,target))
                        if old:
                            linked=journal.operation(old['batch_id'])
                            if (linked['delivery_id']!=delivery['id'] or linked['worker_id'] not in ('',old['worker_id'])
                                    or (not linked['worker_id'] and linked['state']!='new')
                                    or old['subscription_id']!=reader['id']
                                    or old['request_id']!='host-message-'+linked['id']):raise HostError('host_reference_conflict')
                            if linked['state']=='complete':
                                self.host.finish_dispatch(linked['id'],'complete',worker.receipt(linked))
                                unfinished=[];old=None
                        # An ACK retry is never replaced by a fresh read.
                        if delivery['state']=='acking':
                            self._ack(journal,client,delivery);self.host.rotate()
                            if self.host.control()['remaining']==0:
                                self.host.stop('budget_exhausted');return self.receipt('complete','message_ack_confirmed')
                            return self.receipt('complete','message_ack_confirmed')
                        for operation in delivery['operations']:
                            if operation['state']=='new' and operation['thread_id'] not in target['thread_ids']:
                                journal.update(operation['id'],operation['revision'],'ignored',reason='thread_not_selected')
                        delivery=journal.delivery(delivery['id'])
                        remaining=[o for o in delivery['operations'] if o['state'] not in ('complete','ignored')]
                        if not remaining:
                            self._ack(journal,client,delivery)
                            if self.host.control()['remaining']==0:
                                self.host.stop('budget_exhausted');return self.receipt('complete','message_ack_confirmed')
                            reader=self._reader(journal,target,config,control['instance_id'])
                            if not delivery['operations']:
                                break
                            continue
                        operation=remaining[0];active_id=operation['id']
                        old=unfinished[0] if unfinished else None
                        if old and old['batch_id']!=operation['id']:raise HostError('host_reference_conflict')
                        if not old and self.host.control()['remaining']==0:
                            self.host.stop('budget_exhausted');return self.receipt('disabled','budget_exhausted')
                        batch={'id':operation['id'],'request_id':'host-message-'+operation['id'],'subscription_id':reader['id']}
                        dispatch=self.host.reserve_dispatch(target['id'],batch,activation)
                        operation=journal.bind_worker(operation['id'],dispatch['worker_id'])
                        self.host.link_worker(operation['id'],operation['worker_id'])
                        result=worker.run(operation['id'],target,config['owner_id'],should_cancel=target_cancelled)
                        if result.get('worker_operation_id')!=dispatch['worker_id']:raise HostError('host_reference_conflict')
                        state='complete' if result['status']=='complete' else 'needs_review' if result['status']=='needs_review' else 'pending'
                        self.host.finish_dispatch(operation['id'],state,result)
                        self.host.save_target(target['id'],result)
                        if state=='needs_review':
                            self.host.stop('needs_review');return self.receipt('needs_review',result['reason'],batch_id=operation['id'])
                        if state=='complete':
                            delivery=journal.delivery(delivery['id'])
                            if all(o['state'] in ('complete','ignored') for o in delivery['operations']):
                                self._ack(journal,client,delivery);self.host.rotate()
                            if self.host.control()['remaining']==0:self.host.stop('budget_exhausted')
                        return self.receipt(result['status'],result['reason'],batch_id=operation['id'],
                            worker_operation_id=dispatch['worker_id'])
                    self.host.rotate()
            return self.receipt('idle','no_selected_messages')
        except BoardReadError as error:
            if error.code in ('timeout','connection_failed'):
                return self.receipt('pending','board_unavailable')
            self.host.stop('needs_review');return self.receipt('needs_review','message_access_requires_review')
        except Exception:
            if active_id:
                try:self.host.finish_dispatch(active_id,'needs_review',{'status':'needs_review','reason':'message_reconciliation_required'})
                except Exception:pass
            self.host.stop('needs_review')
            return self.receipt('needs_review','message_reconciliation_required')

    def reconcile(self,batch_id):
        """Offline confirmation of an exact already-published reply only."""
        self.host._require_lock();control=self.host.control()
        if control['enabled']:raise HostError('host_must_be_stopped')
        dispatch=next((d for d in self.host.dispatches() if d['batch_id']==batch_id),None)
        if dispatch is None:raise HostError('host_reference_conflict')
        target=next(t for t in control['configuration']['targets'] if t['id']==dispatch['target_id'])
        with MessageJournal(self.host.directory/'messages') as journal:
            op=journal.operation(batch_id);delivery=journal.delivery(op['delivery_id'])
            reader=self._reader(journal,target,control['configuration'],control['instance_id'])
            if (op['state']!='complete' or op['worker_id']!=dispatch['worker_id']
                    or delivery['reader_id']!=reader['id'] or dispatch['subscription_id']!=reader['id']
                    or dispatch['request_id']!='host-message-'+op['id']
                    or not op['payload'].get('reply_id')):raise HostError('host_child_not_resolved')
            self.host.finish_dispatch(batch_id,'complete',{'status':'complete','reason':'human_reconciled',
                'worker_operation_id':op['worker_id']})
        return self.receipt('complete','human_reconciled',batch_id=batch_id)
