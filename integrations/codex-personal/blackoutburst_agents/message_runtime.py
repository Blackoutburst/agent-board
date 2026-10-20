"""Same Runtime capacity and cleanup protocol, exact message authorization."""
import copy
import re
from .board_reader import BoardReadError
from .runtime_client import RuntimeBoardClient, _identifier
from .runtime_guard import RuntimeGuard, RuntimeControlError, RuntimeAdmissionRefused
from .runtime_admission import REFUSAL_MODE, validate_refusal
from .runtime_journal import RuntimeJournal
from .message_scope import validate_scope, local_agent


class MessageRuntimeClient(RuntimeBoardClient):
    def call(self,name,args):
        if name!='reserve_message_execution':return super().call(name,args)
        fields={'idempotency_key','thread_id','message_id','source_sha256','work_id','turn_id','provider','model'}
        if not isinstance(args,dict) or set(args)!=fields:raise BoardReadError('invalid_arguments')
        for key,value in args.items():
            if key=='source_sha256':
                if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):raise BoardReadError('invalid_arguments')
            elif key=='model':
                if not isinstance(value,str) or not value.strip() or len(value)>200 or any(ord(c)<32 or ord(c)==127 for c in value):
                    raise BoardReadError('invalid_arguments')
            else:_identifier(value)
        self._initialize()
        arguments={**copy.deepcopy(args),'session_id':self.session_id,'refusal_mode':REFUSAL_MODE}
        result=self._tool(name,arguments)
        if type(result) is dict and result.get('reserved') is False:
            try:validate_refusal(result,arguments)
            except (ValueError,TypeError,KeyError):raise BoardReadError('runtime_refusal_invalid') from None
        return result


class MessageRuntimeGuard(RuntimeGuard):
    # Set by the message worker for a new admission. Cached cleanup intentionally
    # needs no context read, because it never sends a prompt or starts inference.
    before_transport=None
    admission_identity=None

    def reject_admission(self,result):
        if type(result) is dict and result.get('reserved') is False:
            config=self.operation['configuration']
            from .runtime_admission import refusal_arguments
            identity=self.admission_identity
            if (type(identity) is not dict or set(identity)!={'device_id','profile_version'}
                    or type(identity['device_id']) is not str or type(identity['profile_version']) is not int):
                raise RuntimeControlError('runtime_refusal_identity_unconfirmed')
            proof=validate_refusal(result,refusal_arguments(config,self.operation['key']),
                owner_id=self.scope['owner_id'],agent_id=self.scope['agent_id'],**identity)
            self.save('not_admitted',proof)
            raise RuntimeAdmissionRefused('runtime_capacity_exhausted')

    def before(self,work,turn_id):
        if not callable(self.before_transport):raise RuntimeControlError('message_context_guard_required')
        super().before(work,turn_id)
        try:
            self.before_transport()
        except Exception:
            # Admission succeeded but no transport was invoked. Close this exact
            # slot with the same private blocked result PersonalRunner records.
            super().finish(work,turn_id,{'status':'blocked','reason':'runtime_start_requires_review',
                'text':'','uncertain':True,'verification':{'cleanup_confirmed':True}})
            raise RuntimeControlError('message_context_changed') from None

    def configuration(self,work,turn_id):
        scope=validate_scope(self.scope)
        if (work.get('owner_id')!=scope['owner_id'] or work.get('agent_id')!=local_agent(scope)
                or work.get('project_id')!=scope['thread_id'] or work.get('ticket_id')!=scope['message_id']
                or work.get('decision_memory') is not None):
            raise RuntimeControlError('runtime_scope_mismatch')
        return RuntimeJournal.configuration(scope,work['id'],turn_id,work['model'])

    def expected(self,config):
        return {**{key:config['scope'][key] for key in ('owner_id','agent_id','scope_kind','thread_id','message_id','source_sha256')},
            'session_id':config['scope']['board_session_id'],
            **{key:config[key] for key in ('work_id','turn_id','provider','model')}}

    def reservation(self,config):
        return 'reserve_message_execution',{'idempotency_key':self.operation['key'],
            **{key:config['scope'][key] for key in ('thread_id','message_id','source_sha256')},
            **{key:config[key] for key in ('work_id','turn_id','provider','model')}}
