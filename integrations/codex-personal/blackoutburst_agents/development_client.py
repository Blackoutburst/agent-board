"""Pinned explicit-development MCP channel; no queue or implicit registration."""
import copy
import os
import math
from pathlib import Path
from .board_reader import BoardReadError, _connection_data, _json, _text, MAX_CONNECTION_BYTES
from .dev_manifest import absolute_directory, identity, signature
from .dev_journal import PrivateDirectory
from .worker_client import WorkerBoardClient, _arguments


class DevelopmentBoardClient(WorkerBoardClient):
    """Only prepare may register once. Reopened sessions retain their pins."""
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
        return super().register()

    def register(self):
        raise BoardReadError('development_implicit_registration_forbidden')

    def adopt_registration(self):
        # The operation has already pinned registration identity/version. A
        # changed profile must fail server authorization, never refresh here.
        self._registered = True

    def call(self, name, args):
        if not self._registered:
            raise BoardReadError('development_registration_required')
        if name == 'get_work':
            if (type(args) is not dict or set(args) != {'project_id', 'limit'}
                    or not _text(args['project_id'],200) or args['limit'] != 1 or type(args['limit']) is not int):
                raise BoardReadError('invalid_arguments')
            values = copy.deepcopy(args)
        elif name in ('get_ticket', 'get_thread', 'claim_ticket', 'renew_ticket', 'update_ticket'):
            values = _arguments(name, args)
        else:
            raise BoardReadError('development_tool_not_allowed')
        self._initialize()
        return self._tool(name, {**values, 'session_id': self.session_id})
