"""Live private-checkpoint facts at a clean development ready boundary.

No model/test/broker effect, budget charge, result_ready promotion, fence
transfer, claim mutation or delivery proof. Git must additionally inventory its
whole tree and compare unselected paths/modes/base inside this live hold.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import fcntl
import os
import threading
import uuid

from .dev_journal import PrivateDirectory,decode
from .dev_manifest import DevelopmentError,canonical,digest,opaque,sha256,RepositoryFiles

_ISSUER=object()


def require(value,code='development_checkpoint_unconfirmed'):
    if not value:raise DevelopmentError(code)


@dataclass(frozen=True)
class DevelopmentCheckpointRequest:
    worker:object
    task_id:str
    generation_id:str
    cycle_id:str
    attempt_id:str
    original_public_base:str
    source_release_sha256:str
    parent_checkpoint_sha256:object=None
    after_actions:int=1

    def validate(self,planner):
        from .development_worker import DevelopmentBoardWorker
        from .dev_planner import DevelopmentPlanner
        require(type(planner) is DevelopmentPlanner and type(self.worker) is DevelopmentBoardWorker
            and self.worker.store is planner.store and self.worker.runtime is planner.runtime_journal
            and self.worker.control is planner.client and planner.parent_operation_id is not None)
        opaque(self.task_id)
        for value in (self.generation_id,self.cycle_id,self.attempt_id):
            try:require(type(value) is str and str(uuid.UUID(value))==value)
            except (ValueError,TypeError,AttributeError):raise DevelopmentError('development_checkpoint_request_invalid') from None
        require(len({self.generation_id,self.cycle_id,self.attempt_id})==3
            and type(self.after_actions) is int and 1<=self.after_actions<=64)
        # A base is an object identifier, never a branch or ref supplied by text.
        require(type(self.original_public_base) is str and len(self.original_public_base) in (40,64)
            and all(char in '0123456789abcdef' for char in self.original_public_base))
        sha256(self.source_release_sha256)
        if self.parent_checkpoint_sha256 is not None:sha256(self.parent_checkpoint_sha256)


@dataclass(frozen=True)
class DevelopmentCheckpointReady:
    status:str
    operation_id:str
    proof:object


@contextmanager
def _read_work(store,work_id):
    """Existing exclusive lock only; never mark interrupted turns or create it."""
    require(work_id not in store._locks,'development_checkpoint_work_busy')
    descriptor=None
    try:
        with PrivateDirectory(store.state_dir) as private:
            name=work_id+'.lock';descriptor=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=private.fd)
            private.validate_file(descriptor);fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            before=os.fstat(descriptor)
            require(os.stat(name,dir_fd=private.fd,follow_symlinks=False)==before)
            yield
            private.verify();private.validate_file(descriptor)
            require(os.fstat(descriptor)==before and os.stat(name,dir_fd=private.fd,follow_symlinks=False)==before)
    except OSError:raise DevelopmentError('development_checkpoint_work_busy') from None
    finally:
        if descriptor is not None:os.close(descriptor)


def _worker(planner,request):
    from .development_authorization import DevelopmentAuthorization
    from .dev_planner import _directory_pin,_reasoning_configuration
    worker=request.worker;op=worker.journal.get(planner.parent_operation_id);worker._pin_controller(op)
    worker._local_control(op['id'])
    worker._pin_configuration(op,planner.broker.journal,planner.policy,planner.broker.test_backend)
    planner.policy.validate_for(planner.policy.executable,planner.policy.workspace)
    config,payload=op['configuration'],op['payload'];pins=config['pins']
    require(('reasoning_effort' in config)==('reasoning_effort' in planner.configuration)
        and ('reasoning_effort' not in config or config['reasoning_effort'] is not None))
    require(op['stage']=='running' and op['id']==planner.operation_id and op['scope']==planner.scope
        and config.get('kind')=='development'
        and config['manifest_hash']==planner.broker.journal.manifest_hash
        and config['policy_hash']==planner.configuration['policy_hash']==planner.policy.fingerprint
        and planner.configuration['policy_cli_hash']==planner.policy.cli_sha256
        and config['model']==planner.configuration['model'] and config['instruction']==planner.configuration['instruction']
        and _reasoning_configuration(config.get('reasoning_effort'))==_reasoning_configuration(planner.configuration.get('reasoning_effort'))
        and payload['planner_budgets']==planner.configuration['budgets'] and payload['context']==planner.configuration.get('context')
        and payload['claim_id']==planner.claim_id and all(_directory_pin(pin['path'])==pin for pin in pins.values()))
    from .dev_runtime import development_work_id
    parent_scope={'operation_id':op['id'],'journal':pins['journal'],
        'configuration_hash':digest(canonical({'configuration':config,'initial_context_hash':payload['initial_context']['fingerprint']}))}
    parent={'scope':op['scope'],'root_identity':planner.broker.manifest['root_identity'],'journal':pins['broker'],
        'configuration_hash':parent_scope['configuration_hash'],'parent_scope':parent_scope,
        'child_scope':{'work_id':development_work_id(op['id']),'runs':pins['runs'],'runtime':pins['runtime']}}
    require(payload['parent_binding']==parent and planner.configuration.get('parent_operation')==parent_scope
        and planner.binding.get('parent_scope')==parent_scope)
    require(all(pins[name]==planner.configuration['pins'][name] for name in ('broker','runs','runtime'))
        and pins['planner_workspace']==planner.configuration['pins']['planner'])
    auth=DevelopmentAuthorization(worker.client,scope=op['scope'],manifest=planner.broker.manifest,
        registration=payload['registration'],clock=worker.clock,
        stop_requested=lambda:worker._controller_cancelled(op['id']),
        local_stop_requested=lambda:worker._local_cancelled(op['id']),delegation_guard=worker.delegation_guard)
    auth.expected_context=deepcopy(payload['context']);auth.claim_version=payload['claim_version'];auth.claim_id=payload['claim_id']
    auth.snapshot(require_claim=True);grant=auth()
    planner._validate_grant(grant)
    return op


def _test_caps(planner,broker):
    manifest=planner.broker.manifest;backend=planner.broker.test_backend
    if broker['schema']==3:
        from .linux_test_backend import SupervisedLinuxNamedPythonBackend
        from .linux_test_protocol import linux_execution_backend_id
        require(type(backend) is SupervisedLinuxNamedPythonBackend)
        backend._pins()  # Existing static plan/source reads, never qualification.
        plan=backend.plan;plan_sha=digest(canonical(plan))
        require(plan_sha==broker['linux_execution_plan_sha256']
            and all(test['backend_id']==linux_execution_backend_id(plan_sha) for test in manifest['tests']))
        tests=[]
        for suite in manifest['tests']:
            script=plan['input_plan']['scripts'][suite['name']]
            require(all(script[key]==suite[key] for key in ('timeout_seconds','max_output_bytes')))
            tests.append({'name':suite['name'],'script_sha256':script['sha256'],
                'timeout_seconds':suite['timeout_seconds'],'max_output_bytes':suite['max_output_bytes']})
        return {'tests':sorted(tests,key=lambda row:row['name']),'imports':plan['imports'],'limits':plan['limits']}
    # This separate legacy profile never claims a Linux script or test pass.
    return {'tests':deepcopy(manifest['tests']),'profile':'legacy-named-test'}


def _finished(planner,record):
    return _finished_outcome(planner,record,'completed')


def _finished_outcome(planner,record,outcome):
    from .runtime_guard import RuntimeGuard
    require(outcome in ('completed','blocked'))
    require(type(record) is dict and record['phase']=='finished' and record['start_confirmed']==1
        and type(record['receipt']) is dict and record['receipt'].get('cleanup_confirmed') is True
        and record['receipt'].get('outcome')==outcome)
    validator=RuntimeGuard(planner.runtime_journal,planner.client,record['configuration']['scope']);validator.operation=record
    found=planner.client.call('lookup_execution',{'idempotency_key':record['key']})
    require(type(found) is dict and found.get('found') is True)
    receipt=validator.metadata(found.get('execution'));scope=planner.broker.manifest['scope']
    require(receipt==record['receipt'] and receipt['state']=='finished' and receipt['cleanup_confirmed'] is True
        and receipt['stop_requested'] is False and receipt['device_id']==scope['device_id']
        and receipt['profile_version']==scope['profile_version'])
    return digest(canonical(record))


def _projection(planner,request,broker):
    return _projection_common(planner,request,broker)


def _provider_projection(planner,request,broker,failure):
    from .development_provider_continuation import _ProviderFailure
    require(type(failure) is _ProviderFailure)
    return _projection_common(planner,request,broker,provider=failure)


def _projection_common(planner,request,broker,*,provider=None):
    from .dev_planner import parse_action
    from .dev_runtime import ToolRuntimeGuard,development_agent_id,development_work_id
    request.validate(planner);planner.journal.checkpoint_lock()
    state=planner.journal.read();manifest=planner.broker.manifest
    planner_raw=planner.journal.private.read('planner.json',2*1024*1024)
    broker_raw=planner.broker.journal.private.read('state.json',2*1024*1024)
    require(planner_raw==canonical(state) and broker_raw==canonical(broker)
        and digest(planner.broker.journal.private.read('manifest.json',128*1024))==planner.broker.journal.manifest_hash)
    require(state['review'] is None and planner.active_guard is None and not planner.journal.stopped())
    if provider is None:require(state['phase']=='ready' and state['step'] is None and state['history'])
    else:require(state['phase']=='model_intent' and state['step'] is not None)
    # Inspection keeps absolute counters/deadline intact, including exhaustion.
    planner._fresh(state,inspection=True);before_op=_worker(planner,request)
    require(all(row['phase']=='complete' for row in broker['operations'].values()))
    count=planner.store._db.execute('SELECT COUNT(*) FROM turns WHERE work_id=?',(planner.work['id'],)).fetchone()[0]
    require(0<count<=64)
    stored=planner.store.status(planner.work['id']);work=stored['work'];turns={row['id']:row for row in stored['turns']}
    history=state['history'];extra=0 if provider is None else 1
    require(len(turns)==len(history)+extra==state['used']['turns'] and len(history)==state['used']['actions']
        and len(turns)<=64 and work['status']==('open' if provider is None else 'needs_review')
        and work['id']==development_work_id(planner.parent_operation_id)
        and work['agent_id']==development_agent_id(planner.scope,planner.parent_operation_id)
        and all(work[key]==planner.scope[key] for key in ('owner_id','project_id','ticket_id'))
        and work['workspace']==planner.policy.workspace and work['executable']==planner.policy.executable
        and work['model']==planner.configuration['model'])
    current={path:row['sha256'] for path,row in manifest['files'].items()};expected_runtime=set();operations=set();runtime_hashes=[];model_hashes={}
    for item in history:
        require(type(item) is dict and set(item)=={'step_id','kind','model_turn_id','receipt_sha256'}
            and item['model_turn_id'] in turns and item['kind'] in {'read','patch','test'})
        turn=turns[item['model_turn_id']];result=planner.store.turn_result(turn['id'])
        require(turn['status']=='completed' and not turn['requires_review'] and not turn['cancel_requested']
            and turn['resolution'] is None and turn['provider_session_id']==work['provider_session_id']
            and turn['request_id']=='dev-model-'+item['step_id']
            and result.get('status')=='completed' and type(result.get('verification')) is dict
            and all(result['verification'].get(key) is True for key in ('tool_free','cleanup_confirmed','session_verified','turn_completed','exit_zero'))
            and result['verification'].get('authentication')=='chatgpt')
        model_hashes[turn['id']]=digest(canonical(result))
        action=parse_action(result.get('text'),manifest,planner.configuration['budgets']['max_response_bytes'])
        require(action['kind']==item['kind']);sha256(item['receipt_sha256'])
        model=planner.runtime_journal.find(planner.model_guard.configuration(work,turn['id']))
        runtime_hashes.append(_finished(planner,model));expected_runtime.add(model['id'])
        label=action['name'] if item['kind']=='test' else 'patch_file' if item['kind']=='patch' else 'read_file'
        guard=ToolRuntimeGuard(planner.runtime_journal,planner.client,planner.scope,action_label=label,operation_id=planner.parent_operation_id)
        tool=planner.runtime_journal.find(guard.configuration({**work,'model':label},item['step_id']))
        runtime_hashes.append(_finished(planner,tool));expected_runtime.add(tool['id'])
        if item['kind'] in {'patch','test'}:
            key=digest(('dev-action-'+item['step_id']).encode('ascii'));entry=broker['operations'].get(key)
            require(entry is not None and entry['kind']==item['kind'] and entry['phase']=='complete');operations.add(key)
            if item['kind']=='patch':
                wanted={'kind':'patch','path':action['path'],'before_sha256':action['expected_sha256'],
                    'after_sha256':digest(action['content'].encode()),'bytes':len(action['content'].encode())}
                require(entry['request_hash']==digest(canonical(wanted)) and current[action['path']]==action['expected_sha256']
                    and entry['receipt']['result'] in {'applied','unchanged','verified_after_restart'}
                    and digest(canonical(entry['receipt']))==item['receipt_sha256'])
                current[action['path']]=wanted['after_sha256']
            else:
                require(entry['detail']['name']==action['name'] and entry['receipt'].get('cleanup_confirmed') is True)
    if provider is not None:
        turn,result,record=provider.verify(planner,request,state,stored)
        require(turn['id'] not in model_hashes)
        model_hashes[turn['id']]=digest(canonical(result))
        runtime_hashes.append(_finished_outcome(planner,record,'blocked'));expected_runtime.add(record['id'])
    require(operations==set(broker['operations']) and len(expected_runtime)==2*len(history)+extra)
    rows=planner.runtime_journal.db.execute("SELECT id FROM executions WHERE json_extract(configuration,'$.work_id')=? LIMIT 130",(work['id'],)).fetchall()
    require(len(rows)<=128 and {row['id'] for row in rows}==expected_runtime)
    def source():
        files=RepositoryFiles(manifest['root'],manifest['root_identity']);signatures={}
        try:
            for path,expected in current.items():
                data,pin=files.read(path,manifest['budgets']['max_file_bytes']);require(digest(data)==expected);signatures[path]=pin
        finally:files.close()
        return signatures
    signatures=source()
    require(planner.journal.read()==state and _worker(planner,request)==before_op and not planner.journal.stopped())
    planner._fresh(state,inspection=True)
    tests=_test_caps(planner,broker);paths=sorted(current)
    require(source()==signatures and planner.journal.read()==state
        and decode(planner.broker.journal.private.read('state.json',2*1024*1024))==broker)
    caps={'paths':paths,'tests':tests,'actions':manifest['actions'],'resource':manifest['resource'],
        'broker_limits':manifest['budgets'],'planner_limits':planner.configuration['budgets']}
    metadata={'schema':'development-checkpoint-observation-v1','operation_id':planner.operation_id,'work_id':work['id'],
        **{key:getattr(request,key) for key in ('task_id','generation_id','cycle_id','attempt_id','original_public_base',
            'source_release_sha256','parent_checkpoint_sha256')},'scope':deepcopy(manifest['scope']),
        'resource':manifest['resource'],'root':manifest['root'],'root_identity':manifest['root_identity'],
        'current_base_commit':manifest['base_sha'],'manifest_sha256':planner.broker.journal.manifest_hash,
        'configuration_sha256':planner.journal.fingerprint,'planner_document_sha256':digest(planner_raw),
        'broker_document_sha256':digest(broker_raw),'worker_operation_sha256':digest(canonical(before_op)),
        'work_state_sha256':digest(canonical(stored)),'model_results_sha256':digest(canonical(model_hashes)),
        'runtime_cleanup_sha256':digest(canonical(sorted(runtime_hashes))),'selected_postimages':current,'selected_paths':paths,
        'source_snapshot_sha256':digest(canonical(current)),'test_caps_sha256':digest(canonical(tests)),
        'static_caps_sha256':digest(canonical(caps)),'used_planner':deepcopy(state['used']),'used_broker':deepcopy(broker['used']),
        'started_at':state['started_at'],'deadline':state['deadline'],'passed_tests_promoted':False}
    if provider is not None:
        metadata.update(schema='development-provider-checkpoint-observation-v1',failed_step_id=state['step']['id'],
            provider_receipt={'work_id':work['id'],'turn_id':provider.metadata['turn_id'],
                'receipt_sha256':provider.receipt_sha256},provider_result_sha256=provider.metadata['result_sha256'],
            provider_cause='unknown')
    return metadata


class DevelopmentCheckpointProof:
    def __init__(self,issuer,planner,request,metadata):
        require(issuer is _ISSUER);self.planner,self.request=planner,request
        self.metadata=deepcopy(metadata);self.pid,self.thread=os.getpid(),threading.current_thread();self._held=None
    def __copy__(self):raise TypeError('Live checkpoint proofs cannot be copied')
    def __deepcopy__(self,_):raise TypeError('Live checkpoint proofs cannot be copied')
    def __reduce_ex__(self,_):raise TypeError('Live checkpoint proofs cannot be serialized')
    def _observe(self,broker):return _projection(self.planner,self.request,broker)
    @contextmanager
    def hold(self):
        require(self.pid==os.getpid() and self.thread is threading.current_thread() and self._held is None)
        try:
            with self.planner.journal.lock():
                token=self.planner.journal.checkpoint_lock()
                with self.planner.broker.journal.lock() as broker,_read_work(self.planner.store,self.planner.work['id']):
                    lease=None
                    from .dev_journal import _linux_lock_supported
                    if _linux_lock_supported():lease=self.planner.broker.journal.held_lock_lease()
                    observation=self._observe(broker);require(observation==self.metadata)
                    self._held=(token,broker,observation,lease)
                    try:yield deepcopy(observation)
                    finally:self._held=None
        except DevelopmentError:raise
        except Exception:raise DevelopmentError('development_checkpoint_unconfirmed') from None
    def recheck(self,observation):
        require(self.pid==os.getpid() and self.thread is threading.current_thread() and self._held is not None)
        token,broker,expected,lease=self._held;self.planner.journal.checkpoint_lock(token)
        if lease is not None:self.planner.broker.journal.require_lock_lease(lease)
        require(observation==expected and self._observe(broker)==expected)
        return deepcopy(expected)


class DevelopmentProviderCheckpointProof(DevelopmentCheckpointProof):
    """Explicit source-only failed-turn boundary; never a ready/v1 proof."""
    @classmethod
    def _issue(cls,planner,request,metadata,failure):
        value=cls(_ISSUER,planner,request,metadata);value.failure=failure;return value
    def _observe(self,broker):return _provider_projection(self.planner,self.request,broker,self.failure)


def _checkpoint_ready(planner,request):
    planner.journal.checkpoint_lock()
    with planner.broker.journal.lock() as broker,_read_work(planner.store,planner.work['id']):
        metadata=_projection(planner,request,broker)
    proof=DevelopmentCheckpointProof(_ISSUER,planner,request,metadata)
    return DevelopmentCheckpointReady('checkpoint_ready',planner.operation_id,proof)
