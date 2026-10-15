"""Drain one explicitly selected ticket's events, then exit."""
from __future__ import annotations

from contextlib import ExitStack
import json
import math
from pathlib import Path
import re
import signal
import threading

from .board_worker import BoardWorker
from .event_client import EventBoardClient, HumanEventBoardClient
from .board_reader import _text
from .event_drain import EventDrainer
from .event_journal import EventJournal, EventJournalError
from .run_store import RunStore
from .subscription_probe import ProbeSettings, validate_executable
from .work_journal import WorkJournal
from .worker_cli import _instruction


STATUSES = frozenset({'idle', 'pending', 'more_available', 'waiting', 'complete',
                      'needs_review', 'blocked', 'cancelled'})


def configure_parser(parser):
    parser.add_argument('--journal', required=True, type=Path, help='Journal privé des événements et lots.')
    parser.add_argument('--work-journal', type=Path, help='Journal privé du worker ; requis avec --execute.')
    parser.add_argument('--state', type=Path, help='Sessions Codex privées ; requis avec --execute.')
    commands = parser.add_subparsers(dest='events_command', required=True)
    drain = commands.add_parser('drain', help='Lire les événements d’un ticket puis quitter, sans listener.')
    drain.add_argument('--connection-file', required=True, type=Path)
    drain.add_argument('--session', required=True)
    for name in ('owner', 'agent', 'project', 'ticket'):
        drain.add_argument('--' + name, required=True)
    drain.add_argument('--instruction-file', required=True, type=Path)
    drain.add_argument('--workspace', required=True, type=Path)
    drain.add_argument('--codex', required=True)
    drain.add_argument('--model')
    drain.add_argument('--timeout', type=float, default=120)
    drain.add_argument('--after', type=int, default=0, help='Position initiale explicite, immuable pour cette portée.')
    drain.add_argument('--limit', type=int, default=50)
    drain.add_argument('--max-pages', type=int, default=4)
    drain.add_argument('--execute', action='store_true', help='Autoriser un tour pour un lot éligible, puis publier en revue.')
    drain.add_argument('--human-owner', help='Autoriser une question au participant fixé et une reprise explicite après sa réponse.')
    status = commands.add_parser('status', help='Lire les métadonnées d’un suivi local.')
    status.add_argument('--subscription', required=True)
    reconcile = commands.add_parser('reconcile', help='Clore un lot après achèvement ou abandon local explicite du worker.')
    reconcile.add_argument('--batch', required=True)
    reconcile.add_argument('--operation', required=True)
    reconcile.add_argument('--connection-file', type=Path,
        help='Rapprocher en lecture seule la question distante si son accusé a été perdu ; utilise la session d’origine.')


def _public_result(value):
    if (not isinstance(value, dict) or value.get('status') not in STATUSES
            or not isinstance(value.get('reason'), str)
            or not re.fullmatch('[a-z][a-z0-9_]{0,100}', value['reason'])):
        raise ValueError
    result = {key: value[key] for key in ('status', 'reason')}
    for key in ('after', 'ingested_count', 'pages'):
        if type(value.get(key)) is not int or not 0 <= value[key] < 2 ** 63:
            raise ValueError
        result[key] = value[key]
    for key in ('subscription_id', 'batch_id', 'worker_operation_id'):
        if key in value and value[key] is not None:
            if not isinstance(value[key], str) or not re.fullmatch('[A-Za-z0-9_-]{1,256}', value[key]):
                raise ValueError
            result[key] = value[key]
    return result


def run(args):
    try:
        if args.events_command in ('status', 'reconcile'):
            with EventJournal(args.journal) as journal:
                if args.events_command == 'status':
                    output = journal.status(args.subscription)
                else:
                    if args.work_journal is None:
                        raise ValueError
                    batch = journal.get_batch(args.batch)
                    subscription = journal.get_subscription(batch['subscription_id'])
                    with journal.lock(subscription['scope']):
                        with WorkJournal(args.work_journal) as work:
                            operation = work.get(args.operation)
                            with work.lock(operation['scope']):
                                operation = work.get(args.operation)
                                if args.connection_file is not None:
                                    previous = batch.get('receipt') or {}
                                    if (operation.get('scope') != subscription['scope']
                                            or operation.get('request_id') != batch.get('request_id')
                                            or json.dumps(operation.get('configuration'), sort_keys=True, allow_nan=False)
                                                != json.dumps(subscription.get('configuration'), sort_keys=True, allow_nan=False)
                                            or previous.get('operation_id', operation.get('id')) != operation.get('id')):
                                        raise EventJournalError('resolution_mismatch')
                                    scope = operation['scope']
                                    client = HumanEventBoardClient.from_connection_file(args.connection_file,
                                        session_id=scope['board_session_id'])
                                    if client.endpoint != scope['board_endpoint']:
                                        raise ValueError
                                    request = BoardWorker(work, None, client).lookup_human_question(operation)
                                    if request is None:
                                        raise EventJournalError('human_request_unresolved')
                                    batch = journal.resolve_batch(args.batch, operation, verified_human_request=request)
                                else:
                                    batch = journal.resolve_batch(args.batch, operation)
                    output = {'status': 'reconciled', 'batch_id': batch['id'],
                              'worker_operation_id': operation['id'], 'decision': operation['stage']}
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
        if (not math.isfinite(args.timeout) or not 1 <= args.timeout <= 300
                or not 1 <= args.limit <= 200 or not 1 <= args.max_pages <= 20
                or not 0 <= args.after < 2 ** 63
                or (args.execute and (args.state is None or args.work_journal is None))):
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
        client_class = HumanEventBoardClient if args.human_owner is not None else EventBoardClient
        client = client_class.from_connection_file(args.connection_file, session_id=args.session)
        scope = {'owner_id': args.owner, 'agent_id': args.agent, 'project_id': args.project,
                 'ticket_id': args.ticket, 'board_session_id': args.session, 'board_endpoint': client.endpoint}
        with ExitStack() as stack:
            journal = stack.enter_context(EventJournal(args.journal))
            worker = None
            if args.execute:
                work_journal = stack.enter_context(WorkJournal(args.work_journal))
                store = RunStore(args.state)
                stack.callback(store.close)
                worker = BoardWorker(work_journal, store, client)
            cancelled, previous = threading.Event(), {}
            try:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous[signum] = signal.signal(signum, lambda *_: cancelled.set())
                value = EventDrainer(journal, worker, client).drain(scope=scope, instruction=instruction,
                    workspace=workspace, executable=executable, model=args.model, timeout=args.timeout,
                    after=args.after, max_pages=args.max_pages, limit=args.limit, execute=args.execute,
                    should_cancel=cancelled.is_set, human_owner_id=args.human_owner)
                output = _public_result(value)
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0 if output['status'] in ('idle', 'complete', 'pending', 'more_available', 'waiting') else 2
    except KeyboardInterrupt:
        print(json.dumps({'status': 'cancelled', 'reason': 'drain_interrupted'}))
        return 130
    except EventJournalError as error:
        reason = 'human_request_unresolved' if error.code == 'human_request_unresolved' else 'invalid_event_configuration_or_io'
        print(json.dumps({'status': 'needs_review' if reason == 'human_request_unresolved' else 'blocked', 'reason': reason}))
        return 2
    except Exception:
        # The journal/configuration and any remote diagnostics stay private.
        print(json.dumps({'status': 'blocked', 'reason': 'invalid_event_configuration_or_io'}))
        return 2
