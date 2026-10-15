"""One explicit ticket command; no background listener or queue selection."""
from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import re
import signal
import stat
import threading
import uuid

from .board_reader import BoardReadError, _text
from .board_worker import BoardWorker
from .human_client import HumanWorkerClient
from .run_store import RunStore, StoreError
from .runner_cli import _write_private
from .subscription_probe import ProbeSettings, validate_executable
from .work_journal import JournalError, WorkJournal, STAGES
from .worker_client import WorkerBoardClient


def configure_parser(parser):
    parser.add_argument('--journal', required=True, type=Path, help='Journal privé des effets MCP.')
    parser.add_argument('--state', type=Path, help='État privé des sessions Codex ; requis pour run.')
    commands = parser.add_subparsers(dest='worker_command', required=True)
    run = commands.add_parser('run', help='Traiter un ticket choisi, puis soumettre le texte en revue.')
    run.add_argument('--connection-file', required=True, type=Path)
    run.add_argument('--session', required=True)
    for name in ('owner', 'agent', 'project', 'ticket'):
        run.add_argument('--' + name, required=True)
    run.add_argument('--request', required=True, help='Clé stable conservée lors des nouvelles tentatives.')
    run.add_argument('--instruction-file', required=True, type=Path)
    run.add_argument('--workspace', required=True, type=Path)
    run.add_argument('--codex', required=True, help='Chemin absolu du client Codex personnel.')
    run.add_argument('--model')
    run.add_argument('--human-owner', help='Destinataire fixe d’une éventuelle question ; reprise explicite après sa réponse.')
    run.add_argument('--timeout', type=float, default=120)
    status = commands.add_parser('status', help='Lire uniquement les métadonnées locales, sans connexion.')
    status.add_argument('--operation', required=True)
    inspect = commands.add_parser('inspect', help='Exporter le contexte et le résultat privés pour examen local.')
    inspect.add_argument('--operation', required=True)
    inspect.add_argument('--output', required=True, type=Path)
    abandon = commands.add_parser('abandon', help='Abandon local après réconciliation humaine des effets sur le board.')
    abandon.add_argument('--operation', required=True)
    abandon.add_argument('--revision', required=True, type=int)
    abandon.add_argument('--reason', required=True, help='Constat privé, sans secret, conservé dans le journal.')
    abandon.add_argument('--acknowledge-board-reconciled', required=True, action='store_true',
                         help='Confirmer avoir vérifié/réconcilié la réservation et la publication éventuelle sur le board.')


