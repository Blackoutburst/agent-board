"""Opt-in logical-task cycles; no model, queue, claim or execution entry point.

Only an in-memory permit can enter a new cycle driver. Replayed receipts never
reconstruct it. Callbacks are trusted controller code, not serialized grants;
existing Runtime/claim/fence guards still apply to every actual effect.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import math
import os
import re
import threading
import time
from types import MappingProxyType
from urllib.parse import urlsplit
import uuid

from .dev_manifest import canonical,digest
from .continuation_journal import ContinuationJournal,ContinuationError,require,identifier,encoded

USAGE_FIELDS=('turns','actions','operations','tests','read_bytes','patch_bytes')
PHASES=frozenset({'ready','running','paused_cycle','paused_provider','paused_quota','needs_you','stopped','complete'})
REASONS=frozenset({'','checkpoint','cycle','provider','quota','complete','stop_requested',
    'continuation_checks_failed','continuation_check_result_invalid','continuation_boundary_unknown',
    'continuation_scope_changed','continuation_profile_changed','continuation_claim_changed',
    'continuation_source_changed','continuation_fence_unknown','continuation_runtime_unknown',
    'continuation_provider_unconfirmed','continuation_quota_unconfirmed','continuation_cycle_limit',
    'continuation_clock_changed','controller_review'})
_FACTORY=object()


def _sha(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None)
    return value


def _uuid(value):
    require(type(value) is str)
    try:require(str(uuid.UUID(value))==value)
    except (ValueError,TypeError,AttributeError):raise ContinuationError('continuation_invalid') from None
    return value


def _same(left,right):return canonical(left)==canonical(right)


def validate_configuration(value):
    require(type(value) is dict and set(value)=={'schema','logical_task_id','scope','cycle_limits','initial_source_sha256'}
        and value['schema']=='continuation-task-v1')
    identifier(value['logical_task_id']);_sha(value['initial_source_sha256'])
    scope=value['scope']
    require(type(scope) is dict and set(scope)=={'owner_id','agent_id','project_id','ticket_id','device_id',
        'profile_version','resource','board_endpoint','source_release_sha256'})
    for key in ('owner_id','agent_id','project_id','ticket_id','device_id','resource'):identifier(scope[key])
    require(type(scope['profile_version']) is int and scope['profile_version']>0)
    _sha(scope['source_release_sha256'])
    require(type(scope['board_endpoint']) is str and len(scope['board_endpoint'])<=2048)
    try:url=urlsplit(scope['board_endpoint'])
    except ValueError:raise ContinuationError('continuation_invalid') from None
    require(url.scheme=='https' and url.hostname and not url.username and not url.password and not url.query and not url.fragment)
    limits=value['cycle_limits']
    require(type(limits) is dict and set(limits)=={'max_'+key for key in USAGE_FIELDS}|{'max_wall_seconds'})
    require(all(type(item) is int and 1<=item<=2**31 for item in limits.values()))
    encoded(value);return deepcopy(value)


def validate_usage(value,limits=None):
    require(type(value) is dict and set(value)==set(USAGE_FIELDS)
        and all(type(item) is int and item>=0 for item in value.values()))
    if limits is not None:require(all(value[key]<=limits['max_'+key] for key in USAGE_FIELDS),'continuation_cycle_limit')
    return dict(value)


def validate_checkpoint(value):
    require(type(value) is dict and set(value)=={'schema','source_sha256','diff_sha256','plan_sha256',
        'test_receipts','provider_receipt'} and type(value['schema']) is int and value['schema']==1)
    for key in ('source_sha256','diff_sha256','plan_sha256'):_sha(value[key])
    tests=value['test_receipts'];require(type(tests) is list and len(tests)<=16)
    seen=set()
    for receipt in tests:
        require(type(receipt) is dict and set(receipt)=={'test_name','receipt_sha256','source_sha256','outcome'})
        identifier(receipt['test_name']);_sha(receipt['receipt_sha256']);_sha(receipt['source_sha256'])
        require(receipt['test_name'] not in seen and receipt['source_sha256']==value['source_sha256']
            and receipt['outcome'] in {'passed','failed','cancelled','not_started'})
        seen.add(receipt['test_name'])
    provider=value['provider_receipt']
    if provider is not None:
        require(type(provider) is dict and set(provider)=={'work_id','turn_id','receipt_sha256'})
        _uuid(provider['work_id']);_uuid(provider['turn_id']);_sha(provider['receipt_sha256'])
    encoded(value);return deepcopy(value)


def validate_state(state,configuration):
    require(type(state) is dict and set(state)=={'revision','logical_task_id','configuration_sha256','phase',
        'stop_requested','cycle_seq','attempt_seq','current_cycle','checkpoint','checkpoint_sha256','used_total','reason'})
    require(type(state['revision']) is int and state['revision']>=0
        and state['logical_task_id']==configuration['logical_task_id']
        and state['configuration_sha256']==digest(encoded(configuration))
        and type(state['phase']) is str and state['phase'] in PHASES
        and type(state['stop_requested']) is bool and state['stop_requested']==(state['phase']=='stopped')
        and type(state['reason']) is str and state['reason'] in REASONS)
    for key in ('cycle_seq','attempt_seq'):require(type(state[key]) is int and state[key]>=0)
    require(state['attempt_seq']==state['cycle_seq'])
    validate_usage(state['used_total'])
    cp=state['checkpoint']
    if cp is None:require(state['checkpoint_sha256'] is None)
    else:validate_checkpoint(cp);require(digest(encoded(cp))==state['checkpoint_sha256'])
    cycle=state['current_cycle']
    if cycle is None:require(state['cycle_seq']==0 and state['phase'] in {'ready','needs_you','stopped'})
    else:
        require(type(cycle) is dict and set(cycle)=={'generation_id','cycle_id','attempt_id','cycle_seq','attempt_seq','started_at','used','sealed'})
        for key in ('generation_id','cycle_id','attempt_id'):_uuid(cycle[key])
        require(state['phase']!='ready' and type(cycle['cycle_seq']) is int and type(cycle['attempt_seq']) is int
            and len({cycle[key] for key in ('generation_id','cycle_id','attempt_id')})==3
            and cycle['cycle_seq']==state['cycle_seq'] and cycle['attempt_seq']==state['attempt_seq']
            and type(cycle['started_at']) in (int,float) and not isinstance(cycle['started_at'],bool)
            and math.isfinite(cycle['started_at']) and cycle['started_at']>0 and type(cycle['sealed']) is bool)
        validate_usage(cycle['used'],configuration['cycle_limits'])
        require(all(state['used_total'][key]>=cycle['used'][key] for key in USAGE_FIELDS))
        if state['phase']=='running':require(cycle['sealed'] is False)
        if state['phase'] in {'paused_cycle','paused_provider','paused_quota','complete'}:require(cycle['sealed'] is True and cp is not None)
    encoded(state);return deepcopy(state)


@dataclass(frozen=True)
class LiveCheckContext:
    logical_task_id:str
    state_sha256:str
    generation_id:object
    cycle_id:object
    attempt_id:object
    checkpoint_sha256:object
    source_sha256:str
    purpose:str
    scope:object
    cycle_used:object
    provider_receipt_sha256:object


class ContinuationChecks:
    """Trusted fresh checks return None or raise; serialized grants are refused.

    The Runtime callback positively checks only this attempt's terminal or
    never-started state and pending effects. It cannot attest global process
    absence. Arbitrary Python in this trusted controller remains outside this
    cooperative boundary, as with the existing live Runtime/journal handles.
    """
    __slots__=('callbacks','provider_observation','quota_observation','pid','thread')
    def __init__(self,*,scope,claim,source,fence,runtime,resume,provider_observation=None,quota_observation=None):
        values=(scope,claim,source,fence,runtime,resume)
        require(all(callable(value) for value in values)
            and all(value is None or callable(value) for value in (provider_observation,quota_observation)))
        self.callbacks=values;self.provider_observation=provider_observation;self.quota_observation=quota_observation
        self.pid,self.thread=os.getpid(),threading.current_thread()
    def verify(self,context,*,provider=False,quota=False):
        require(type(context) is LiveCheckContext and self.pid==os.getpid() and self.thread is threading.current_thread(),
            'continuation_checks_failed')
        calls=self.callbacks if context.purpose in {'start','execute'} else self.callbacks[:5]
        for callback in calls:require(callback(context) is None,'continuation_check_result_invalid')
        if provider:
            require(self.provider_observation is not None,'continuation_provider_unconfirmed')
            require(self.provider_observation(context) is None,'continuation_check_result_invalid')
        if quota:
            require(self.quota_observation is not None,'continuation_quota_unconfirmed')
            require(self.quota_observation(context) is None,'continuation_check_result_invalid')


class _Ephemeral:
    def __init__(self,factory,task,context,payload_sha):
        require(factory is _FACTORY)
        self.task,self.context,self.payload_sha=task,context,payload_sha
        self.pid,self.thread,self.active=os.getpid(),threading.current_thread(),True
    def __reduce_ex__(self,_):raise TypeError('Continuation permits cannot be serialized')
    def __copy__(self):raise TypeError('Continuation permits cannot be copied')
    def __deepcopy__(self,_):raise TypeError('Continuation permits cannot be copied')
    def take(self,task,context,payload_sha):
        require(self.active and self.task is task and self.pid==os.getpid() and self.thread is threading.current_thread()
            and self.context==context and self.payload_sha==payload_sha,'continuation_boundary_unknown')
        self.active=False


class CleanBoundary(_Ephemeral):pass


class LiveCyclePermit(_Ephemeral):
    @contextmanager
    def use(self):
        snapshot=self.task.journal.snapshot();state=snapshot['state']
        context=self.task._context(snapshot,purpose='execute')
        self.take(self.task,context,self.payload_sha)
        require(state['phase']=='running' and state['stop_requested'] is False,'continuation_stopped')
        self.task.checks.verify(context)
        require(self.task.journal.snapshot()==snapshot,'continuation_permit_stale')
        yield context


@dataclass(frozen=True)
class CycleStart:
    receipt:dict
    permit:object


class ContinuationTask:
    def __init__(self,journal,checks,*,clock=time.time):
        require(type(journal) is ContinuationJournal and type(checks) is ContinuationChecks and callable(clock))
        self.journal,self.checks,self.clock=journal,checks,clock
        self.configuration=deepcopy(journal.configuration)
    @classmethod
    def create(cls,directory,*,logical_task_id,scope,cycle_limits,initial_source_sha256,checks,clock=time.time):
        config={'schema':'continuation-task-v1','logical_task_id':logical_task_id,'scope':deepcopy(scope),
            'cycle_limits':deepcopy(cycle_limits),'initial_source_sha256':initial_source_sha256}
        validate_configuration(config)
        state={'revision':0,'logical_task_id':logical_task_id,'configuration_sha256':digest(encoded(config)),
            'phase':'ready','stop_requested':False,'cycle_seq':0,'attempt_seq':0,'current_cycle':None,
            'checkpoint':None,'checkpoint_sha256':None,'used_total':dict.fromkeys(USAGE_FIELDS,0),'reason':''}
        return cls(ContinuationJournal.create(directory,config,state),checks,clock=clock)
    def snapshot(self):return self.journal.snapshot()
    def close(self):self.journal.close()
    def _time(self,cycle=None):
        value=self.clock()
        require(type(value) in (int,float) and math.isfinite(value) and value>0
            and (cycle is None or value>=cycle['started_at']),'continuation_clock_changed')
        return value
    def _context(self,snapshot,*,purpose,checkpoint=None,used=None):
        state=snapshot['state'];cycle=state['current_cycle'];cp=state['checkpoint'] if checkpoint is None else checkpoint
        provider=None if cp is None else cp['provider_receipt']
        return LiveCheckContext(state['logical_task_id'],snapshot['state_sha256'],
            None if cycle is None else cycle['generation_id'],None if cycle is None else cycle['cycle_id'],
            None if cycle is None else cycle['attempt_id'],None if cp is None else digest(encoded(cp)),
            self.configuration['initial_source_sha256'] if cp is None else cp['source_sha256'],purpose,
            MappingProxyType(deepcopy(self.configuration['scope'])),
            MappingProxyType(dict.fromkeys(USAGE_FIELDS,0) if cycle is None else dict(cycle['used']) if used is None else dict(used)),
            None if provider is None else provider['receipt_sha256'])
    @staticmethod
    def _failure(error):
        code=getattr(error,'code',None)
        return code if type(code) is str and code in REASONS else 'continuation_checks_failed'
    def _lookup(self,expected_revision,request_id,payload):
        # An identity-preserving duplicate may be inspected after Stop, but it
        # never recreates a live permit. The append rechecks this under its lock.
        request_sha=digest(encoded(payload));identifier(request_id)
        self.journal._schema()
        row=self.journal.db.execute('SELECT * FROM events WHERE request_id=?',(request_id,)).fetchone()
        if row is None:return None
        require(row['request_sha256']==request_sha,'continuation_request_conflict')
        old=self.journal._row(row)
        return {'snapshot':self.snapshot(),'receipt':{'request_id':request_id,'revision':row['revision'],
            'event_sha256':old['event_sha256'],'state_sha256':old['state_sha256']},'historical':True,'launchable':False}
    def start_cycle(self,*,expected_revision,request_id):
        payload={'kind':'start','expected_revision':expected_revision}
        replay=self._lookup(expected_revision,request_id,payload)
        if replay is not None:return CycleStart(replay,None)
        before=self.snapshot();state=before['state']
        require(state['revision']==expected_revision,'continuation_revision_conflict')
        require(not state['stop_requested'] and state['phase'] in {'ready','paused_cycle','paused_provider','paused_quota'},'continuation_start_refused')
        context=self._context(before,purpose='start');failure=None
        try:self.checks.verify(context);now=self._time(state['current_cycle'])
        except Exception as error:failure=self._failure(error);now=None
        def transition(head):
            require(head==before,'continuation_revision_conflict');value=head['state']
            if failure is not None:value.update(phase='needs_you',reason=failure);return value
            value['cycle_seq']+=1;value['attempt_seq']+=1
            value['current_cycle']={'generation_id':str(uuid.uuid4()),'cycle_id':str(uuid.uuid4()),'attempt_id':str(uuid.uuid4()),
                'cycle_seq':value['cycle_seq'],'attempt_seq':value['attempt_seq'],'started_at':now,
                'used':dict.fromkeys(USAGE_FIELDS,0),'sealed':False}
            value.update(phase='running',reason='');return value
        result=self.journal._append(expected_revision=expected_revision,request_id=request_id,payload=payload,transition=transition)
        permit=None
        if not result['historical'] and result['snapshot']['state']['phase']=='running':
            context=self._context(result['snapshot'],purpose='execute')
            permit=LiveCyclePermit(_FACTORY,self,context,digest(encoded(payload)))
        return CycleStart(result,permit)
    def clean_boundary(self,*,expected_revision,checkpoint,cycle_used,reason):
        require(reason in {'checkpoint','cycle','provider','quota','complete'})
        checkpoint=validate_checkpoint(checkpoint);used=validate_usage(cycle_used,self.configuration['cycle_limits'])
        before=self.snapshot();require(before['state']['revision']==expected_revision,'continuation_revision_conflict')
        require(before['state']['phase']=='running' and not before['state']['stop_requested'],'continuation_boundary_unknown')
        context=self._context(before,purpose=reason,checkpoint=checkpoint,used=used)
        self.checks.verify(context,provider=reason in {'provider','quota'},quota=reason=='quota')
        require(self.snapshot()==before,'continuation_revision_conflict')
        payload={'kind':'checkpoint','expected_revision':expected_revision,'checkpoint':checkpoint,'cycle_used':used,'reason':reason}
        return CleanBoundary(_FACTORY,self,context,digest(encoded(payload)))
    def checkpoint(self,*,expected_revision,request_id,checkpoint,cycle_used,reason,boundary=None):
        require(reason in {'checkpoint','cycle','provider','quota','complete','needs_you'})
        cp=validate_checkpoint(checkpoint);used=validate_usage(cycle_used,self.configuration['cycle_limits'])
        payload={'kind':'checkpoint','expected_revision':expected_revision,'checkpoint':cp,'cycle_used':used,'reason':reason}
        replay=self._lookup(expected_revision,request_id,payload)
        if replay is not None:return replay
        before=self.snapshot();state=before['state'];cycle=state['current_cycle']
        require(state['revision']==expected_revision,'continuation_revision_conflict')
        require(state['phase']=='running' and not state['stop_requested'] and cycle is not None,'continuation_checkpoint_refused')
        require(all(used[key]>=cycle['used'][key] for key in USAGE_FIELDS),'continuation_usage_regressed')
        context=self._context(before,purpose=reason,checkpoint=cp,used=used);failure=None
        try:
            require(reason!='needs_you','controller_review')
            require(type(boundary) is CleanBoundary,'continuation_boundary_unknown')
            boundary.take(self,context,digest(encoded(payload)))
            self.checks.verify(context,provider=reason in {'provider','quota'},quota=reason=='quota')
            now=self._time(cycle)
            if reason=='checkpoint':
                require(now<cycle['started_at']+self.configuration['cycle_limits']['max_wall_seconds']
                    and all(used[key]<self.configuration['cycle_limits']['max_'+key] for key in USAGE_FIELDS),
                    'continuation_cycle_limit')
        except Exception as error:failure=self._failure(error)
        def transition(head):
            require(head==before,'continuation_revision_conflict');value=head['state'];current=value['current_cycle']
            for key in USAGE_FIELDS:value['used_total'][key]+=used[key]-current['used'][key]
            current['used']=used
            if failure is not None:value.update(phase='needs_you',reason=failure);return value
            value['checkpoint']=cp;value['checkpoint_sha256']=digest(encoded(cp))
            value.update(phase={'checkpoint':'running','cycle':'paused_cycle','provider':'paused_provider',
                'quota':'paused_quota','complete':'complete'}[reason],reason=reason)
            current['sealed']=reason!='checkpoint';return value
        return self.journal._append(expected_revision=expected_revision,request_id=request_id,payload=payload,transition=transition)
    def stop(self,*,expected_revision,request_id):
        payload={'kind':'stop','expected_revision':expected_revision}
        def transition(head):
            value=head['state'];value.update(phase='stopped',stop_requested=True,reason='stop_requested');return value
        return self.journal._append(expected_revision=expected_revision,request_id=request_id,payload=payload,transition=transition)
