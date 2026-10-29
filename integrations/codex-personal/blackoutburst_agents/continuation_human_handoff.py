"""Closed visibility for a failed actual generation, never execution authority.

The caller invokes this only at its failure boundary while the concrete handles
remain open. A confirmed question blocks/releases only the existing own claim
through the normal Board transaction. No answer is consumed or treated as a
grant, continuation permit, model input or permission to resume.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import os
from pathlib import Path
import re
import threading
import time

from .continuation import ContinuationTask
from .development_authorization import DevelopmentAuthorization
from .development_client import DevelopmentBoardClient, CONTINUATION_HUMAN_QUESTIONS, failure_notice
from .development_worker import DevelopmentBoardWorker
from .development_aggregate_delivery import _existing_record
from .dev_fence import DevelopmentFence, canonical_anchor
from .dev_journal import PrivateDirectory, DevelopmentJournal, decode
from .dev_manifest import DevelopmentError, RepositoryFiles, canonical, digest, validate_manifest
from .dev_planner import _directory_pin
from .dev_planner_journal import PlannerJournal
from .dev_runtime import DevelopmentRuntimeGuard, development_work_id
from .git_workspace import _operation
from .runtime_guard import RuntimeGuard

MAX_DOCUMENT=2*1024*1024


def require(value):
    if not value:raise DevelopmentError('continuation_human_handoff_unconfirmed')


class ContinuationHumanHandoff:
    """Inert construction; publish performs a single bounded visibility pass."""
    def __init__(self,worker,task,operation_id):
        require(type(worker) is DevelopmentBoardWorker and type(task) is ContinuationTask)
        self.worker,self.task,self.operation_id=worker,task,_operation(operation_id)
        self.pid,self.thread=os.getpid(),threading.current_thread()
        self.deadline=None

    def _budget(self):
        require(self.deadline is not None and time.monotonic()<self.deadline)

    def _context(self):
        require(self.pid==os.getpid() and self.thread is threading.current_thread())
        w=self.worker;op=w.journal.get(self.operation_id);w._pin_controller(op)
        require(op['configuration']['kind']=='development' and type(w.client) is DevelopmentBoardClient)
        require(w.client._registered is True and w.client._initialized is True and w.client._closed is False)
        scope=op['scope'];config=self.task.configuration
        require(w.client.endpoint==scope['board_endpoint'] and w.client.session_id==scope['board_session_id']
            and all(config['scope'][key]==scope[key] for key in ('owner_id','agent_id','project_id','ticket_id','board_endpoint')))
        for pin in op['configuration']['pins'].values():require(_directory_pin(pin['path'])==pin)
        ticket=op['payload']['initial_selected_ticket']
        require(ticket['id']==scope['ticket_id'] and ticket['project_id']==scope['project_id'])
        snapshot=self.task.snapshot();cycle=snapshot['state']['current_cycle']
        require(type(cycle) is dict)
        with PrivateDirectory(w.journal.directory) as private:
            intent=decode(private.read('development-checkpoint-intent-'+op['id']+'.json',128*1024))
        request=intent['request']
        require(intent['operation_id']==op['id'] and request['task_id']==config['logical_task_id']
            and request['source_release_sha256']==config['scope']['source_release_sha256']
            and all(request[key]==cycle[key] for key in ('generation_id','cycle_id','attempt_id')))
        return op,snapshot,ticket['thread_id']

    def _runtime(self,op):
        w=self.worker;work_id=development_work_id(op['id'])
        with w.store.work_lock(work_id):
            stored=w.store.status(work_id);work=stored['work']
            require(all(work[key]==op['scope'][key] for key in ('owner_id','project_id','ticket_id')))
            require(0<len(stored['turns'])<=128)
            model=DevelopmentRuntimeGuard(w.runtime,w.control,op['scope'],operation_id=op['id'])
            expected={digest(canonical(model.configuration(work,turn['id']))) for turn in stored['turns']}
            for turn in stored['turns']:
                result=w.store.turn_result(turn['id']);verification=result.get('verification',{})
                require(turn['status'] in ('completed','blocked','cancelled')
                    and verification.get('cleanup_confirmed') is True and verification.get('tool_free') is True)
            rows=w.runtime.list_status();require(0<len(rows)<=128)
            receipts=[];seen=set()
            for row in rows:
                self._budget()
                record=w.runtime.get(row['id']);configuration=record['configuration']
                require(configuration['scope']==op['scope'] and configuration['work_id']==work_id
                    and record['phase']=='finished' and type(record['receipt']) is dict
                    and record['receipt'].get('cleanup_confirmed') is True)
                if configuration['provider']=='codex_chatgpt':
                    fingerprint=digest(canonical(configuration))
                    require(fingerprint in expected and fingerprint not in seen);seen.add(fingerprint)
                guard=RuntimeGuard(w.runtime,w.control,op['scope']);guard.operation=record
                found=w.control.call('lookup_execution',{'idempotency_key':record['key']})
                require(type(found) is dict and found.get('found') is True)
                receipt=guard.metadata(found.get('execution'))
                require(receipt==record['receipt'] and receipt['state']=='finished' and receipt['cleanup_confirmed'] is True)
                receipts.append(receipt)
            require(seen==expected)
        return digest(canonical(receipts))

    @contextmanager
    def _effects(self,op):
        """Existing shared broker lock; no constructor, cleanup or recovery."""
        pin=op['configuration']['pins']['broker']
        with PrivateDirectory(pin['path']) as private:
            require(_directory_pin(pin['path'])==pin)
            fd=os.open('broker.lock',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=private.fd)
            try:
                private.validate_file(fd);fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
                manifest=validate_manifest(decode(private.read('manifest.json',128*1024)))
                broker=decode(private.read('state.json',MAX_DOCUMENT))
                planner=decode(private.read('planner.json',MAX_DOCUMENT))
                require(digest(canonical(manifest))==op['configuration']['manifest_hash'])
                scope=self.task.configuration['scope']
                require(manifest['resource']==scope['resource']
                    and manifest['scope']['device_id']==scope['device_id']
                    and manifest['scope']['profile_version']==scope['profile_version'])
                validator=object.__new__(DevelopmentJournal)
                validator._manifest,validator.manifest_hash=manifest,op['configuration']['manifest_hash']
                validator.validate_state(broker);DevelopmentJournal.require_settled(broker)
                validator=object.__new__(PlannerJournal);validator.operation_id=op['id']
                validator.configuration=planner['configuration'];validator.fingerprint=digest(canonical(validator.configuration))
                validator.validate(planner)
                require(planner['configuration']['scope']==op['scope']
                    and planner['configuration']['work_id']==development_work_id(op['id'])
                    and planner['configuration']['manifest_hash']==op['configuration']['manifest_hash'])
                step=planner['step']
                # A current accepted action that was not advanced to history
                # remains uncertain here, even if cleanup alone is positive.
                require(step is None or step['action'] is None)
                require(all(row['phase']=='broker_admitted' for row in broker.get('linux_execution',{}).values()))
                postimages={path:row['sha256'] for path,row in manifest['files'].items()};operations=set()
                for row in planner['history']:
                    if row['kind'] not in ('patch','test'):continue
                    key=digest(('dev-action-'+row['step_id']).encode('ascii'))
                    entry=broker['operations'].get(key)
                    require(entry is not None and entry['kind']==row['kind'] and key not in operations)
                    operations.add(key)
                    if row['kind']=='patch':
                        detail=entry['detail'];receipt=entry['receipt']
                        require(postimages[detail['path']]==detail['before_sha256']
                            and receipt['result'] in ('applied','unchanged','verified_after_restart')
                            and digest(canonical(receipt))==row['receipt_sha256'])
                        postimages[detail['path']]=detail['after_sha256']
                require(operations==set(broker['operations']))
                def source():
                    files=RepositoryFiles(manifest['root'],manifest['root_identity']);signatures={}
                    try:
                        for path,expected in postimages.items():
                            data,pin=files.read(path,manifest['budgets']['max_file_bytes'])
                            require(digest(data)==expected);signatures[path]=pin
                    finally:files.close()
                    return signatures
                signatures=source()
                expected={row['step_id']:row['kind'] for row in planner['history']}
                if step is not None and step['action'] is not None:expected[step['id']]=step['action']['kind']
                tools={}
                for row in self.worker.runtime.list_status():
                    config=self.worker.runtime.get(row['id'])['configuration']
                    if config['provider']=='codex_chatgpt':continue
                    require(config['turn_id'] in expected and config['turn_id'] not in tools)
                    kind=expected[config['turn_id']]
                    allowed={suite['name'] for suite in manifest['tests']} if kind=='test' else {'patch_file'} if kind=='patch' else {'read_file'}
                    require(config['model'] in allowed);tools[config['turn_id']]=kind
                require(set(tools)==set(expected))
                before=(canonical(manifest),canonical(broker),canonical(planner))
                anchor=canonical_anchor() if self.worker.test_anchor is None else Path(self.worker.test_anchor)
                with _existing_record(anchor,'fence.lock',shared=True) as fence:
                    value=DevelopmentFence._validate(object.__new__(DevelopmentFence),decode(fence.read('fence.json',MAX_DOCUMENT)))
                    binding=deepcopy(op['payload']['parent_binding'])
                    binding['configuration_hash']=digest(canonical(planner['configuration']))
                    key=DevelopmentFence.key(op['scope'],manifest['root_identity'])
                    expected={'operation_id':op['id'],'binding':binding}
                    require(value['records'].get(key)==expected)
                    def fresh():
                        private.verify();fence.verify()
                        require(before==tuple(canonical(decode(private.read(name,MAX_DOCUMENT)))
                            for name in ('manifest.json','state.json','planner.json')))
                        require(source()==signatures)
                        current=DevelopmentFence._validate(object.__new__(DevelopmentFence),decode(fence.read('fence.json',MAX_DOCUMENT)))
                        require(current['records'].get(key)==expected)
                    yield manifest,digest(b''.join(before)+canonical(signatures)),fresh
                    fresh()
            finally:os.close(fd)

    def _claim(self,op,manifest):
        self._budget()
        w=self.worker
        auth=DevelopmentAuthorization(w.client,scope=op['scope'],manifest=manifest,
            registration=op['payload']['registration'],clock=w.clock)
        # Asking a question needs work-request permission, not the missing
        # repository action/resource grant that the human may need to inspect.
        policy=w.client.call('get_work',{'project_id':op['scope']['project_id'],'limit':1})['policy']
        require(all(policy.get(key) is True for key in ('enabled','allow_read','allow_requests')))
        self._budget();response=w.client.call('get_ticket',{'ticket_id':op['scope']['ticket_id']});self._budget()
        project,ticket=response['project'],response['ticket']
        require(project['id']==op['scope']['project_id'] and op['scope']['owner_id'] in project['participants']
            and project.get('archived') is False and ticket['id']==op['scope']['ticket_id']
            and ticket['project_id']==project['id'] and ticket['thread_id']==op['payload']['initial_selected_ticket']['thread_id']
            and ticket.get('assignee_agent_id')==op['scope']['agent_id'] and auth.own_claim(ticket))
        return ticket['version']

    def _request(self,intent):
        return dict(ticket_id=intent['ticket_id'],owner_id=intent['owner_id'],
            question_code=intent['question_code'],idempotency_key=intent['idempotency_key'])

    def _question_receipt(self,op,intent,value):
        author={**{key:op['scope'][key] for key in ('owner_id','agent_id')},'kind':'agent',
            'session_id':op['scope']['board_session_id'],'device_id':op['payload']['registration']['identity']['device_id']}
        require(type(value) is dict and type(value.get('id')) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',value['id']) is not None
            and value.get('ticket_id')==intent['ticket_id'] and value.get('owner_id')==intent['owner_id']
            and value.get('question')==CONTINUATION_HUMAN_QUESTIONS[intent['question_code']]
            and value.get('kind')=='question' and value.get('status') in ('open','resolved')
            and type(value.get('requested_by')) is dict
            and all(value['requested_by'].get(key)==item for key,item in author.items()))
        return {'schema':'continuation-human-receipt-v1','operation_id':op['id'],
            'intent_sha256':digest(canonical(intent)),'kind':'question','request_id':value['id']}

    def _lookup(self,private,op,intent):
        self._budget()
        value=self.worker.client.lookup_continuation_human_input(**self._request(intent))
        if value=={'found':False}:return self._result('pending')
        receipt=self._question_receipt(op,intent,value)
        self._receipt(private,'continuation-human-receipt-'+op['id']+'.json',receipt)
        return self._result('question_confirmed',request_id=receipt['request_id'])

    @staticmethod
    def _receipt(private,name,value):
        try:private.create(name,canonical(value))
        except DevelopmentError as error:
            if error.code!='private_file_exists':raise
            require(private.read(name,16384)==canonical(value))

    def _result(self,status,**values):
        return {'schema':'continuation-human-visibility-v1','operation_id':self.operation_id,
            'status':status,'human_question_confirmed':status=='question_confirmed','launchable':False,**values}

    def _notice(self,private,op,thread,question_code,cleaned):
        self._budget()
        notice=failure_notice('development_grants_unavailable' if question_code=='permissions'
            else 'development_control_requires_review',cleaned)
        intent={'schema':1,'operation_id':op['id'],'session_id':op['scope']['board_session_id'],
            'ticket_id':op['scope']['ticket_id'],'project_id':op['scope']['project_id'],'thread_id':thread,
            'idempotency_key':'development-failure-notice-'+op['id'],'notice':notice}
        name='continuation-failure-notice-intent-'+op['id']+'.json'
        try:private.create(name,canonical(intent))
        except DevelopmentError as error:
            if error.code!='private_file_exists':raise
            # Do not automatically repeat an uncertain post or invent a second
            # key. Its absence of an ACK is not evidence of absence on Board.
            require(private.read(name,16384)==canonical(intent));return self._result('pending')
        try:
            receipt=self.worker.client.post_failure_notice(thread_id=thread,
                idempotency_key=intent['idempotency_key'],notice=notice)
            self._receipt(private,'continuation-failure-notice-receipt-'+op['id']+'.json',receipt)
            return self._result('notice_confirmed',message_id=receipt['message_id'])
        except Exception:return self._result('pending')

    def publish(self,*,question_code):
        require(type(question_code) is str and question_code in CONTINUATION_HUMAN_QUESTIONS)
        self.deadline=time.monotonic()+30
        try:return self._publish(question_code)
        except Exception:return self._result('pending')

    def _publish(self,question_code):
        op,snapshot,thread=self._context()
        with self.worker.journal.lock(op['scope']),PrivateDirectory(self.worker.journal.directory) as private:
            name='continuation-human-intent-'+op['id']+'.json'
            try:intent=decode(private.read(name,16384))
            except DevelopmentError as error:
                if error.code!='private_read_failed':return self._result('pending')
                try:os.stat(name,dir_fd=private.fd,follow_symlinks=False)
                except FileNotFoundError:intent=None
                else:return self._result('pending')
            if intent is not None:
                require(type(intent) is dict and set(intent)=={'schema','operation_id','task_configuration_sha256','state_sha256',
                    'effects_sha256','runtime_receipts_sha256','ticket_id','owner_id','question_code','idempotency_key','expected_version'}
                    and intent['schema']=='continuation-human-intent-v1'
                    and type(intent['expected_version']) is int and 1<=intent['expected_version']<2**63
                    and all(type(intent[key]) is str and re.fullmatch(r'[0-9a-f]{64}',intent[key]) is not None
                        for key in ('task_configuration_sha256','state_sha256','effects_sha256','runtime_receipts_sha256'))
                    and intent.get('operation_id')==op['id'] and intent.get('task_configuration_sha256')==digest(canonical(self.task.configuration))
                    and intent.get('ticket_id')==op['scope']['ticket_id'] and intent.get('owner_id')==op['scope']['owner_id']
                    and intent.get('question_code')==question_code and intent.get('idempotency_key')=='continuation-human-'+op['id'])
                try:return self._lookup(private,op,intent)
                except Exception:return self._result('pending')
            state=snapshot['state']
            if state['phase'] in ('ready','paused_cycle','paused_provider','paused_quota','stopped'):
                return self._result('not_applicable')
            cleaned=False
            try:
                require(state['phase']=='needs_you' and state['stop_requested'] is False)
                runtime_sha=self._runtime(op);cleaned=True
                with self._effects(op) as (manifest,effects_sha,fresh):
                    version=self._claim(op,manifest)
                    require(self.task.snapshot()==snapshot and self.worker.journal.get(op['id'])==op)
                    intent={'schema':'continuation-human-intent-v1','operation_id':op['id'],
                        'task_configuration_sha256':digest(canonical(self.task.configuration)),
                        'state_sha256':snapshot['state_sha256'],'effects_sha256':effects_sha,
                        'runtime_receipts_sha256':runtime_sha,
                        'ticket_id':op['scope']['ticket_id'],'owner_id':op['scope']['owner_id'],
                        'question_code':question_code,'idempotency_key':'continuation-human-'+op['id'],
                        'expected_version':version}
                    private.create(name,canonical(intent))
                    # Recheck actual terminal Runtime and current claim after
                    # durability, immediately before the one Board mutation.
                    require(self._runtime(op)==runtime_sha and self._claim(op,manifest)==version and self.task.snapshot()==snapshot)
                    try:
                        self._budget();fresh()
                        require(self.task.snapshot()==snapshot and self.worker.journal.get(op['id'])==op
                            and not self.worker._stopped(op['id']))
                        self._budget()
                        value=self.worker.client.request_continuation_human_input(**self._request(intent),expected_version=version)
                        receipt=self._question_receipt(op,intent,value)
                        self._receipt(private,'continuation-human-receipt-'+op['id']+'.json',receipt)
                        return self._result('question_confirmed',request_id=receipt['request_id'])
                    except Exception:
                        try:return self._lookup(private,op,intent)
                        except Exception:return self._result('pending')
            except Exception:
                # A request intent is an ambiguous handoff and is lookup-only;
                # do not also publish a second alarm or recover any claim.
                try:os.stat(name,dir_fd=private.fd,follow_symlinks=False)
                except FileNotFoundError:
                    current=self.task.snapshot()['state']
                    if current['phase'] in ('ready','paused_cycle','paused_provider','paused_quota','stopped'):
                        return self._result('not_applicable')
                    return self._notice(private,op,thread,question_code,cleaned)
                return self._result('pending')
