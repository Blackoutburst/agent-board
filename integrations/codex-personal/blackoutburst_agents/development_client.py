"""Pinned explicit-development MCP channel; no queue or implicit registration."""
import copy
import hashlib
import os
import math
import re
from pathlib import Path
from .board_reader import BoardReadError, _connection_data, _json, _text, MAX_CONNECTION_BYTES
from .dev_manifest import absolute_directory, canonical, identity, signature
from .dev_journal import PrivateDirectory
from .worker_client import WorkerBoardClient, _arguments


FAILURE_NOTICE_CODES = frozenset({'development_run_requires_review',
    'development_cleanup_requires_review',
    'development_control_requires_review', 'development_context_changed',
    'development_grants_unavailable', 'development_claim_lost',
    'development_stop_requested', 'development_planner_scope_mismatch',
    'planner_control_requires_review', 'planner_runtime_stopped',
    'planner_turns_budget_exhausted', 'planner_actions_budget_exhausted',
    'planner_bytes_budget_exhausted',
    'supervisor_authorization_expired', 'permission_revoked',
    # Public worker/planner/action codes only; arbitrary text keeps the fallback.
    'invalid_document', 'duplicate_document_key', 'invalid_planner_action',
    'planner_response_too_large', 'action_not_allowed', 'path_not_allowed',
    'invalid_patch', 'file_too_large', 'test_not_allowed',
    'invalid_final_summary', 'planner_model_requires_review', 'planner_policy_changed',
    'planner_reasoning_effort_changed', 'planner_authorization_changed', 'planner_controller_stopped',
    'planner_stop_requested', 'planner_runtime_scope_changed', 'planner_prompt_too_large',
    'planner_wall_budget_exhausted', 'planner_tool_finish_unconfirmed', 'planner_reconciliation_required',
    'planner_directory_changed', 'planner_clock_changed', 'planner_disabled',
    'final_test_proof_missing', 'final_test_receipt_changed', 'planner_model_receipt_unavailable',
    'planner_tool_receipt_unavailable', 'planner_effect_requires_review', 'planner_recovered_effect_requires_review',
    'operations_budget_exhausted', 'read_bytes_budget_exhausted',
    'patch_bytes_budget_exhausted', 'tests_budget_exhausted',
})


def failure_notice(code, cleanup_confirmed=False):
    """Closed telemetry only; never retain an exception or arbitrary reason."""
    return {'schema':1,'phase':'worker_run',
        'code':code if type(code) is str and code in FAILURE_NOTICE_CODES else 'development_run_requires_review',
        'cleanup_state':'confirmed' if cleanup_confirmed is True else 'unknown'}


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
        if self._closed is not False:
            raise BoardReadError('connection_closed')
        self._registered = True
        # Tool support is a per-client capability observation, not permission.
        # Every actual frame remains fresh; reopening a client observes support
        # again. Explicit require_* still refreshes and invalidates on failure.
        if self.development_ticket_frame and not (
                self._initialized is True and getattr(self,'_development_ticket_frame_ready',False) is True):
            self.require_development_ticket_frame()

    def require_development_ticket_frame(self):
        """Explicit support check before preparation/model; never fallback."""
        self._development_ticket_frame_ready=False
        if self._closed is not False:raise BoardReadError('connection_closed')
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

    def post_failure_notice(self, *, thread_id, idempotency_key, notice):
        """One closed notice using the already initialized session, never register.

        This method is not part of call() or the planner's tool channel. The
        normal server checks the current profile's message permission. No claim,
        Runtime operation, registration, retry or permission fallback is added.
        """
        if type(notice) is not dict:raise BoardReadError('invalid_failure_notice')
        notice=dict(notice)  # Validate and serialize one snapshot, never a caller's mutable dict.
        if (self._closed is not False or self._registered is not True or self._initialized is not True
                or type(thread_id) is not str or not _text(thread_id,200)
                or type(idempotency_key) is not str or re.fullmatch(
                    r'development-failure-notice-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',idempotency_key) is None
                or type(notice) is not dict or set(notice)!={'schema','phase','code','cleanup_state'}
                or type(notice['schema']) is not int or notice['schema']!=1
                or type(notice['phase']) is not str or notice['phase']!='worker_run' or type(notice['code']) is not str
                or notice['code'] not in FAILURE_NOTICE_CODES
                or type(notice['cleanup_state']) is not str or notice['cleanup_state'] not in ('confirmed','unknown')):
            raise BoardReadError('invalid_failure_notice')
        body='Cloud execution needs review.\n'+canonical(notice).decode('utf-8')
        value=self._tool('post_message',dict(session_id=self.session_id,thread_id=thread_id,
            body=body,idempotency_key=idempotency_key,reply_to=None,mentions=[],attachment_ids=[],view='full'))
        if (type(value) is not dict or type(value.get('id')) is not str or not _text(value['id'],200)
                or value.get('thread_id')!=thread_id or value.get('body')!=body
                or type(value.get('author')) is not dict or value['author'].get('kind')!='agent'
                or value['author'].get('session_id')!=self.session_id):
            raise BoardReadError('failure_notice_unconfirmed')
        return {'message_id':value['id'],'thread_id':thread_id,
            'body_sha256':hashlib.sha256(body.encode('utf-8')).hexdigest()}

    def release_checkpoint_claim(self, *, ticket_id, expected_version, idempotency_key, reason):
        """Controller-only claim maintenance after proved checkpoint cleanup.

        The transport is not cleanup proof. The cycle controller must verify
        the actual stopped operation and fresh Runtime receipts before calling
        it. This entry is deliberately outside the planner's call channel.
        """
        if (self._closed is not False or self._registered is not True
                or type(ticket_id) is not str or not _text(ticket_id,200)
                or type(expected_version) is not int or not 1<=expected_version<2**63
                or type(idempotency_key) is not str or re.fullmatch(
                    r'development-checkpoint-release-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
                    idempotency_key) is None
                or type(reason) is not str or not reason.strip() or len(reason)>20000 or '\x00' in reason):
            raise BoardReadError('invalid_arguments')
        values=dict(ticket_id=ticket_id,expected_version=expected_version,idempotency_key=idempotency_key,
                    reason=reason,status='todo',unassign_self=False,session_id=self.session_id)
        if len(canonical(values))>MAX_CONNECTION_BYTES-1024:raise BoardReadError('invalid_arguments')
        self._initialize()
        return self._tool('release_ticket',values)

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
