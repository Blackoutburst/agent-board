"""One owner process multiplexing fixed targets, with an independent control loop.

The worker retains the original HostJournal owner lock. The network loop can
persist a stop while an existing supervised runner is busy. No model is used
to poll or interpret these closed metadata commands.
"""
import threading
import time

from .board_reader import BoardReadError
from .host_config import HostError,digest
from .host_journal import HostJournal
from .host_supervisor import HostSupervisor
from .server_control_protocol import response,_sha


def launch_catalog(configuration,source_release_sha256):
    """Hash PRIVATE fixed launch configuration and verified installed release."""
    _sha(source_release_sha256)
    entries=[]
    for target in configuration['targets']:
        entries.append({'catalog_key':target['id'],'profile_id':target['agent_id'],
            'profile_version':target['profile_version'],
            'mode':'messages' if configuration['schema_version']==2 else 'ticket_text',
            'launchspec_sha256':digest({'release':source_release_sha256,
                'configuration':digest(configuration),'target':target})})
    if len({e['profile_id'] for e in entries})!=len(entries):raise HostError('managed_profile_duplicate')
    return entries


class ServerController:
    def __init__(self,journal,client,*,supervisor_factory=HostSupervisor,clock=time.monotonic,
                 lock_directory=None,authority_seconds=15):
        if type(authority_seconds) not in (float,int) or not 1<=authority_seconds<=15:
            raise HostError('managed_authority_invalid')
        self.host,self.client,self.factory,self.clock=journal,client,supervisor_factory,clock
        self.lock_directory,self.authority_seconds=lock_directory,authority_seconds
        self.host_id=journal.managed_settings()['host_id']
        self._until=0.;self._commands={};self._busy=False;self._error=False
        self._mutex=threading.RLock();self._quit=threading.Event();self._wake=threading.Event()
        self._ready=threading.Event();self._thread=None
        self._owns_worker=False
        self._last_tick={}
        self._message_feeds={}
        self._known_pending=set()
        self._remote_seen_at=None

    @staticmethod
    def _dispatch_identity(journal,dispatch):
        target=journal.managed_target(dispatch['target_id'])
        return (dispatch['target_id'],dispatch['batch_id'],dispatch['worker_id'],
                dispatch['activation'],target['generation'],target['activation'])

    def _continuable_pending(self,journal,dispatch):
        return (dispatch['state']=='pending' and bool(dispatch['worker_id'])
            and journal.managed_target(dispatch['target_id'])['enabled']
            and self._dispatch_identity(journal,dispatch) in self._known_pending)

    def _cancelled(self):
        with self._mutex:return self._quit.is_set() or self._error or self.clock()>=self._until

    def _owned_call(self,name,*args):
        # Never hold the mutex across network I/O. Every local read or write
        # that participates in an effect is rechecked after that I/O instead.
        with self._mutex:
            if not self._owns_worker:raise HostError('managed_worker_not_owned')
            return getattr(self.host,name)(*args)

    def _effect(self,identity,method,args):
        record=self._owned_call('begin_managed_effect',identity,method,args)
        if record['state']=='superseded':raise HostError('managed_effect_superseded')
        if record['state']=='complete':return record['result']
        result=self.client.call(method,record['arguments'])
        return self._owned_call('finish_managed_effect',identity,result)['result']

    def publish(self,catalog):
        if self.host._messages_append_version()==5:
            from .message_append_journal import effective_message_catalog
            state=self.host._messages_binding_state()
            effective=effective_message_catalog(self.host,state['source_release_sha256'])
            if sorted(catalog,key=lambda e:e['catalog_key'])!=sorted(effective,key=lambda e:e['catalog_key']):
                raise HostError('managed_catalog_conflict')
        expected=self.host.managed_settings()['catalog_sha256']
        if digest(sorted(catalog,key=lambda e:e['catalog_key']))!=expected:
            raise HostError('managed_catalog_conflict')
        locals_=[self.host.managed_target(t['id']) for t in self.host.control()['configuration']['targets']]
        if all(t['instance_id'] for t in locals_):
            self.host.require_no_catalog_source_transition()
            result=self.client.call('poll_instance_commands',{'host_id':self.host_id,'limit':20})
            self._owned_call('managed_settings');response('poll_instance_commands',result)
            if result['host_id']!=self.host_id or result['catalog_version']<1:raise HostError('managed_catalog_conflict')
            self._instances(result['instances'])
            return {k:result[k] for k in ('host_id','catalog_version','instances')}
        if any(t['instance_id'] for t in locals_):raise HostError('managed_catalog_conflict')
        result=self._effect('catalog-'+expected,'publish_server_catalog',{
            'host_id':self.host_id,'expected_catalog_version':0,'entries':catalog,
            'idempotency_key':'managed-catalog-'+expected})
        self._instances(result['instances'])
        self._owned_call('bind_managed_instances',{i['catalog_key']:i['id'] for i in result['instances']})
        return result

    def _instances(self,instances):
        targets={t['id']:t for t in self.host.control()['configuration']['targets']}
        if {i['catalog_key'] for i in instances}!=set(targets):raise HostError('managed_catalog_conflict')
        for i in instances:
            target=targets[i['catalog_key']];local=self.host.managed_target(target['id'])
            if (i['host_id']!=self.host_id or i['profile_id']!=target['agent_id']
                    or i['profile_version']!=target['profile_version']
                    or i['mode']!=('messages' if self.host.control()['configuration']['schema_version']==2 else 'ticket_text')
                    or i['launchspec_sha256']!=local['launchspec_sha256']
                    or (local['instance_id'] and i['id']!=local['instance_id']) or not i['active']):
                raise HostError('managed_catalog_conflict')

    def start_worker(self):
        self.host.require_no_catalog_source_transition()
        if self._thread is not None:raise HostError('managed_worker_already_started')
        self._thread=threading.Thread(target=self._worker,name='fixed-owner-worker',daemon=False)
        self._thread.start()
        if not self._ready.wait(5) or self._error:
            self._quit.set();self._wake.set();raise HostError('managed_worker_unavailable')

    def _worker(self):
        try:
            with HostJournal(self.host.directory) as worker:
                with worker.owner_lock(lock_directory=self.lock_directory):
                    normal_exit=False
                    with self._mutex:self._owns_worker=True
                    try:
                        # Only the successful owner may alter startup state.
                        # A restart never resumes/reset an old activation.
                        worker.require_no_catalog_source_transition()
                        worker.stop()
                        supervisor=self.factory(worker)
                        supervisor.message_feed_ready=self._message_feed_ready
                        self._ready.set();next_tick=0.
                        while not self._quit.is_set():
                            self._wake.wait(.1);self._wake.clear()
                            if self._cancelled():
                                if worker.control()['enabled']:worker.stop('authorization_changed')
                                continue
                            with self._mutex:
                                commands=list(self._commands.values())
                                queued=[c for c in commands if (worker.managed_command(c['id']) or {}).get('state')=='queued']
                                if not queued and (not worker.control()['enabled'] or self.clock()<next_tick):continue
                                self._busy=True
                            try:
                                for c in queued:
                                    if self._cancelled():break
                                    try:worker.apply_managed_command(c['id'])
                                    except HostError as error:
                                        if error.code!='host_work_pending':raise
                                if not worker.control()['enabled']:continue
                                result=supervisor.tick(should_cancel=self._cancelled)
                                worker.settle_managed_budgets()
                                with self._mutex:
                                    self._last_tick=result
                                    # A live, successful supervisor return may wait
                                    # for a human or retry an exact durable effect.
                                    # Cold or explicitly uncertain children are
                                    # never adopted by this process-local set.
                                    self._known_pending={self._dispatch_identity(worker,d)
                                        for d in worker.dispatches()
                                        if result['status']=='pending' and d['state']=='pending'
                                        and d['worker_id'] and worker.managed_target(d['target_id'])['enabled']}
                                next_tick=self.clock()+worker.control()['configuration']['interval_seconds']
                            finally:
                                with self._mutex:self._busy=False
                        normal_exit=True
                    finally:
                        # Persist while still owning the process lock; serialize
                        # with shutdown so no stale object can stop a successor.
                        with self._mutex:
                            try:worker.stop('stopped' if normal_exit else 'needs_review')
                            finally:self._owns_worker=False
        except BaseException:
            with self._mutex:self._error=True;self._until=0.;self._busy=False
            self._ready.set()

    def _stop_authority(self):
        with self._mutex:
            self._until=0.;self._commands={}
            if self._owns_worker:self.host.stop('authorization_changed')
        self._wake.set()

    def _message_feed_ready(self,target_id,token):
        # The worker checked cancellation after the confirmed binding. Do not
        # use the control thread's SQLite connection here. step() rechecks this
        # observation against the target's exact current activation token.
        with self._mutex:
            if self._owns_worker and not self._cancelled():
                self._message_feeds[target_id]=token

    @staticmethod
    def _normalized(instance,c):
        return {'id':c['id'],'target_id':instance['catalog_key'],'instance_id':instance['id'],
            **{k:c[k] for k in ('generation','action','launchspec_sha256','budget')}}

    @staticmethod
    def _retired_observation(instance):
        c=instance['current_command']
        return (instance['observed_state']=='needs_review' or (c is not None and c['status']=='complete'
            and instance['observed_state'] in ('unknown','stopped','stopping')))

    def _remote_pending(self,instances):
        by_id={i['id']:i for i in instances}
        for effect in self.host.pending_managed_effects():
            if effect['method']=='catalog_source_transition':raise HostError('catalog_transition_pending')
            if effect['method'] in ('publish_server_catalog','replace_server_catalog'):continue
            args=effect['arguments']
            if effect['method']=='ack_instance_command':
                matches=[i for i in instances if i['command_id']==args['command_id']]
                stale=not matches or (not matches[0]['can_start'] and matches[0]['current_command']['action']!='stop')
            else:
                i=by_id.get(args['instance_id'])
                stale=i is None or any(i[k]!=args[k] for k in ('command_id','generation','launchspec_sha256'))
            if stale:
                self._owned_call('supersede_managed_effect',effect['id']);continue
            # Retry the exact original request, not a new revision under its key.
            try:self._effect(effect['id'],effect['method'],args)
            except BoardReadError as error:
                # A late RPC from the previous controller may have won this
                # revision. First resolve our original idempotency key; only a
                # confirmed rejection plus a newer fresh revision retires it.
                # Timeouts remain pending and can never manufacture a receipt.
                newer=(effect['method']=='report_instance_state'
                    and i['observation_revision']>args['expected_observation_revision']) or (
                    effect['method']=='ack_instance_command'
                    and matches[0]['current_command']['revision']>args['expected_revision'])
                if error.code!='tool_rejected' or not newer:raise
                self._owned_call('supersede_managed_effect',effect['id'])

    def _report(self,instance,state,cleanup,reason=''):
        command=instance['current_command']
        # Even an identical observation must settle an outstanding transition.
        settle=(command is not None and command['status'] in ('queued','accepted')
            and (state in ('ready','busy') or (state=='stopped' and cleanup)))
        seen=instance['last_seen_at']
        if (not settle and seen is not None and self._remote_seen_at is not None
                and 0<=self._remote_seen_at-seen<60
                and (instance['observed_state'],instance['cleanup_confirmed'],instance['reason'])
                    ==(state,cleanup,reason)):
            return instance
        args={'host_id':self.host_id,'instance_id':instance['id'],
            'expected_observation_revision':instance['observation_revision'],
            'generation':instance['generation'],'launchspec_sha256':instance['launchspec_sha256'],
            'command_id':instance['command_id'],'state':state,'cleanup_confirmed':cleanup,'reason':reason}
        identity='report-'+digest(args);args['idempotency_key']=identity
        return self._effect(identity,'report_instance_state',args)['instance']

    def step(self):
        """One bounded poll; local stops are persisted before ACKs or reports."""
        with self._mutex:
            if not self._owns_worker:raise HostError('managed_worker_not_owned')
        try:
            snapshot=self.client.call('poll_instance_commands',{'host_id':self.host_id,'limit':20})
            self._owned_call('managed_settings')
            response('poll_instance_commands',snapshot)
            if snapshot['host_id']!=self.host_id:raise HostError('managed_catalog_conflict')
            self._instances(snapshot['instances'])
            instances=snapshot['instances']
            # These revocations reach an active runner via its existing callback.
            for i in instances:
                local=self.host.managed_target(i['catalog_key'])
                if (not snapshot['enabled'] or not i['can_start'] or i['desired_state']=='stopped'
                        or local['generation']!=i['generation'] or self._retired_observation(i)):
                    self._owned_call('stop_target',i['catalog_key'],'authorization_changed' if not i['can_start'] else 'stopped')
                c=i['current_command']
                if c is not None and c['status'] in ('queued','accepted'):
                    if c['launchspec_sha256']!=i['launchspec_sha256'] or c['instance_version']!=i['version']:
                        if c['action']!='stop':raise HostError('managed_catalog_conflict')
                    self._owned_call('queue_managed_command',self._normalized(i,c))
            self._remote_pending(instances)
            # A recovered outbox response may be older than the poll. Refresh
            # authoritative CAS metadata instead of adopting that old response.
            collected_at=self.clock()
            snapshot=self.client.call('poll_instance_commands',{'host_id':self.host_id,'limit':20})
            self._owned_call('managed_settings')
            response('poll_instance_commands',snapshot);self._instances(snapshot['instances'])
            if snapshot['host_id']!=self.host_id:raise HostError('managed_catalog_conflict')
            self._remote_seen_at=snapshot['last_seen_at']
            authority_until=collected_at+self.authority_seconds
            admissions={}
            for i in snapshot['instances']:
                target=i['catalog_key'];c=i['current_command']
                if self.clock()>=authority_until:raise HostError('managed_authority_expired')
                if (c is not None and c['status'] in ('queued','accepted') and c['action']!='stop'
                        and c['budget']>self.host.control()['configuration']['dispatch_budget']):
                    # The web bound is shared; this owner's fixed launch policy
                    # may be tighter. Refuse without killing its control worker.
                    self._owned_call('stop_target',target,'stopped')
                    with self._mutex:
                        if not self._owns_worker:raise HostError('managed_worker_not_owned')
                        pending=any(d['state']!='complete' for d in self.host.dispatches())
                        busy=self._busy
                    self._report(i,'stopping' if busy else 'needs_review' if pending else 'stopped',
                        not busy and not pending,'launch_refused')
                    continue
                if c is not None and c['status']=='queued' and (c['action']=='stop' or (snapshot['enabled'] and i['can_start'])):
                    self._owned_call('queue_managed_command',self._normalized(i,c))
                    ack=self._effect('ack-'+c['id'],'ack_instance_command',{
                        'host_id':self.host_id,'command_id':c['id'],'expected_revision':c['revision'],
                        'idempotency_key':'managed-ack-'+c['id']})
                    if ack['command']['id']!=c['id']:raise HostError('managed_protocol_invalid')
                    # Only the fresh poll controls admission; ACK proves durable
                    # acceptance and is never proof of running or cleanup.
                    i=ack['instance'];c=i['current_command']
                with self._mutex:
                    if not self._owns_worker:raise HostError('managed_worker_not_owned')
                    local=self.host.managed_target(target)
                    busy=self._busy;failed=self._error
                    unfinished=[d for d in self.host.dispatches() if d['state']!='complete']
                    pending=bool(unfinished)
                    uncertain=any(not self._continuable_pending(self.host,d) for d in unfinished)
                    target_busy=busy and any(d['target_id']==target for d in unfinished)
                    feed_ready=(self.host.control()['configuration']['schema_version']!=2
                        or self._message_feeds.get(target)==self.host.target_token(target))
                    row=None if c is None else self.host.managed_command(c['id'])
                allowed=(snapshot['enabled'] and i['can_start'] and c is not None
                    and c['status'] in ('accepted','complete') and c['action']!='stop'
                    and c['instance_version']==i['version'] and c['launchspec_sha256']==i['launchspec_sha256']
                    and i['desired_state']=='running' and not self._retired_observation(i))
                if not allowed:self._owned_call('stop_target',target,'stopped')
                if failed or (not busy and (uncertain or local['reason']=='needs_review')):
                    self._owned_call('stop_target',target,'needs_review')
                    self._report(i,'needs_review',False,'cleanup_unconfirmed' if pending else 'execution_failed')
                    continue
                if not busy and c is not None and c['action']=='restart' and not c['stop_confirmed']:
                    i=self._report(i,'stopped',True,'stopped');c=i['current_command']
                if (allowed and row is not None and row['state']=='queued'
                        and (c['action']!='restart' or c['stop_confirmed'])):
                    i=self._report(i,'starting',False)
                    admissions[target]=self._normalized(i,c)
                elif local['enabled'] and allowed:
                    self._report(i,'busy' if target_busy else 'ready' if feed_ready else 'starting',False)
                elif busy:
                    self._report(i,'stopping',False,'stopped')
                else:
                    reason='budget_exhausted' if local['reason']=='budget_exhausted' else 'stopped'
                    self._report(i,'stopped',True,reason)
            with self._mutex:
                if not self._owns_worker:raise HostError('managed_worker_not_owned')
                if self.clock()>=authority_until:raise HostError('managed_authority_expired')
                self._commands=admissions;self._until=authority_until
            self._wake.set()
            return {'status':'connected','instances':len(snapshot['instances']),'model_calls_by_control_loop':0}
        except (BoardReadError,HostError):
            self._stop_authority();raise

    def run(self,*,should_cancel=lambda:False,interval=2):
        if type(interval) not in (float,int) or not .1<=interval<=5:raise HostError('managed_interval_invalid')
        if self._thread is None:self.start_worker()
        with self._mutex:
            if not self._owns_worker:raise HostError('managed_worker_not_owned')
        try:
            while not should_cancel():
                try:self.step()
                except (BoardReadError,HostError):pass
                if self._error:break
                if self._quit.wait(interval):break
        finally:self.shutdown()
        return {'status':'stopped' if not self._error else 'needs_review','model_calls_by_control_loop':0}

    def shutdown(self):
        self._quit.set();self._stop_authority();self._wake.set()
        if self._thread is not None:self._thread.join(5)
        if self._thread is not None and self._thread.is_alive():
            with self._mutex:self._error=True
            return False
        return not self._error
