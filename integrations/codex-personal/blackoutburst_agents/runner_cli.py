"""Human-invoked commands for one personal installation."""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import signal
import tempfile
import threading

from .run_store import RunStore, StoreError
from .runner import PersonalRunner, public_result
from .subscription_probe import ProbeSettings, validate_executable


def configure_parser(parser):
    parser.add_argument('--state', required=True, type=Path,
                        help='Dossier privé local de l’exécutant (créé en mode 0700).')
    commands = parser.add_subparsers(dest='runner_command', required=True)
    create = commands.add_parser('create', help='Créer un contexte local, sans appeler de modèle.')
    for name in ('owner', 'agent', 'project', 'ticket'):
        create.add_argument('--' + name, required=True)
    create.add_argument('--workspace', required=True, type=Path)
    create.add_argument('--codex')
    create.add_argument('--model')
    turn = commands.add_parser('turn', help='Exécuter ou reprendre un tour explicitement demandé.')
    turn.add_argument('--work', required=True)
    turn.add_argument('--request', required=True, help='Clé stable de cette entrée, réutilisée lors d’une nouvelle tentative.')
    turn.add_argument('--prompt-file', required=True, type=Path)
    turn.add_argument('--timeout', type=float, default=120)
    turn.add_argument('--output-text', type=Path, help='Écrire explicitement la réponse privée dans ce fichier.')
    turn.add_argument('--context-state', type=Path)
    turn.add_argument('--decision-memory',type=Path,help='Magasin privé exact requis si le contexte inspecté épingle une mémoire.')
    turn.add_argument('--context-key')
    turn.add_argument('--context-fingerprint')
    turn.add_argument('--board-connection', type=Path)
    turn.add_argument('--board-session')
    for command in ('status', 'cancel', 'resolve'):
        child = commands.add_parser(command)
        child.add_argument('--work', required=True)
        if command == 'resolve':
            child.add_argument('--decision', required=True, choices=('continue', 'abandon'))
            child.add_argument('--reason', required=True, help='Constat humain après examen de l’interruption.')


def _write_private(path: Path, value: str):
    path = path.expanduser().absolute()
    if path.is_symlink() or not path.parent.is_dir():
        raise ValueError('invalid_output_path')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.response-', delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(args):
    from .board_reader import BoardReadError
    from .context import ContextError,ContextScope
    from .context_cache import ContextCache, ContextCacheError
    from .context_cli import verified_context, render_prompt
    store = None
    memory_resources=ExitStack()
    memory=None
    artifact=None
    try:
        store = RunStore(args.state)
        if args.runner_command == 'create':
            executable = args.codex or shutil.which('codex')
            if not executable:
                raise ValueError('codex_not_installed')
            executable = validate_executable(executable)
            ProbeSettings(executable=executable, model=args.model)
            workspace = args.workspace.expanduser().resolve(strict=True)
            if not workspace.is_dir():
                raise ValueError('invalid_workspace')
            work = store.create_work(owner_id=args.owner, agent_id=args.agent,
                project_id=args.project, ticket_id=args.ticket, workspace=str(workspace),
                executable=str(executable), model=args.model)
            output = {'work_id': work['id'], 'status': work['status']}
        elif args.runner_command == 'status':
            output = store.status(args.work)
        elif args.runner_command == 'cancel':
            output = {'work_id': args.work, 'cancel_requested': store.request_cancel(args.work)}
        elif args.runner_command == 'resolve':
            with store.work_lock(args.work):
                work = store.resolve_interruption(args.work, decision=args.decision, reason=args.reason)
            output = {'work_id': args.work, 'status': work['status']}
        else:
            if not 1 <= args.timeout <= 300:
                raise ValueError('invalid_timeout')
            with args.prompt_file.open('rb') as stream:
                raw = stream.read(65537)
            if not raw or len(raw) > 65536:
                raise ValueError('invalid_prompt_size')
            prompt = raw.decode('utf-8', 'strict')
            if args.decision_memory is not None:
                from .decision_memory import DecisionMemory
                memory=memory_resources.enter_context(DecisionMemory(args.decision_memory,create=False))
            if memory is not None and args.context_state is None:
                raise ContextCacheError('incomplete_context_arguments')
            context_args = (args.context_state, args.context_key, args.context_fingerprint,
                            args.board_connection, args.board_session)
            if any(v is not None for v in context_args):
                if not all(v is not None for v in context_args):
                    raise ContextCacheError('incomplete_context_arguments')
                work = store.get_work(args.work)
                scope = {k: work[k] for k in ('owner_id', 'agent_id', 'project_id', 'ticket_id')}
                with ContextCache(args.context_state) as cache:
                    artifact = verified_context(cache, args.context_key, args.context_fingerprint,
                                                args.board_connection, args.board_session, scope,
                                                **({'decision_memory':memory} if memory is not None else {}))
                if memory is not None:
                    memory.bind_work(ContextScope(**artifact['scope']),artifact['sources']['decision_memory'],work)
                prompt = render_prompt(artifact, prompt)
            cancelled = threading.Event()
            previous = {}
            # Signals only request cancellation; the transport stops its own
            # child process group. No command signals a saved arbitrary PID.
            try:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous[signum] = signal.signal(signum, lambda *_: cancelled.set())
                def should_cancel():
                    if cancelled.is_set():return True
                    if memory is not None:
                        try:
                            memory.require_descriptor(ContextScope(**artifact['scope']),artifact['sources']['decision_memory'])
                        except Exception:return True
                    return False
                value = PersonalRunner(store).run(args.work, args.request, prompt,
                    timeout=args.timeout, should_cancel=should_cancel if memory is not None else cancelled.is_set,
                    **({'context_guard':artifact['sources']['decision_memory']} if memory is not None else {}))
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
            output = public_result(value)
            if args.output_text and value['result'].get('status') == 'completed':
                _write_private(args.output_text, value['result']['text'])
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0 if output['status'] == 'completed' else 2
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (StoreError, ContextCacheError, ContextError, BoardReadError) as error:
        print(json.dumps({'status': 'blocked', 'reason': error.code}))
        return 2
    except (OSError, ValueError):
        print(json.dumps({'status': 'blocked', 'reason': 'invalid_configuration_or_local_io'}))
        return 2
    finally:
        memory_resources.close()
        if store is not None:
            store.close()
