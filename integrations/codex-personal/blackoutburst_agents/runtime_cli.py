"""Inspect runtime metadata or reconcile cleanup from an exact private result."""
from contextlib import ExitStack
import json
from pathlib import Path
import re

from .client_lifecycle import own_client
from .run_store import RunStore
from .runtime_client import RuntimeBoardClient
from .runtime_guard import RuntimeGuard, RuntimeControlError
from .dev_runtime import DevelopmentRuntimeGuard
from .runtime_journal import RuntimeJournal


def configure_parser(parser):
    parser.add_argument('--journal',required=True,type=Path)
    commands=parser.add_subparsers(dest='runtime_command',required=True)
    commands.add_parser('list',help='Lister les identifiants et états locaux, sans connexion.')
    status=commands.add_parser('status',help='Lire les métadonnées locales sans connexion.')
    status.add_argument('--operation',required=True)
    reconcile=commands.add_parser('reconcile',help='Rapprocher la fin exacte, sans réservation ni modèle.')
    reconcile.add_argument('--operation',required=True)
    reconcile.add_argument('--state',required=True,type=Path)
    reconcile.add_argument('--connection-file',required=True,type=Path)
    reconcile.add_argument('--development-operation',help='UUID parent explicite du travail de développement.')


def run(args):
    try:
        with ExitStack() as stack:
            journal=stack.enter_context(RuntimeJournal(args.journal))
            if args.runtime_command=='list':output={'executions':journal.list_status()}
            elif args.runtime_command=='status':output=journal.status(args.operation)
            else:
                operation=journal.get(args.operation)
                config=operation['configuration'];scope=config['scope']
                development_id=getattr(args,'development_operation',None)
                client_kind,guard_kind=RuntimeBoardClient,RuntimeGuard
                if scope.get('scope_kind')=='message':
                    if development_id is not None:
                        raise RuntimeControlError('runtime_development_scope_mismatch')
                    from .message_runtime import MessageRuntimeClient,MessageRuntimeGuard
                    client_kind,guard_kind=MessageRuntimeClient,MessageRuntimeGuard
                client=own_client(stack,client_kind.from_connection_file(args.connection_file,
                    session_id=scope['board_session_id'],timeout=3))
                store=RunStore(args.state);stack.callback(store.close)
                # Hold the model-work lock: a cached result cannot be reconciled
                # while a cooperating model process still owns this work.
                with store.work_lock(config['work_id']):
                    status=store.status(config['work_id'])
                    turn=next((t for t in status['turns'] if t['id']==config['turn_id']),None)
                    if turn is None or turn['status'] not in ('completed','blocked','cancelled'):
                        raise RuntimeControlError('runtime_turn_requires_review')
                    work=status['work']
                    binding=store.get_context_guard(config['work_id'])
                    if binding is not None:work={**work,'decision_memory':binding}
                    guard=(guard_kind(journal,client,scope) if development_id is None else
                        DevelopmentRuntimeGuard(journal,client,scope,operation_id=development_id))
                    guard.confirm_cached(work,config['turn_id'],store.turn_result(config['turn_id']))
                output=journal.status(operation['id'])
        print(json.dumps(output,ensure_ascii=False,indent=2));return 0
    except KeyboardInterrupt:
        print(json.dumps({'status':'needs_review','reason':'runtime_interrupted'}));return 130
    except Exception as error:
        code=getattr(error,'code','runtime_state_unavailable')
        if not isinstance(code,str) or not re.fullmatch('[a-z][a-z0-9_]{0,100}',code):code='runtime_state_unavailable'
        print(json.dumps({'status':'needs_review','reason':code}));return 2
