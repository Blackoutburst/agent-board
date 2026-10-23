"""Pinned explicit-development MCP channel; no queue or implicit registration."""
import copy
import os
import math
import re
from pathlib import Path
from .board_reader import BoardReadError, _connection_data, _json, _text, MAX_CONNECTION_BYTES
from .dev_manifest import absolute_directory, canonical, identity, signature
from .dev_journal import PrivateDirectory
from .worker_client import WorkerBoardClient, _arguments


class DevelopmentBoardClient(WorkerBoardClient):
    """Only prepare may register once. Reopened sessions retain their pins."""
    development_ticket_frame = True
    @classmethod
    def from_connection_file(cls, path, *, session_id, timeout=3):
        # A connection file must have no alias writable through a checkout.
        # Reuse the established credential parser without exposing its content.
        if (not _text(session_id,200) or type(timeout) not in (int,float)
                or not math.isfinite(timeout) or not 0<timeout<=60):
            raise BoardReadError('invalid_session_or_timeout')
        path=Path(path).absolute()
        parent=absolute_directory(path.parent)
        descriptor=None
        try:
            descriptor=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            PrivateDirectory.validate_file(descriptor)
            before=os.fstat(descriptor)
            if before.st_size>MAX_CONNECTION_BYTES:
                raise BoardReadError('connection_too_large')
            chunks=[];total=0
            while True:
                chunk=os.read(descriptor,min(8192,MAX_CONNECTION_BYTES+1-total))
                if not chunk:break
                total+=len(chunk)
                if total>MAX_CONNECTION_BYTES:raise BoardReadError('connection_too_large')
                chunks.append(chunk)
            config=_connection_data(_json(b''.join(chunks),'invalid_connection'))
            current=os.stat(path.name,dir_fd=parent,follow_symlinks=False)
            if signature(before)!=signature(os.fstat(descriptor)) or signature(before)!=signature(current):
                raise BoardReadError('development_connection_changed')
            value=cls(config,session_id,timeout)
            value.connection_path=str(path)
            value.connection_identity=identity(before)
            return value
        except OSError:
            raise BoardReadError('development_connection_unavailable') from None
        finally:
            if descriptor is not None:os.close(descriptor)
            os.close(parent)

    def register_initial(self):
        if self._registered:
            raise BoardReadError('development_registration_already_pinned')
        value=super().register()
        if self.development_ticket_frame:self.require_development_ticket_frame()
        return value

    def register(self):
        raise BoardReadError('development_implicit_registration_forbidden')

    def adopt_registration(self):
        # The operation has already pinned registration identity/version. A
        # changed profile must fail server authorization, never refresh here.
        self._registered = True
        if self.development_ticket_frame:self.require_development_ticket_frame()

    def require_development_ticket_frame(self):
        """Explicit support check before preparation/model; never fallback."""
        self._development_ticket_frame_ready=False
        if not self._registered:raise BoardReadError('development_registration_required')
        self._initialize()
        result=self._rpc('tools/list',{})
        try:
            tools=result['tools']
            if type(tools) is not list or len(tools)>256:raise ValueError
            matches=[tool for tool in tools if type(tool) is dict
                     and tool.get('name')=='get_development_ticket_frame']
            if len(matches)!=1:raise ValueError
            schema=matches[0]['inputSchema']
            if (type(schema) is not dict or set(schema['properties'])!={'session_id','ticket_id'}
                    or set(schema['required'])!=set(schema['properties'])
                    or schema.get('additionalProperties') is not False
                    or any(schema['properties'][key].get('type')!='string'
                           for key in ('session_id','ticket_id'))):raise ValueError
        except (KeyError,TypeError,ValueError):
            raise BoardReadError('development_ticket_frame_unavailable') from None
        self._development_ticket_frame_ready=True

    def get_development_ticket_frame(self, *, ticket_id):
        if (not self._registered or not getattr(self,'_development_ticket_frame_ready',False)
                or any(type(value) is not str or not re.fullmatch(
                    r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}',value) for value in (self.session_id,ticket_id))):
            raise BoardReadError('development_ticket_frame_unavailable')
        self._initialize()
        return self._tool('get_development_ticket_frame',dict(session_id=self.session_id,ticket_id=ticket_id))

    def call(self, name, args):
        if not self._registered:
            raise BoardReadError('development_registration_required')
        if name == 'get_work':
            if (type(args) is not dict or set(args) != {'project_id', 'limit'}
                    or not _text(args['project_id'],200) or args['limit'] != 1 or type(args['limit']) is not int):
                raise BoardReadError('invalid_arguments')
            values = copy.deepcopy(args)
        elif name == 'release_ticket':
            # Assignment maintenance only: no arbitrary release/status API is
            # added to the ordinary worker channel.
            if (type(args) is not dict or set(args) != {'ticket_id','expected_version',
                    'idempotency_key','reason','status','unassign_self'}
                    or type(args['ticket_id']) is not str or not _text(args['ticket_id'],200)
                    or type(args['idempotency_key']) is not str or not _text(args['idempotency_key'],200)
                    or type(args['expected_version']) is not int or not 1 <= args['expected_version'] < 2**63
                    or type(args['reason']) is not str or not args['reason'].strip()
                    or len(args['reason']) > 20000 or '\x00' in args['reason']
                    or type(args['status']) is not str or args['status'] != 'review' or args['unassign_self'] is not True):
                raise BoardReadError('invalid_arguments')
            try:
                values = copy.deepcopy(args)
                # Validate encoding/envelope bound before touching transport.
                if len(canonical(values)) > MAX_CONNECTION_BYTES-1024: raise ValueError
            except Exception:
                raise BoardReadError('invalid_arguments') from None
        elif name in ('get_ticket', 'get_thread', 'claim_ticket', 'renew_ticket', 'update_ticket'):
            values = _arguments(name, args)
        else:
            raise BoardReadError('development_tool_not_allowed')
        self._initialize()
        return self._tool(name, {**values, 'session_id': self.session_id})