def _instruction(path):
    descriptor = None
    try:
        # An accidental FIFO/device must not block or read indefinitely.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError
        chunks, size = [], 0
        while size <= 12000:
            chunk = os.read(descriptor, min(4096, 12001 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        value = b''.join(chunks).decode('utf-8', 'strict')
        if not value.strip() or size > 12000 or '\x00' in value:
            raise ValueError
        return value
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _public_result(value):
    """Whitelist the worker's receipt, never forwarding model/source content."""
    if (not isinstance(value, dict) or value.get('status') not in ('complete', 'pending', 'needs_review', 'abandoned')
            or value.get('stage') not in STAGES or type(value.get('cached')) is not bool
            or not isinstance(value.get('reason'), str)
            or not re.fullmatch('[a-z][a-z0-9_]{0,100}', value['reason'])):
        raise ValueError
    output = {key: value[key] for key in ('status', 'stage', 'reason', 'cached')}
    if 'operation_id' in value:
        operation_id = value['operation_id']
        if not isinstance(operation_id, str) or str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError
        output['operation_id'] = operation_id
    if 'published_status' in value:
        if value['published_status'] not in ('review', 'blocked'):
            raise ValueError
        output['published_status'] = value['published_status']
    return output


def _close_work(store, operation):
    work_id = operation['payload'].get('work_id')
    if work_id is None:
        return
    with store.work_lock(work_id):
        work = store.get_work(work_id)
        scope, configuration = operation['scope'], operation['configuration']
        identity = json.dumps({key: scope[key] for key in ('board_endpoint', 'board_session_id', 'agent_id')},
                              ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        agent = 'board-agent-' + hashlib.sha256(identity.encode('utf-8')).hexdigest()
        if (any(work[key] != scope[key] for key in ('owner_id', 'project_id', 'ticket_id'))
                or work['agent_id'] != agent
                or any(work[key] != configuration.get(key) for key in ('workspace', 'executable', 'model'))):
            raise JournalError('linked_work_scope_mismatch')
        if work['status'] != 'closed':
            store.resolve_interruption(work_id, decision='abandon',
                                       reason=operation['payload']['abandonment_intent']['reason'])


def _abandon(args, journal):
    if (args.state is None or args.acknowledge_board_reconciled is not True
            or type(args.revision) is not int or args.revision < 1
            or not isinstance(args.reason, str) or not args.reason.strip() or len(args.reason) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in args.reason)):
        raise ValueError
    operation = journal.get(args.operation)
    with journal.lock(operation['scope']):
        operation = journal.get(args.operation)
        expected_intent = {'revision': args.revision, 'reason': args.reason,
                           'board_reconciled': True}
        intent = operation['payload'].get('abandonment_intent') if isinstance(operation['payload'], dict) else None
        if intent is not None:
            if (intent != expected_intent or operation['revision'] != args.revision + (
                    2 if operation['stage'] == 'abandoned' else 1)):
                raise JournalError('abandonment_conflict')
            if operation['stage'] == 'abandoned':
                return journal.status(operation['id'])
        else:
            if operation['revision'] != args.revision:
                raise JournalError('revision_conflict')
            if (not isinstance(operation['payload'], dict) or not (
                    operation['stage'] == 'needs_review'
                    or (operation['stage'] == 'new' and operation['payload'] == {}))):
                raise JournalError('invalid_stage_transition')
            operation = journal.update(operation['id'], operation['revision'], stage='needs_review',
                                       payload={**operation['payload'], 'abandonment_intent': expected_intent})
        if operation['stage'] != 'needs_review':
            raise JournalError('invalid_stage_transition')
        if operation['payload'].get('work_id') is not None:
            store = RunStore(args.state)
            try:
                _close_work(store, operation)
            finally:
                store.close()
        journal.abandon(operation['id'], operation['revision'], args.reason)
        return journal.status(operation['id'])


def run(args):
    try:
        if args.worker_command in ('status', 'inspect', 'abandon'):
            with WorkJournal(args.journal) as journal:
                if args.worker_command == 'inspect':
                    operation = journal.get(args.operation)
                    _write_private(args.output, json.dumps(operation, ensure_ascii=False, indent=2) + '\n')
                    output = {'status': 'exported', 'operation_id': operation['id'], 'revision': operation['revision']}
                elif args.worker_command == 'abandon':
                    output = _abandon(args, journal)
                else:
                    output = journal.status(args.operation)
            exit_code = 0
        else:
            if (args.state is None or not math.isfinite(args.timeout) or not 1 <= args.timeout <= 300):
                raise ValueError
            if args.human_owner is not None:
                if not _text(args.human_owner, 256):
                    raise ValueError
                args.human_owner.encode('utf-8', 'strict')
            instruction = _instruction(args.instruction_file)
            executable = validate_executable(args.codex)
            ProbeSettings(executable=executable, model=args.model, timeout_seconds=args.timeout)
            workspace = args.workspace.expanduser().resolve(strict=True)
            if not workspace.is_dir():
                raise ValueError
            client_class = HumanWorkerClient if args.human_owner is not None else WorkerBoardClient
            client = client_class.from_connection_file(args.connection_file, session_id=args.session)
            scope = {'owner_id': args.owner, 'agent_id': args.agent, 'project_id': args.project,
                     'ticket_id': args.ticket, 'board_session_id': args.session, 'board_endpoint': client.endpoint}
            with WorkJournal(args.journal) as journal:
                store = RunStore(args.state)
                previous = {}
                cancelled = threading.Event()
                try:
                    for signum in (signal.SIGINT, signal.SIGTERM):
                        previous[signum] = signal.signal(signum, lambda *_: cancelled.set())
                    result = BoardWorker(journal, store, client).run(scope=scope,
                        request_id=args.request, instruction=instruction, workspace=workspace,
                        executable=executable, model=args.model, timeout=args.timeout,
                        should_cancel=cancelled.is_set, human_owner_id=args.human_owner)
                    output = _public_result(result)
                finally:
                    for signum, handler in previous.items():
                        signal.signal(signum, handler)
                    store.close()
            exit_code = 0 if output['status'] == 'complete' else 2
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return exit_code
    except (JournalError, StoreError, BoardReadError) as error:
        print(json.dumps({'status': 'blocked', 'reason': error.code}))
        return 2
    except (OSError, ValueError, TypeError):
        print(json.dumps({'status': 'blocked', 'reason': 'invalid_worker_configuration_or_local_io'}))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({'status': 'blocked', 'reason': 'worker_interrupted'}))
        return 130
    except Exception:
        # Unexpected failures must not turn private context into a traceback.
        print(json.dumps({'status': 'blocked', 'reason': 'worker_failed'}))
        return 2
