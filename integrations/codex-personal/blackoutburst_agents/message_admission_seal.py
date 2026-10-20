"""Explicit closed message-admission seal client. Never called by a worker loop."""
import copy
import hashlib
import json
import re

from .board_reader import BoardReadError
from .runtime_client import RuntimeBoardClient

METHOD = 'seal_message_execution_admission'
SCHEMA = 'message-runtime-admission-seal-v1'
FIELDS = frozenset({'idempotency_key','thread_id','message_id','source_sha256','work_id','turn_id','provider','model'})
PROOF_FIELDS = frozenset({'schema','reason','request_sha256','admission_request_sha256',
    'owner_id','agent_id','device_id','profile_version','session_id'})


def _identifier(value, *, model=False):
    pattern=r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}' if model else r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}'
    if type(value) is not str or re.fullmatch(pattern,value) is None:
        raise ValueError('runtime_seal_invalid')


def wire_arguments(arguments, session_id):
    if type(arguments) is not dict or set(arguments) != FIELDS:
        raise ValueError('runtime_seal_invalid')
    _identifier(session_id)
    for key,value in arguments.items():
        if key == 'source_sha256':
            if type(value) is not str or re.fullmatch('[0-9a-f]{64}',value) is None:
                raise ValueError('runtime_seal_invalid')
        elif key in ('provider','model'):
            _identifier(value,model=True)
        else:
            _identifier(value)
    return {**copy.deepcopy(arguments),'session_id':session_id}


def request_sha256(method, arguments):
    if method not in (METHOD,'reserve_message_execution'):
        raise ValueError('runtime_seal_invalid')
    raw=json.dumps({'method':method,'arguments':arguments},ensure_ascii=False,
        sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    if len(raw)>8192:raise ValueError('runtime_seal_invalid')
    return hashlib.sha256(raw).hexdigest()


def validate_seal(value, arguments, *, owner_id, agent_id, device_id, profile_version):
    if type(arguments) is not dict or set(arguments) != FIELDS|{'session_id'}:
        raise ValueError('runtime_seal_invalid')
    wire=wire_arguments({k:v for k,v in arguments.items() if k!='session_id'},arguments['session_id'])
    for identifier in (owner_id,agent_id,device_id):_identifier(identifier)
    if type(profile_version) is not int or not 1<=profile_version<2**63:
        raise ValueError('runtime_seal_invalid')
    if (type(value) is not dict or set(value)!={'reserved','refusal'} or value['reserved'] is not False
            or type(value['refusal']) is not dict or set(value['refusal'])!=PROOF_FIELDS):
        raise ValueError('runtime_seal_invalid')
    expected={'schema':SCHEMA,'reason':'explicit_pretransport_abandonment',
        'request_sha256':request_sha256(METHOD,wire),
        'admission_request_sha256':request_sha256('reserve_message_execution',wire),
        'owner_id':owner_id,'agent_id':agent_id,'device_id':device_id,
        'profile_version':profile_version,'session_id':wire['session_id']}
    # Exact scalar types distinguish profile_version=True from integer 1,
    # without serializing arbitrary response values into another large string.
    if any(type(value['refusal'][key]) is not type(item) or value['refusal'][key]!=item
           for key,item in expected.items()):
        raise ValueError('runtime_seal_invalid')
    return dict(expected)


class MessageAdmissionSealClient(RuntimeBoardClient):
    """Original registered owner/device/session only; no register or normal work tools."""
    def seal(self, arguments, *, owner_id, agent_id, device_id, profile_version):
        try:
            wire=wire_arguments(arguments,self.session_id)
            # Validate caller-pinned identity before any RPC.
            for identifier in (owner_id,agent_id,device_id):_identifier(identifier)
            if type(profile_version) is not int or not 1<=profile_version<2**63:
                raise ValueError('runtime_seal_invalid')
        except (ValueError,TypeError,KeyError):
            raise BoardReadError('runtime_seal_invalid') from None
        self._initialize()
        value=self._tool(METHOD,wire)
        try:return validate_seal(value,wire,owner_id=owner_id,agent_id=agent_id,
            device_id=device_id,profile_version=profile_version)
        except (ValueError,TypeError,KeyError):
            raise BoardReadError('runtime_seal_invalid') from None

    def call(self,name,args):
        # The explicit seal API requires pinned identity, not a generic tool call.
        raise BoardReadError('tool_not_allowed')
