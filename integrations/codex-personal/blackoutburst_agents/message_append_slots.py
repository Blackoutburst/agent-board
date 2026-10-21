"""Read an existing owner-prepared slot; never provision or copy a credential.

Only an opaque Board slot id selects the fixed private file. Instructions,
paths and transport pins come from that private preparation. The resulting
catalogue membership says nothing about provider authentication or model access.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import stat

from .board_reader import _connection_file
from .host_config import HostError, connection_digest, validate
from .message_append_protocol import slot as validate_slot


def _fail():
    raise HostError('messages_append_prepared_slot_unconfirmed')


def _private(path,base,*,directory=False):
    path,base=Path(path).absolute(),Path(base).absolute()
    if '..' in path.parts or not path.is_relative_to(base):_fail()
    for node in (path,*path.parents):
        if not node.is_relative_to(base):break
        try:info=node.lstat()
        except OSError:_fail()
        isdir=node!=path or directory
        if (info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=(0o700 if isdir else 0o600)
                or not (stat.S_ISDIR(info.st_mode) if isdir else stat.S_ISREG(info.st_mode))
                or (not isdir and info.st_nlink!=1)):_fail()
    return path


class PreparedMessageSlots:
    def __init__(self,owner_home,*,configuration_guard):
        if not callable(configuration_guard):_fail()
        self.base=Path(owner_home).absolute()
        self.configuration_guard=configuration_guard

    def candidate(self,record,configuration):
        body,slot=record['body'],record['slot'];validate_slot(slot)
        if (slot['slot_id']!=body['slot_id'] or slot['profile_id']!=body['profile_id']
                or slot['profile_version']!=body['profile_version']):_fail()
        # No filesystem path supplied by the Board enters this selection.
        path=self.base/'.config/blackoutburst-agent-host/message-slots'/(slot['slot_id']+'.json')
        _private(path,self.base)
        fd=None
        try:
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            before=os.fstat(fd)
            if not 1<=before.st_size<=65536:_fail()
            chunks=[];length=0
            while True:
                part=os.read(fd,min(8192,65537-length))
                if not part:break
                chunks.append(part);length+=len(part)
                if length>65536:_fail()
            after=os.fstat(fd);named=path.stat(follow_symlinks=False)
            fields=('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
            if any(getattr(before,k)!=getattr(after,k) or getattr(before,k)!=getattr(named,k) for k in fields):_fail()
            def pairs(items):
                value={}
                for key,item in items:
                    if key in value:_fail()
                    value[key]=item
                return value
            prepared=json.loads(b''.join(chunks).decode('utf-8'),object_pairs_hook=pairs,
                parse_constant=lambda _:_fail())
        except (OSError,ValueError,UnicodeError,TypeError,RecursionError):_fail()
        finally:
            if fd is not None:os.close(fd)
        if (type(prepared) is not dict or set(prepared)!={'schema','slot','profile_sha256','target'}
                or prepared['schema']!='owner-prepared-message-slot-v1' or prepared['slot']!=slot
                or prepared['profile_sha256']!=body['profile_sha256']):_fail()
        target=prepared['target']
        if (type(target) is not dict or target.get('agent_id')!=body['profile_id']
                or target.get('profile_version')!=body['profile_version']
                or target.get('board_session_id')!=slot['session_id']
                or target.get('model')!=body['requested_model'] or target.get('start_at')!='now'):_fail()
        candidate=deepcopy(configuration)
        candidate['targets'].append(deepcopy(target))
        for key,root in (('workspace',self.base/'workspaces'),
                         ('connection_file',self.base/'.config/blackoutburst-board')):
            value=Path(target.get(key,''))
            if not value.is_absolute() or value==root or not value.is_relative_to(root):_fail()
            _private(value,self.base,directory=key=='workspace')
        connection=_connection_file(target['connection_file'])
        if (connection['mcp_url']!=configuration['board_endpoint']
                or connection['client']!=slot['agent_kind'] or connection['identity_key']!=slot['identity_key']
                or connection_digest(target['connection_file'])!=target.get('connection_sha256')):_fail()
        normalized=validate(candidate,bind_connections=True)
        if normalized!=candidate:_fail()
        if self.configuration_guard(candidate) is not None:_fail()
        _private(path,self.base)
        return candidate
