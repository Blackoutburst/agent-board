"""Explicit discovery/admission, without dispatch or a listener."""
import json
from pathlib import Path

from .board_reader import BoardReadError
from .dev_journal import decode
from .dev_manifest import DevelopmentError
from .development_pool_selection import file_document
from .development_queue import DevelopmentQueueAdmission
from .development_queue_client import DevelopmentQueueDiscoveryClient
from .finite_pool_batch import MAX_BATCH, child


def configure_parser(parser):
    commands = parser.add_subparsers(dest='development_queue_command', required=True)
    for name in ('preview', 'admit'):
        command = commands.add_parser(name, help='Inspecter une sélection sans claim.' if name == 'preview'
                                      else 'Écrire une admission approuvée, sans démarrer de modèle.')
        command.add_argument('--root', type=Path, required=True)
        command.add_argument('--catalog', type=Path, required=True)
        command.add_argument('--session', required=True)
        if name == 'admit':
            command.add_argument('--request-id', required=True, help='Nouvel UUID conservé pour cette demande.')
            command.add_argument('--approve-sha256', required=True)


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
                result = controller.preview(args.catalog)
            else:
                result = controller.admit(args.catalog, request_id=args.request_id,
                                          approved_sha256=args.approve_sha256)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (DevelopmentError, BoardReadError) as error:
        print(json.dumps({'status': 'needs_review', 'reason': error.code}))
        return 2
