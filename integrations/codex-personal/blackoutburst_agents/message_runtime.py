"""Same Runtime capacity and cleanup protocol, exact message authorization."""
import copy
import re
from .board_reader import BoardReadError
from .runtime_client import RuntimeBoardClient, _identifier
from .runtime_guard import RuntimeGuard, RuntimeControlError
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
        return self._tool(name,{**copy.deepcopy(args),'session_id':self.session_id})


class MessageRuntimeGuard(RuntimeGuard):
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
