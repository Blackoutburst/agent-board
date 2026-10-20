"""One configured human reply; no ticket mutation or executable board command."""
import time
from .board_reader import BoardReadError
from .context import ContextError
from .host_config import HostError, digest
from .message_client import MAX_REPLY_BYTES
from .message_context import exact_source, prepare_context, registration_profile, revalidate_context
from .message_scope import local_agent, validate_scope
from .runner import PersonalRunner
from .runtime_guard import RuntimeControlError, RuntimeAdmissionRefused
from .message_runtime import MessageRuntimeGuard
from .run_store import StoreError


class MessageWorker:
    def __init__(self,journal,runs,client,*,runtime_guard_factory,runner_factory=PersonalRunner):
        if not callable(runtime_guard_factory):raise HostError('message_runtime_required')
        self.journal,self.runs,self.client=journal,runs,client
        self.runtime_guard_factory,self.runner_factory=runtime_guard_factory,runner_factory

    def _save(self,op,state,**values):
        return self.journal.update(op['id'],op['revision'],state,**{**op['payload'],**values})

    @staticmethod
    def receipt(op,status=None,reason=None):
        return {'status':status or ('complete' if op['state']=='complete' else 'needs_review' if op['state']=='needs_review' else 'pending'),
            'reason':reason or op['payload'].get('reason','message_pending'),'worker_operation_id':op['worker_id'],
            'message_operation_id':op['id']}

    def _fresh(self,op,target,owner,*,history=True):
        registration=self.client.register()
        registration_profile(registration,owner,target['agent_id'],target['board_session_id'],target['profile_version'])
        if op['state']=='new':exact_source(self.client,op)
        else:revalidate_context(self.client,registration,target,owner,op,history=history)
        return registration

    def run(self,operation_id,target,owner,*,should_cancel):
        op=self.journal.operation(operation_id)
        if not op['worker_id']:raise HostError('host_reference_conflict')
        if op['state'] in ('complete','needs_review'):return self.receipt(op)
        if op['state']=='ignored':raise HostError('host_reference_conflict')
        target_hash=digest(target)
        if op['payload'].get('configuration_sha256',target_hash)!=target_hash:raise HostError('message_configuration_conflict')
        if should_cancel():return self.receipt(op,'pending','host_stopped')
        try:
            registration=self._fresh(op,target,owner)
            if op['state']=='new':
                scope,prompt,context_hash,refs=prepare_context(self.client,registration,target,owner,op,target['instruction'])
                op=self._save(op,'prepared',scope=scope,prompt=prompt,context_sha256=context_hash,
                    context_refs=refs,
                    configuration_sha256=target_hash,reply_key='host-message-reply-'+digest({
                        'endpoint':self.client.endpoint,'owner_id':owner,'agent_id':target['agent_id'],
                        'thread_id':op['thread_id'],'message_id':op['message_id']}))
            scope=validate_scope(op['payload']['scope'])
            if op['state']=='prepared':
                if should_cancel():return self.receipt(op,'pending','host_stopped')
                work=self.runs.create_work(owner_id=owner,agent_id=local_agent(scope),
                    project_id=scope['thread_id'],ticket_id=scope['message_id'],work_id=op['work_id'],
                    workspace=target['workspace'],executable=target['executable'],model=target['model'])
                # A prepared operation cannot already contain any provider turn.
                if work['provider_session_id'] is not None or self.runs._db.execute(
                        'SELECT 1 FROM turns WHERE work_id=?',(work['id'],)).fetchone():
                    raise HostError('message_foreign_work')
                self._fresh(op,target,owner)
                op=self._save(op,'running')  # Durable before a turn can exist.
            if op['state']=='running':
                work=self.runs.get_work(op['work_id'])  # Missing state never means permission to recreate.
                if (work['agent_id']!=local_agent(scope) or work['owner_id']!=owner
                        or work['project_id']!=scope['thread_id'] or work['ticket_id']!=scope['message_id']
                        or work['model']!=target['model'] or work['workspace']!=target['workspace']
                        or work['executable']!=target['executable']):raise HostError('message_foreign_work')
                registration=self._fresh(op,target,owner)
                guard=self.runtime_guard_factory(scope)
                if not isinstance(guard,MessageRuntimeGuard):raise HostError('message_runtime_required')
                guard.admission_identity={'device_id':registration['identity']['device_id'],
                    'profile_version':registration['profile_version']}
                def before_transport():
                    if should_cancel():raise HostError('host_stopped')
                    self._fresh(op,target,owner)
                guard.before_transport=before_transport
                runner=self.runner_factory(self.runs,runtime_guard=guard)
                if getattr(runner,'runtime_guard',None) is not guard:raise HostError('message_runtime_required')
                last_context_check=time.monotonic();context_failed=False
                def cancelled():
                    nonlocal last_context_check,context_failed
                    if context_failed:return True
                    if should_cancel():return True
                    try:
                        if hasattr(self.client,'check_current'):self.client.check_current()
                        now=time.monotonic()
                        if now-last_context_check>=5:
                            # Root ACLs are checked during the transport; exact
                            # history is checked at both effect boundaries.
                            self._fresh(op,target,owner,history=False)
                            last_context_check=now
                    except Exception:
                        context_failed=True
                        return True
                    return False
                try:
                    result=runner.run(work['id'],'host-message-'+op['worker_id'],op['payload']['prompt'],
                        timeout=target['timeout'],should_cancel=cancelled)
                except RuntimeControlError as error:
                    if isinstance(error,RuntimeAdmissionRefused):
                        op=self._save(op,'needs_review',reason='message_runtime_capacity_exhausted')
                        return self.receipt(op)
                    if str(error) in ('message_context_changed','message_context_guard_required'):
                        op=self._save(op,'needs_review',reason='message_context_requires_review')
                        return self.receipt(op)
                    # A durable successful RunStore result can reconcile a lost
                    # finish reply on the next pass, without starting inference.
                    return self.receipt(op,'pending','message_runtime_reconciliation')
                if context_failed:
                    op=self._save(op,'needs_review',reason='message_context_requires_review')
                    return self.receipt(op)
                text=result.get('result',{}).get('text')
                if (result.get('result',{}).get('status')!='completed' or not isinstance(text,str)
                        or not text.strip() or '\0' in text or len(text.encode())>MAX_REPLY_BYTES):
                    op=self._save(op,'needs_review',reason='message_result_requires_review')
                    return self.receipt(op)
                op=self._save(op,'result_ready',body=text,body_sha256=digest(text),turn_id=result['turn_id'])
            if op['state']=='result_ready':
                self._fresh(op,target,owner)
                if should_cancel():return self.receipt(op,'pending','host_stopped')
                op=self._save(op,'posting')
            if op['state']=='posting':
                registration=self._fresh(op,target,owner)
                if should_cancel():return self.receipt(op,'pending','host_stopped')
                body=op['payload']['body']
                if digest(body)!=op['payload']['body_sha256']:raise HostError('message_journal_corrupt')
                posted=self.client.call('post_message',{'thread_id':op['thread_id'],'reply_to':op['message_id'],
                    'body':body,'idempotency_key':op['payload']['reply_key']})
                author=posted.get('author',{}) if isinstance(posted,dict) else {}
                if (not isinstance(posted,dict) or not isinstance(posted.get('id'),str)
                        or posted.get('thread_id')!=op['thread_id'] or posted.get('reply_to')!=op['message_id']
                        or posted.get('body')!=body or author.get('kind')!='agent' or author.get('owner_id')!=owner
                        or author.get('agent_id')!=target['agent_id'] or author.get('session_id')!=target['board_session_id']
                        or author.get('device_id')!=registration['identity']['device_id']):
                    raise HostError('message_publication_conflict')
                op=self._save(op,'complete',reply_id=posted['id'],reason='message_reply_confirmed')
            return self.receipt(op)
        except BoardReadError as error:
            if error.code in ('timeout','connection_failed','runtime_unavailable'):
                return self.receipt(op,'pending','board_unavailable')
            op=self._save(op,'needs_review',reason='message_authorization_or_source_changed')
            return self.receipt(op)
        except (HostError,StoreError,ContextError,ValueError,TypeError,KeyError):
            op=self._save(op,'needs_review',reason='message_state_requires_review')
            return self.receipt(op)
