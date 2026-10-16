"""Explicit opt-in host controls. No service or listener is installed."""
from pathlib import Path
import json
import signal
import threading

from .host_config import HostError, load
from .host_journal import HostJournal
from .host_supervisor import HostSupervisor


def configure_parser(parser):
    parser.add_argument('--journal', required=True, type=Path)
    commands = parser.add_subparsers(dest='host_command',required=True)
    configure = commands.add_parser('configure',help='Épingler une configuration privée ; désactivé par défaut.')
    configure.add_argument('--config',required=True,type=Path)
    enable = commands.add_parser('enable',help='Autoriser le budget fini configuré, sans démarrer le processus.')
    enable.add_argument('--revision',required=True,type=int)
    reconcile = commands.add_parser('reconcile',help='Rapprocher un lot déjà résolu dans les journaux enfants, hors connexion.')
    reconcile.add_argument('--batch',required=True)
    commands.add_parser('status',help='Métadonnées locales uniquement.')
    commands.add_parser('stop',help='Désactiver durablement puis annuler coopérativement le travail actif.')
    commands.add_parser('tick',help='Un passage borné ; au plus un lot ou message déjà choisi.')
    commands.add_parser('run',help='Boucle explicite au premier plan, sans service installé.')


def run(args):
    previous, cancelled = {}, threading.Event()
    try:
        with HostJournal(args.journal) as journal:
            if args.host_command == 'configure':
                output = journal.configure(load(args.config))
            elif args.host_command == 'status': output = journal.status()
            elif args.host_command == 'stop': output = journal.stop()
            else:
                with journal.owner_lock():
                    if args.host_command == 'enable': output = journal.enable(args.revision)
                    elif args.host_command == 'reconcile': output = HostSupervisor(journal).reconcile(args.batch)
                    else:
                        for signum in (signal.SIGINT,signal.SIGTERM):
                            def stop(*_):
                                # Set the callback immediately; persist before
                                # any subsequent admission or runner iteration.
                                cancelled.set()
                                try: journal.stop()
                                except Exception: pass
                            previous[signum] = signal.signal(signum,stop)
                        supervisor = HostSupervisor(journal)
                        operation = supervisor.tick if args.host_command == 'tick' else supervisor.run
                        output = operation(should_cancel=cancelled.is_set)
        print(json.dumps(output,ensure_ascii=False,indent=2))
        return 2 if output['status'] in ('blocked','needs_review') else 0
    except HostError as error:
        print(json.dumps({'status':'blocked','reason':error.code}))
        return 2
    except Exception:
        print(json.dumps({'status':'blocked','reason':'host_unavailable'}))
        return 2
    finally:
        for signum,handler in previous.items(): signal.signal(signum,handler)
