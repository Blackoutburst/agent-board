#!/usr/bin/env python3
"""Restricted entry point for one installed owner. No login, pairing or auto-enable."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import pwd
import stat
import sys
import signal
import threading

if __package__:
    from .package import (OWNER_NAMES, PackageError, absolute, home, no_symlinks,
                          protected, read_regular, verify_release, manifest_id)
else:
    # Installed here by root. -I does not put a script's directory on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from package import (OWNER_NAMES, PackageError, absolute, home, no_symlinks,
                         protected, read_regular, verify_release, manifest_id)


def owner_context(manifest, username):
    if username not in OWNER_NAMES or os.geteuid() == 0:
        raise PackageError('owner_login_required')
    account = pwd.getpwnam(username)
    if (account.pw_uid != os.geteuid() or account.pw_dir != str(home(username))
            or account.pw_gid != os.getegid() or any(group != account.pw_gid for group in os.getgroups())):
        raise PackageError('unix_owner_mismatch')
    if platform.system() != 'Linux' or platform.machine() != manifest['architecture']:
        raise PackageError('linux_host_mismatch')
    if Path(sys.executable) != Path(manifest['python']['path']):
        raise PackageError('python_interpreter_mismatch')
    # Do not impersonate an owner by replacing HOME/CODEX_HOME.
    if os.environ.get('HOME') not in (None, account.pw_dir) or os.environ.get('CODEX_HOME'):
        raise PackageError('owner_home_environment_mismatch')
    if any(os.environ.get(key) for key in ('OPENAI_API_KEY', 'AZURE_OPENAI_API_KEY', 'ANTHROPIC_API_KEY',
                                          'OPENAI_BASE_URL', 'SSH_AUTH_SOCK')):
        raise PackageError('unexpected_provider_or_ssh_environment')
    private_path(home(username), home(username), directory=True)
    return account


def private_path(path, owner_home, *, directory=False, existing=True):
    path, owner_home = absolute(str(path)), absolute(str(owner_home))
    if not path.is_relative_to(owner_home):
        raise PackageError('outside_owner_home')
    no_symlinks(path)
    if not existing and not path.exists():
        path = path.parent
        directory = True
    for candidate in (path, *path.parents):
        if not candidate.is_relative_to(owner_home):
            break
        try:
            info = candidate.lstat()
        except OSError:
            raise PackageError('private_path_unavailable') from None
        kind = stat.S_ISDIR(info.st_mode) if candidate != path or directory else stat.S_ISREG(info.st_mode)
        mode = 0o700 if candidate != path or directory else 0o600
        if (not kind or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != mode
                or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
            raise PackageError('insecure_owner_path')
    return path


def paths(username):
    base = home(username)
    return base, base / '.config/blackoutburst-agent-host/host.json', base / '.local/state/blackoutburst-agents/host'


def check_configuration(config, manifest, release, username):
    """Narrow the existing text-host contract to this installed Unix owner."""
    from blackoutburst_agents.board_reader import _connection_file
    if (not isinstance(config, dict) or config.get('schema_version') not in (1, 2)
            or config.get('owner_id') != manifest['owners'][username]
            or config.get('board_endpoint') != manifest['board_endpoint']
            or not isinstance(config.get('targets'), list) or not 1 <= len(config['targets']) <= 16):
        raise PackageError('host_owner_or_endpoint_mismatch')
    base = home(username)
    for target in config['targets']:
        if not isinstance(target, dict) or target.get('executable') != str(release / 'tools/codex'):
            raise PackageError('unpinned_host_executable')
        if (not isinstance(target.get('model'), str) or not target['model'].strip()
                or 'routing' in target):
            raise PackageError('explicit_model_required')
        workspace = absolute(target.get('workspace'))
        if workspace == base / 'workspaces' or not workspace.is_relative_to(base / 'workspaces'):
            raise PackageError('dedicated_workspace_required')
        private_path(workspace, base, directory=True)
        connection_path = absolute(target.get('connection_file'))
        if not connection_path.is_relative_to(base / '.config/blackoutburst-board'):
            raise PackageError('outside_owner_connection_directory')
        private_path(connection_path, base)
        # Credentials stay in memory here and are never printed or copied.
        connection = _connection_file(connection_path)
        if connection.get('mcp_url') != manifest['board_endpoint']:
            raise PackageError('connection_endpoint_mismatch')
    return config


def _configuration(manifest, release, username, *, configuring=False):
    from blackoutburst_agents.host_config import load
    from blackoutburst_agents.host_journal import HostJournal
    base, configuration, journal = paths(username)
    if configuring:
        private_path(configuration, base)
        config = load(configuration)
    else:
        private_path(journal, base, directory=True)
        # Never create a new empty authority during service startup or diagnosis.
        if not (journal / 'host.sqlite3').is_file():
            raise PackageError('host_configuration_required')
        with HostJournal(journal) as state:
            config = state.control()['configuration']
    check_configuration(config, manifest, release, username)
    return config


def execute(args, manifest):
    """Text-host lifecycle, no-model doctor, and a separately requested nonce probe."""
    from blackoutburst_agents.cli import main as adapter_main
    base, configuration, journal = paths(args.owner)
    if args.action in ('managed-prepare','managed-run'):
        from blackoutburst_agents.host_journal import HostJournal
        from blackoutburst_agents.server_controller import ServerController,launch_catalog
        from blackoutburst_agents.server_control_client import ServerControlClient
        config=_configuration(manifest,args.release,args.owner)
        catalog=launch_catalog(config,manifest_id(manifest))
        with HostJournal(journal) as state:
            if args.action=='managed-prepare':
                with state.owner_lock():state.prepare_managed(args.host_id,catalog)
                print(json.dumps({'status':'managed_prepared','host_id':args.host_id,
                    'targets':len(catalog),'activation_changed':False,'model_calls':0}))
                return 0
            # A role is explicitly granted to this device by its human owner.
            # Publishing cannot create that grant or broaden any profile access.
            with ServerControlClient.from_connection_file(config['targets'][0]['connection_file'],
                    session_id='fixed-server-control',timeout=5) as client:
                if client.endpoint!=config['board_endpoint']:raise PackageError('connection_endpoint_mismatch')
                controller=ServerController(state,client)
                cancelled=threading.Event();previous={}
                try:
                    controller.start_worker()
                    controller.publish(catalog)
                    for signum in (signal.SIGINT,signal.SIGTERM):
                        previous[signum]=signal.signal(signum,lambda *_:cancelled.set())
                    output=controller.run(should_cancel=cancelled.is_set)
                finally:
                    controller.shutdown()
                    for signum,handler in previous.items():signal.signal(signum,handler)
                print(json.dumps(output));return 2 if output['status']=='needs_review' else 0
    if args.action == 'doctor':
        return adapter_main(['doctor', '--codex', str(args.release / 'tools/codex')])
    if args.action == 'probe':
        if (not isinstance(args.model, str) or not args.model.strip() or len(args.model) > 200
                or any(ord(char) < 32 for char in args.model) or not 1 <= args.timeout <= 300):
            raise PackageError('invalid_probe_request')
        return adapter_main(['probe', '--codex', str(args.release / 'tools/codex'),
                             '--model', args.model, '--timeout', str(args.timeout)])
    if args.action == 'check':
        from blackoutburst_agents.host_journal import HostJournal
        _configuration(manifest, args.release, args.owner)
        with HostJournal(journal) as state:
            enabled = state.control()['enabled']
        print(json.dumps({'status': 'prepared', 'owner': args.owner, 'mode': 'text_only',
                          'activation_changed': False, 'host_enabled': enabled,
                          'linux_development_qualified': False,
                          'provider_login_checked': False, 'board_authorization_checked': False}))
        return 0
    if args.action == 'configure':
        _configuration(manifest, args.release, args.owner, configuring=True)
        private_path(journal, base, directory=True, existing=False)
    elif args.action in ('enable', 'run', 'tick'):
        _configuration(manifest, args.release, args.owner)
    else:
        private_path(journal, base, directory=True)
        if not (journal / 'host.sqlite3').is_file():
            raise PackageError('host_configuration_required')
    argv = ['host', '--journal', str(journal), args.action]
    if args.action == 'configure':
        argv += ['--config', str(configuration)]
    elif args.action == 'enable':
        argv += ['--revision', str(args.revision)]
    elif args.action == 'reconcile':
        argv += ['--batch', args.batch]
    return adapter_main(argv)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--owner', choices=OWNER_NAMES, required=True)
    actions = parser.add_subparsers(dest='action', required=True)
    for name in ('check', 'doctor', 'configure', 'status', 'tick', 'run', 'stop'):
        actions.add_parser(name)
    probe = actions.add_parser('probe', help='Explicit nonce response; consumes personal subscription usage.')
    probe.add_argument('--model', required=True)
    probe.add_argument('--timeout', type=int, default=120)
    actions.add_parser('enable').add_argument('--revision', type=int, required=True)
    actions.add_parser('reconcile').add_argument('--batch', required=True)
    actions.add_parser('managed-prepare').add_argument('--host-id',required=True)
    actions.add_parser('managed-run')
    args = parser.parse_args(argv)
    try:
        manifest = verify_release(args.release)
        owner_context(manifest, args.owner)
        sys.path.insert(0, str(args.release / 'source'))
        return execute(args, manifest)
    except PackageError as error:
        print(json.dumps({'status': 'blocked', 'reason': error.code})); return 2
    except Exception:
        print(json.dumps({'status': 'blocked', 'reason': 'owner_host_unavailable'})); return 2


if __name__ == '__main__':
    raise SystemExit(main())
