"""Exact message identities, separate from ticket work and provider sessions."""
import hashlib
import json
import re
from .board_reader import _endpoint
from .host_config import HostError
from .message_journal import identifier

FIELDS = frozenset({'board_endpoint','board_session_id','owner_id','agent_id','scope_kind',
    'thread_id','message_id','source_sha256'})


def validate_scope(scope):
    if not isinstance(scope,dict) or set(scope)!=FIELDS or scope['scope_kind']!='message':
        raise HostError('message_scope_invalid')
    _endpoint(scope['board_endpoint'])
    for key in ('board_session_id','owner_id','agent_id','thread_id','message_id'):identifier(scope[key])
    if not isinstance(scope['source_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',scope['source_sha256']):
        raise HostError('message_scope_invalid')
    return dict(scope)


def local_agent(scope):
    value={'namespace':'board-message-v1',**validate_scope(scope)}
    return 'board-message-'+hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
        separators=(',',':')).encode()).hexdigest()
