"""Explicit discovery/admission and bounded continuation ticks; no listener."""
import json
from pathlib import Path
import signal
import threading

from .board_reader import BoardReadError
from .dev_journal import decode
from .dev_manifest import DevelopmentError
from .development_pool_selection import file_document
from .development_queue import DevelopmentQueueAdmission
from .development_queue_client import DevelopmentQueueDiscoveryClient
from .finite_pool_batch import MAX_BATCH, child
from .pool_journal import PoolError


def configure_parser(parser):
    commands = parser.add_subparsers(dest='development_queue_command', required=True)
    for name in ('preview', 'admit','tick'):
        command = commands.add_parser(name, help='Inspecter une sélection sans claim.' if name == 'preview'
                                      else 'Une passe de continuation explicitement approuvée.' if name=='tick'
                                      else 'Écrire une admission approuvée, sans démarrer de modèle.')
        command.add_argument('--root', type=Path, required=True)
        command.add_argument('--catalog', type=Path, required=True)
        command.add_argument('--session', required=True)
        if name in ('preview','tick'):
            command.add_argument('--workflow',type=Path,required=name=='tick',help='Workflow opérateur approuvé pour chaque entrée du catalogue.')
            command.add_argument('--checkpoint-after-actions',type=int,default=3)
            command.add_argument('--provider-backoff-seconds',type=int,default=60)
        if name in ('admit','tick'):
            command.add_argument('--approve-sha256', required=True)
        if name == 'admit':
            command.add_argument('--request-id', required=True, help='Nouvel UUID conservé pour cette demande.')
        if name=='tick':
            for option in ('executable','cli-version','interpreter','unshare','gh-executable'):
                command.add_argument('--'+option,required=True)
            command.add_argument('--max-seconds',type=int,default=7200,help='Durée de cette passe, pas une limite globale de tâche.')


def run(args):
    try:
        catalog = child(str(args.catalog.absolute()), args.root.absolute() / 'config')
        raw, _ = file_document(catalog, MAX_BATCH)
        document = decode(raw)
        if type(document) is not dict or type(document.get('connection_file')) is not str:
            raise DevelopmentError('development_queue_invalid')
        child(document['connection_file'], args.root.absolute() / 'connections')
        # Resolve the paired client only from the operator-selected catalog;
        # credentials are parsed locally and never included in output.
        with DevelopmentQueueDiscoveryClient.from_connection_file(document['connection_file'], session_id=args.session) as client:
            controller = DevelopmentQueueAdmission(args.root, client)
            if args.development_queue_command == 'preview':
                if args.workflow is None:result = controller.preview(args.catalog)
                else:
                    plan=controller.tick_plan(args.catalog,str(args.workflow.absolute()),
                        checkpoint_after_actions=args.checkpoint_after_actions,
                        provider_backoff_seconds=args.provider_backoff_seconds)
                    result={key:plan[key] for key in ('schema','approval_sha256','binding','activated','launchable')}
            elif args.development_queue_command=='admit':
                result = controller.admit(args.catalog, request_id=args.request_id,
                                          approved_sha256=args.approve_sha256)
            else:
                stop=threading.Event()
                handlers={number:signal.signal(number,lambda *_:stop.set()) for number in (signal.SIGTERM,signal.SIGINT)}
                try:
                    result=controller.tick(args.catalog,str(args.workflow.absolute()),approved_sha256=args.approve_sha256,
                        **{key:getattr(args,key) for key in ('executable','cli_version','interpreter','unshare',
                            'gh_executable','max_seconds','checkpoint_after_actions','provider_backoff_seconds')},
                        should_stop=stop.is_set)
                finally:
                    for number,handler in handlers.items():signal.signal(number,handler)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 3 if result.get('phase')=='needs_you' else 0
    except (DevelopmentError, BoardReadError) as error:
        print(json.dumps({'status': 'needs_review', 'reason': error.code}))
        return 2
    except PoolError:
        print(json.dumps({'status':'needs_review','reason':'development_queue_tick_invalid'}))
        return 2
