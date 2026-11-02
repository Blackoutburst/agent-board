"""Explicit preview/apply commands; no polling or model usage."""
import json
import os
from pathlib import Path
import stat

from .board_reader import BoardReadError
from .dev_journal import decode
from .dev_manifest import DevelopmentError
from .team_ticket_client import TeamTicketBoardClient
from .team_ticket_plan import MAX_DOCUMENT_BYTES, TeamTicketPlanController


def read_document(path):
    descriptor = None
    try:
        descriptor = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_DOCUMENT_BYTES:
            raise DevelopmentError('team_ticket_document_invalid')
        chunks = []; size = 0
        while True:
            part = os.read(descriptor, min(8192, MAX_DOCUMENT_BYTES + 1 - size))
            if not part:
                break
            chunks.append(part); size += len(part)
            if size > MAX_DOCUMENT_BYTES:
                raise DevelopmentError('team_ticket_document_invalid')
        after = os.fstat(descriptor)
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise DevelopmentError('team_ticket_document_changed')
        return decode(b''.join(chunks))
    except OSError:
        raise DevelopmentError('team_ticket_document_unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def configure_parser(parser):
    commands = parser.add_subparsers(dest='team_plan_command', required=True)
    for name in ('preview', 'apply'):
        command = commands.add_parser(name, help='Afficher le plan vérifié.' if name == 'preview'
                                      else 'Créer explicitement ses tickets, sans lancer d’agent.')
        command.add_argument('--connection-file', type=Path, required=True)
        command.add_argument('--session', required=True, help='Session dédiée au contrôleur Lead sélectionné.')
        command.add_argument('--plan', type=Path, required=True)
        command.add_argument('--catalog', type=Path, required=True)
        if name == 'apply':
            command.add_argument('--journal', type=Path, required=True)
            command.add_argument('--approve-sha256', required=True, help='Empreinte exacte de l’aperçu accepté.')


def run(args):
    try:
        plan = read_document(args.plan); catalog = read_document(args.catalog)
        with TeamTicketBoardClient.from_connection_file(args.connection_file, session_id=args.session) as client:
            controller = TeamTicketPlanController(client, getattr(args, 'journal', None))
            if args.team_plan_command == 'preview':
                result = controller.preview(plan, catalog)
            else:
                result = controller.apply(plan, catalog, approved_sha256=args.approve_sha256)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (DevelopmentError, BoardReadError) as error:
        print(json.dumps({'status': 'needs_review', 'reason': error.code}))
        return 2
