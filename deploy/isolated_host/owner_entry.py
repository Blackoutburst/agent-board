#!/usr/bin/env python3
"""Restricted entry point for one installed owner. No login, pairing or auto-enable."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import pwd
import re
import stat
import sys
import signal
import threading
import subprocess
import uuid
from contextlib import ExitStack

if __package__:
    from .package import (OWNER_NAMES, PackageError, absolute, home, no_symlinks,
                          protected, read_regular, verify_release, manifest_id, RELEASES,
                          UNIT_DIR, render_unit, unit_name)
else:
    # Installed here by root. -I does not put a script's directory on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from package import (OWNER_NAMES, PackageError, absolute, home, no_symlinks,
                         protected, read_regular, verify_release, manifest_id, RELEASES,
                         UNIT_DIR, render_unit, unit_name)


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


def _pinned_executable(value, manifest, release, verified):
    if value==str(release/'tools/codex'):return
    # A source update need not rewrite the owner's immutable private config.
    # Reuse a previous installed tool ONLY when its complete protected release
    # verifies and every execution/account/tool pin is identical.
    path=absolute(value)
    previous=path.parent.parent
    if (path!=previous/'tools/codex' or previous.parent!=RELEASES
            or re.fullmatch('[0-9a-f]{64}',previous.name) is None):
        raise PackageError('unpinned_host_executable')
    if str(path) in verified:return
    old=verify_release(previous)
    if (any(old.get(k)!=manifest.get(k) for k in ('architecture','python','owners','board_endpoint','codex_version'))
            or any(old.get('files',{}).get(k)!=manifest.get('files',{}).get(k)
                   or k not in manifest.get('files',{}) for k in ('tools/codex','tools/codex-resources/bwrap'))):
        raise PackageError('unpinned_host_executable')
    verified.add(str(path))


def check_configuration(config, manifest, release, username):
    """Narrow the existing text-host contract to this installed Unix owner."""
    from blackoutburst_agents.board_reader import _connection_file
    if (not isinstance(config, dict) or config.get('schema_version') not in (1, 2)
            or config.get('owner_id') != manifest['owners'][username]
            or config.get('board_endpoint') != manifest['board_endpoint']
            or not isinstance(config.get('targets'), list) or not 1 <= len(config['targets']) <= 16):
        raise PackageError('host_owner_or_endpoint_mismatch')
    base = home(username)
    verified=set()
    for target in config['targets']:
        if not isinstance(target, dict):
            raise PackageError('unpinned_host_executable')
        _pinned_executable(target.get('executable'),manifest,release,verified)
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


def _recovery_services_stopped():
    """Public service metadata only; this entry never stops or starts a unit."""
    expected = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'SubState': 'dead',
                'UnitFileState': 'disabled', 'MainPID': '0', 'ControlPID': '0',
                'DropInPaths': ''}
    for username in OWNER_NAMES:
        unit = unit_name(username)
        wanted = {**expected, 'FragmentPath': str(UNIT_DIR / unit), 'Names': unit}
        try:
            result = subprocess.run(['/usr/bin/systemctl', 'show', '--no-pager',
                '--property=' + ','.join(wanted), unit], check=True,
                capture_output=True, timeout=5, env={'PATH': '/usr/bin:/bin'})
            if len(result.stdout) > 8192:
                raise ValueError
            values = {}
            for line in result.stdout.decode('utf-8', 'strict').splitlines():
                key, value = line.split('=', 1)
                if key in values:
                    raise ValueError
                values[key] = value
            if values != wanted:
                raise ValueError
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            raise PackageError('recovery_requires_stopped_disabled_services') from None


def _recovery_installed_units(manifest, release):
    """The next service start must use these protected compatible source bytes."""
    template = read_regular(release / 'unit.service.in').decode('utf-8', 'strict')
    for username in OWNER_NAMES:
        path = UNIT_DIR / unit_name(username)
        protected(path)
        if read_regular(path) != render_unit(template, manifest, release, username).encode():
            raise PackageError('recovery_installed_source_changed')


def _recovery_existing_paths(username):
    """Reject missing authorities before any journal constructor can create them."""
    base, _, journal = paths(username)
    databases = [(journal, 'host.sqlite3', 0x4242484A, (2, 3, 4)),
                 (journal / 'messages', 'messages.sqlite3', 0x42424D46, (1, 2, 3)),
                 (journal / 'message-runs', 'runs.sqlite3', 0, (2,)),
                 (journal / 'message-runtime', 'runtime.sqlite3', 0x42425254, (1, 3, 4))]
    for directory, name, application_id, versions in databases:
        private_path(directory, base, directory=True)
        database = private_path(directory / name, base)
        # Inspect only the fixed SQLite header before constructors. In particular
        # RunStore must not implicitly migrate an unexpected legacy authority.
        descriptor = os.open(database, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or not 100 <= info.st_size <= 8 * 1024 * 1024):
                raise PackageError('recovery_existing_journal_required')
            header = os.read(descriptor, 100)
        finally:
            os.close(descriptor)
        if (len(header) != 100 or header[:16] != b'SQLite format 3\x00'
                or int.from_bytes(header[60:64], 'big') not in versions
                or int.from_bytes(header[68:72], 'big') != application_id):
            raise PackageError('recovery_journal_version_requires_review')
        for suffix in ('-journal', '-wal', '-shm'):
            candidate = Path(str(database) + suffix)
            if candidate.exists() or candidate.is_symlink():
                raise PackageError('recovery_journal_sidecar_requires_review')
    return journal


def _message_abandon(args, manifest):
    """Finite owner-only explicit resolution; no worker, registration or ACK."""
    from blackoutburst_agents.host_config import digest, connection_digest
    from blackoutburst_agents.host_journal import HostJournal
    from blackoutburst_agents.message_admission_abandonment import MessageAdmissionAbandonment
    from blackoutburst_agents.message_admission_seal import MessageAdmissionSealClient
    from blackoutburst_agents.message_journal import MessageJournal
    from blackoutburst_agents.run_store import RunStore
    from blackoutburst_agents.runtime_journal import RuntimeJournal
    from blackoutburst_agents.server_controller import launch_catalog

    for value in (args.old_release_id, args.config_sha256):
        if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
            raise PackageError('recovery_invalid_pin')
    if args.operation in ('apply', 'resume'):
        if (type(args.proposal_sha256) is not str
                or re.fullmatch('[0-9a-f]{64}', args.proposal_sha256) is None
                or type(args.expected_revision) is not int or args.expected_revision < 0):
            raise PackageError('recovery_expected_proposal_and_revision_required')
    if args.retry_seal and args.operation != 'resume':
        raise PackageError('recovery_retry_requires_explicit_resume')
    if args.operation == 'resume' and not args.intent_id:
        raise PackageError('recovery_intent_required')
    old = verify_release(RELEASES / args.old_release_id)
    if (manifest_id(manifest) == args.old_release_id
            or any(old.get(key) != manifest.get(key) for key in
                   ('architecture', 'python', 'owners', 'board_endpoint', 'codex_version'))
            or any(old['files'].get(key) != manifest['files'].get(key) for key in
                   ('tools/codex', 'tools/codex-resources/bwrap'))):
        raise PackageError('recovery_source_chain_changed')
    journal = _recovery_existing_paths(args.owner)
    _recovery_installed_units(manifest, args.release)
    _recovery_services_stopped()
    with ExitStack() as resources:
        host = resources.enter_context(HostJournal(journal))
        resources.enter_context(host.owner_lock())
        _recovery_installed_units(manifest, args.release)
        _recovery_services_stopped()
        control = host.control()
        config = check_configuration(control['configuration'], manifest, args.release, args.owner)
        settings = host.managed_settings()
        if (config['schema_version'] != 2 or control['hash'] != args.config_sha256
                or control['instance_id'] != args.host_instance
                or settings['catalog_sha256'] != digest(sorted(
                    launch_catalog(config, args.old_release_id), key=lambda row: row['catalog_key']))):
            raise PackageError('recovery_host_or_catalog_changed')
        targets = [target for target in config['targets'] if target['id'] == args.target_id]
        if len(targets) != 1:
            raise PackageError('recovery_target_changed')
        target = targets[0]
        if connection_digest(target['connection_file']) != target['connection_sha256']:
            raise PackageError('recovery_connection_changed')
        # Recheck paths after acquiring the owner lock, before child constructors.
        _recovery_existing_paths(args.owner)
        messages = resources.enter_context(MessageJournal(journal / 'messages'))
        runs = RunStore(journal / 'message-runs', existing_only=True); resources.callback(runs.close)
        runtime = resources.enter_context(RuntimeJournal(journal / 'message-runtime'))
        client = resources.enter_context(MessageAdmissionSealClient.from_connection_file(
            target['connection_file'], session_id=target['board_session_id'], timeout=5))
        if client.endpoint != config['board_endpoint']:
            raise PackageError('connection_endpoint_mismatch')
        coordinator = MessageAdmissionAbandonment(host, messages, runs, runtime, seal_client=client)
        identity = {'owner_id': config['owner_id'], 'agent_id': target['agent_id'],
                    'device_id': args.device_id, 'profile_version': target['profile_version'],
                    'session_id': target['board_session_id']}
        if args.operation == 'status':
            result = coordinator.status(request_key=args.request_key)
        elif args.operation == 'resume':
            proposal = coordinator.proposal(args.request_key)
            if (proposal['proposal_sha256'] != args.proposal_sha256
                    or proposal['request_key'] != args.request_key or proposal['identity'] != identity
                    or proposal['host']['target_id'] != args.target_id
                    or proposal['host']['configuration_sha256'] != args.config_sha256
                    or proposal['host']['instance_id'] != args.host_instance
                    or proposal['dispatch']['target_id'] != args.target_id
                    or proposal['dispatch']['batch_id'] != args.batch_id
                    or proposal['dispatch']['worker_id'] != args.worker_id):
                raise PackageError('recovery_proposal_changed')
            status = coordinator.status(request_key=args.request_key)
            if status['intent_id'] != args.intent_id:
                raise PackageError('recovery_intent_changed')
            coordinator.resume(args.intent_id, expected_revision=args.expected_revision,
                               retry_seal=args.retry_seal)
            result = coordinator.status(intent_id=args.intent_id)
        else:
            proposal = coordinator.propose(request_key=args.request_key, target_id=args.target_id,
                batch_id=args.batch_id, worker_id=args.worker_id,
                identity=identity,
                expected_config_sha256=args.config_sha256, expected_host_instance=args.host_instance)
            if args.operation == 'plan':
                if (type(proposal['proposal_sha256']) is not str
                        or re.fullmatch('[0-9a-f]{64}', proposal['proposal_sha256']) is None):
                    raise PackageError('recovery_invalid_proposal')
                result = {'status': 'planned', 'proposal_sha256': proposal['proposal_sha256']}
            else:
                if proposal['proposal_sha256'] != args.proposal_sha256:
                    raise PackageError('recovery_proposal_changed')
                coordinator.resolve(proposal, expected_revision=args.expected_revision)
                result = coordinator.status(request_key=args.request_key)
        if args.operation != 'plan':
            phases = ('intent', 'sealed', 'runtime_abandoned', 'work_abandoned',
                      'message_abandoned', 'complete')
            if (type(result) is not dict or set(result) != {'intent_id', 'revision', 'phase', 'reason'}
                    or type(result['intent_id']) is not str
                    or type(result['revision']) is not int or not 1 <= result['revision'] < 2**63
                    or type(result['phase']) is not str or result['phase'] not in phases
                    or result['reason'] != 'message_abandon_' + result['phase']):
                raise PackageError('recovery_invalid_status')
            try:
                if str(uuid.UUID(result['intent_id'])) != result['intent_id']:
                    raise ValueError
            except ValueError:
                raise PackageError('recovery_invalid_status') from None
        public = json.dumps({**result, 'activation_changed': False, 'model_calls': 0}, allow_nan=False)
        if len(public.encode()) > 8192:
            raise PackageError('recovery_invalid_status')
        print(public)
        return 0


def _message_executed_disposition(args, manifest):
    """Explicit disposal of one executed blocked message; no retry, publication or ACK."""
    from blackoutburst_agents.host_config import digest, connection_digest
    from blackoutburst_agents.host_journal import HostJournal
    from blackoutburst_agents.message_executed_disposition import MessageExecutedDisposition
    from blackoutburst_agents.message_execution_observer import MessageExecutionObservationClient
    from blackoutburst_agents.message_journal import MessageJournal
    from blackoutburst_agents.run_store import RunStore
    from blackoutburst_agents.runtime_journal import RuntimeJournal
    from blackoutburst_agents.server_controller import launch_catalog

    for value in (args.old_release_id, args.config_sha256):
        if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
            raise PackageError('recovery_invalid_pin')
    if args.operation in ('apply', 'resume'):
        if (type(args.proposal_sha256) is not str
                or re.fullmatch('[0-9a-f]{64}', args.proposal_sha256) is None
                or type(args.expected_revision) is not int or args.expected_revision < 0):
            raise PackageError('recovery_expected_proposal_and_revision_required')
    if args.retry_observation and args.operation != 'resume':
        raise PackageError('recovery_retry_requires_explicit_resume')
    if args.operation == 'resume' and not args.intent_id:
        raise PackageError('recovery_intent_required')
    old = verify_release(RELEASES / args.old_release_id)
    if (manifest_id(manifest) == args.old_release_id
            or any(old.get(key) != manifest.get(key) for key in
                   ('architecture', 'python', 'owners', 'board_endpoint', 'codex_version'))
            or any(old['files'].get(key) != manifest['files'].get(key) for key in
                   ('tools/codex', 'tools/codex-resources/bwrap'))):
        raise PackageError('recovery_source_chain_changed')
    journal = _recovery_existing_paths(args.owner)
    _recovery_installed_units(manifest, args.release)
    _recovery_services_stopped()
    with ExitStack() as resources:
        host = resources.enter_context(HostJournal(journal))
        resources.enter_context(host.owner_lock())
        _recovery_installed_units(manifest, args.release)
        _recovery_services_stopped()
        control = host.control()
        config = check_configuration(control['configuration'], manifest, args.release, args.owner)
        settings = host.managed_settings()
        if (config['schema_version'] != 2 or control['hash'] != args.config_sha256
                or control['instance_id'] != args.host_instance
                or settings['catalog_sha256'] != digest(sorted(
                    launch_catalog(config, args.old_release_id), key=lambda row: row['catalog_key']))):
            raise PackageError('recovery_host_or_catalog_changed')
        targets = [target for target in config['targets'] if target['id'] == args.target_id]
        if len(targets) != 1:
            raise PackageError('recovery_target_changed')
        target = targets[0]
        if connection_digest(target['connection_file']) != target['connection_sha256']:
            raise PackageError('recovery_connection_changed')
        # Recheck paths after acquiring the owner lock, before child constructors.
        _recovery_existing_paths(args.owner)
        messages = resources.enter_context(MessageJournal(journal / 'messages'))
        runs = RunStore(journal / 'message-runs', existing_only=True); resources.callback(runs.close)
        runtime = resources.enter_context(RuntimeJournal(journal / 'message-runtime'))
        client = resources.enter_context(MessageExecutionObservationClient.from_connection_file(
            target['connection_file'], session_id=target['board_session_id'], timeout=5))
        if client.endpoint != config['board_endpoint']:
            raise PackageError('connection_endpoint_mismatch')
        batch_id, worker_id = args.batch_id, args.worker_id
        if (batch_id is None) != (worker_id is None):
            raise PackageError('recovery_exact_message_references_required')
        if batch_id is None:
            # These two local IDs are dependent references of the explicitly
            # selected public work. Discover them only under the true owner lock,
            # never by exporting a journal or selecting a general pending queue.
            references = messages.db.execute(
                'SELECT id,worker_id FROM main.operations WHERE work_id=? LIMIT 2',
                (args.work_id,)).fetchall()
            if len(references) != 1:
                raise PackageError('recovery_exact_message_references_required')
            batch_id, worker_id = references[0]['id'], references[0]['worker_id']
            try:
                if any(type(value) is not str or str(uuid.UUID(value)) != value
                       for value in (batch_id, worker_id)):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise PackageError('recovery_exact_message_references_required') from None
        coordinator = MessageExecutedDisposition(host, messages, runs, runtime, observer=client)
        identity = {'owner_id': config['owner_id'], 'agent_id': target['agent_id'],
                    'device_id': args.device_id, 'profile_version': target['profile_version'],
                    'session_id': target['board_session_id']}
        if args.operation == 'status':
            result = coordinator.status(request_key=args.request_key)
        elif args.operation == 'resume':
            proposal = coordinator.proposal(args.request_key)
            if (proposal['proposal_sha256'] != args.proposal_sha256
                    or proposal['request_key'] != args.request_key or proposal['identity'] != identity
                    or proposal['host']['target_id'] != args.target_id
                    or proposal['host']['configuration_sha256'] != args.config_sha256
                    or proposal['host']['instance_id'] != args.host_instance
                    or proposal['dispatch']['target_id'] != args.target_id
                    or proposal['dispatch']['batch_id'] != batch_id
                    or proposal['dispatch']['worker_id'] != worker_id
                    or proposal['work']['id'] != args.work_id or proposal['turn']['id'] != args.turn_id):
                raise PackageError('recovery_proposal_changed')
            status = coordinator.status(request_key=args.request_key)
            if status['intent_id'] != args.intent_id:
                raise PackageError('recovery_intent_changed')
            coordinator.resume(args.intent_id, expected_revision=args.expected_revision,
                               retry_observation=args.retry_observation)
            result = coordinator.status(intent_id=args.intent_id)
        else:
            proposal = coordinator.propose(request_key=args.request_key, target_id=args.target_id,
                batch_id=batch_id, worker_id=worker_id,
                work_id=args.work_id, turn_id=args.turn_id,
                identity=identity,
                expected_config_sha256=args.config_sha256, expected_host_instance=args.host_instance)
            if args.operation == 'plan':
                if (type(proposal['proposal_sha256']) is not str
                        or re.fullmatch('[0-9a-f]{64}', proposal['proposal_sha256']) is None):
                    raise PackageError('recovery_invalid_proposal')
                result = {'status': 'planned', 'proposal_sha256': proposal['proposal_sha256']}
            else:
                if proposal['proposal_sha256'] != args.proposal_sha256:
                    raise PackageError('recovery_proposal_changed')
                coordinator.apply(proposal, expected_revision=args.expected_revision)
                result = coordinator.status(request_key=args.request_key)
        if args.operation != 'plan':
            phases = ('intent', 'runtime_verified', 'work_disposed', 'message_disposed', 'complete')
            if (type(result) is not dict or set(result) != {'intent_id', 'revision', 'phase', 'reason'}
                    or type(result['intent_id']) is not str
                    or type(result['revision']) is not int or not 1 <= result['revision'] < 2**63
                    or type(result['phase']) is not str or result['phase'] not in phases
                    or result['reason'] != 'message_executed_' + result['phase']
                    or result['revision'] != phases.index(result['phase']) + 1):
                raise PackageError('recovery_invalid_status')
            try:
                if str(uuid.UUID(result['intent_id'])) != result['intent_id']:
                    raise ValueError
            except ValueError:
                raise PackageError('recovery_invalid_status') from None
        _recovery_services_stopped()
        _recovery_installed_units(manifest, args.release)
        public = json.dumps({**result, 'activation_changed': False, 'model_calls': 0}, allow_nan=False)
        if len(public.encode()) > 8192:
            raise PackageError('recovery_invalid_status')
        print(public)
        return 0


def _catalog_source(args, manifest):
    """Finite source-only transition for the diagnosed, closed message host."""
    from blackoutburst_agents.host_config import digest, connection_digest
    from blackoutburst_agents.host_journal import HostJournal
    from blackoutburst_agents.message_journal import MessageJournal
    from blackoutburst_agents.run_store import RunStore
    from blackoutburst_agents.runtime_journal import RuntimeJournal
    from blackoutburst_agents.server_controller import launch_catalog
    from blackoutburst_agents.server_control_client import ServerControlClient
    from blackoutburst_agents.message_admission_seal import MessageAdmissionSealClient
    from blackoutburst_agents.message_admission_abandonment import MessageAdmissionAbandonment
    from blackoutburst_agents.message_abandoned_children import verify_completed_message_children
    from blackoutburst_agents.message_abandon_protocol import hashed
    from blackoutburst_agents.catalog_source_transition import CatalogSourceTransition, proposal as catalog_proposal

    new_id = manifest_id(manifest)
    for value in (args.old_release_id, args.config_sha256, new_id):
        if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
            raise PackageError('recovery_invalid_pin')
    if args.operation in ('apply', 'resume') and (
            type(args.proposal_sha256) is not str
            or re.fullmatch('[0-9a-f]{64}', args.proposal_sha256) is None
            or type(args.expected_revision) is not int or args.expected_revision < 0):
        raise PackageError('recovery_expected_proposal_and_revision_required')

    def source_guard(old_id, next_id, configuration_sha256):
        if (old_id != args.old_release_id or next_id != new_id
                or configuration_sha256 != args.config_sha256):
            raise PackageError('recovery_source_chain_changed')
        current = verify_release(args.release)
        old = verify_release(RELEASES / old_id)
        if (manifest_id(current) != new_id or new_id == old_id
                or any(old.get(key) != current.get(key) for key in
                       ('architecture', 'python', 'owners', 'board_endpoint', 'codex_version'))
                or any(old['files'].get(key) != current['files'].get(key) for key in
                       ('tools/codex', 'tools/codex-resources/bwrap'))):
            raise PackageError('recovery_source_chain_changed')
        _recovery_installed_units(current, args.release)
        _recovery_services_stopped()
        return True

    source_guard(args.old_release_id, new_id, args.config_sha256)
    _recovery_services_stopped()
    journal = _recovery_existing_paths(args.owner)
    with ExitStack() as resources:
        host = resources.enter_context(HostJournal(journal))
        resources.enter_context(host.owner_lock())
        source_guard(args.old_release_id, new_id, args.config_sha256)
        _recovery_services_stopped(); _recovery_existing_paths(args.owner)
        control = host.control()
        config = check_configuration(control['configuration'], manifest, args.release, args.owner)
        allowed_catalogs = [digest(sorted(launch_catalog(config, value), key=lambda row: row['catalog_key']))
                            for value in (args.old_release_id, new_id)]
        if (config['schema_version'] != 2 or control['hash'] != args.config_sha256
                or control['instance_id'] != args.host_instance
                or host.managed_settings()['catalog_sha256'] not in allowed_catalogs):
            raise PackageError('recovery_host_or_catalog_changed')
        targets = [target for target in config['targets'] if target['id'] == args.target_id]
        if len(targets) != 1:
            raise PackageError('recovery_target_changed')
        target = targets[0]
        if connection_digest(target['connection_file']) != target['connection_sha256']:
            raise PackageError('recovery_connection_changed')
        closed = host._abandon_record(request_key=args.abandon_request_key)
        if closed is None or closed['phase'] != 'complete':
            raise PackageError('recovery_completed_abandonment_required')
        original = closed['proposal']
        identity = {'owner_id': config['owner_id'], 'agent_id': target['agent_id'],
                    'device_id': args.device_id, 'profile_version': target['profile_version'],
                    'session_id': target['board_session_id']}
        if (original['identity'] != identity or original['host']['target_id'] != args.target_id
                or original['host']['instance_id'] != args.host_instance
                or original['host']['configuration_sha256'] != args.config_sha256
                or original['dispatch']['batch_id'] != args.batch_id
                or original['dispatch']['worker_id'] != args.worker_id):
            raise PackageError('recovery_proposal_changed')
        messages = resources.enter_context(MessageJournal(journal / 'messages'))
        runs = RunStore(journal / 'message-runs', existing_only=True); resources.callback(runs.close)
        runtime = resources.enter_context(RuntimeJournal(journal / 'message-runtime'))
        seal = resources.enter_context(MessageAdmissionSealClient.from_connection_file(
            target['connection_file'], session_id=target['board_session_id'], timeout=5))
        client = resources.enter_context(ServerControlClient.from_connection_file(
            target['connection_file'], session_id='fixed-server-control', timeout=5))
        if seal.endpoint != config['board_endpoint'] or client.endpoint != config['board_endpoint']:
            raise PackageError('connection_endpoint_mismatch')
        abandonment = MessageAdmissionAbandonment(host, messages, runs, runtime, seal_client=seal)
        expected_children = {'schema': 'message-abandonment-children-v1', 'intent_id': closed['id'],
            'proof_sha256': hashed(closed['proof']),
            'children_sha256': hashed({'intent_id': closed['id'],
                'proposal_sha256': original['proposal_sha256'],
                **{key: closed['checkpoints'][key] for key in ('runtime', 'work', 'message')}})}

        def stopped_guard():
            _recovery_services_stopped()
            if connection_digest(target['connection_file']) != target['connection_sha256']:
                raise PackageError('recovery_connection_changed')
            value = verify_completed_message_children(host, messages, runs, runtime, intent_id=closed['id'])
            if (type(value) is not dict or value != expected_children
                    or set(value) != {'schema', 'intent_id', 'proof_sha256', 'children_sha256'}
                    or value['schema'] != 'message-abandonment-children-v1'
                    or value['intent_id'] != closed['id']
                    or any(type(value[key]) is not str or re.fullmatch('[0-9a-f]{64}', value[key]) is None
                           for key in ('proof_sha256', 'children_sha256'))):
                raise PackageError('recovery_children_unconfirmed')
            return True

        executed_disposition = None
        executed_key = getattr(args, 'executed_request_key', None)
        if executed_key is not None:
            from blackoutburst_agents.message_executed_disposition import MessageExecutedDisposition
            from blackoutburst_agents.message_execution_observer import MessageExecutionObservationClient
            from blackoutburst_agents.message_disposed_children import verify_disposed_message_children
            executed = host._executed_record(request_key=executed_key)
            if executed is None or executed['phase'] != 'complete':
                raise PackageError('recovery_completed_executed_disposition_required')
            ep = executed['proposal']
            if (ep['identity'] != identity or ep['host']['target_id'] != args.target_id
                    or ep['host']['instance_id'] != args.host_instance
                    or ep['host']['configuration_sha256'] != args.config_sha256):
                raise PackageError('recovery_proposal_changed')
            observer = resources.enter_context(MessageExecutionObservationClient.from_connection_file(
                target['connection_file'], session_id=target['board_session_id'], timeout=5))
            if observer.endpoint != config['board_endpoint']:
                raise PackageError('connection_endpoint_mismatch')
            executed_disposition = MessageExecutedDisposition(host, messages, runs, runtime, observer=observer)

            def disposed_children():
                _recovery_services_stopped()
                if connection_digest(target['connection_file']) != target['connection_sha256']:
                    raise PackageError('recovery_connection_changed')
                value = verify_disposed_message_children(host, messages, runs, runtime,
                    abandonment_intent_id=closed['id'], executed_intent_id=executed['id'])
                if (type(value) is not dict or set(value) != {'schema', 'roster_sha256'}
                        or value['schema'] != 'message-disposed-children-v1'
                        or type(value['roster_sha256']) is not str
                        or re.fullmatch('[0-9a-f]{64}', value['roster_sha256']) is None):
                    raise PackageError('recovery_children_unconfirmed')
                return value

            expected_roster = disposed_children()

            def stopped_guard():
                if disposed_children() != expected_roster:
                    raise PackageError('recovery_children_unconfirmed')
                return True

        coordinator = CatalogSourceTransition(host, client, source_guard=source_guard,
            stopped_guard=stopped_guard, abandonment=abandonment,
            executed_disposition=executed_disposition)
        intent_id = 'source-' + digest(args.request_key)
        if args.intent_id is not None and args.intent_id != intent_id:
            raise PackageError('recovery_intent_changed')
        if args.operation == 'status':
            result = coordinator.status(intent_id)
        elif args.operation == 'resume':
            saved = host.managed_effect(intent_id)
            if saved is None:
                raise PackageError('recovery_intent_required')
            proposal = catalog_proposal(saved['arguments'])
            if (proposal['proposal_sha256'] != args.proposal_sha256
                    or proposal['request_key'] != args.request_key
                    or proposal['host_instance'] != args.host_instance
                    or proposal['configuration_sha256'] != args.config_sha256
                    or proposal['old_release_sha256'] != args.old_release_id
                    or proposal['new_release_sha256'] != new_id):
                raise PackageError('recovery_proposal_changed')
            coordinator.resume(intent_id, expected_revision=args.expected_revision,
                               expected_proposal_sha256=args.proposal_sha256)
            result = coordinator.status(intent_id)
        else:
            proposal = coordinator.plan(old_release_sha256=args.old_release_id,
                new_release_sha256=new_id, request_key=args.request_key)
            if type(proposal.get('proposal_sha256')) is not str or re.fullmatch('[0-9a-f]{64}', proposal['proposal_sha256']) is None:
                raise PackageError('recovery_invalid_proposal')
            if args.operation == 'plan':
                result = {'status': 'planned', 'proposal_sha256': proposal['proposal_sha256']}
            else:
                if proposal['proposal_sha256'] != args.proposal_sha256:
                    raise PackageError('recovery_proposal_changed')
                coordinator.resolve(proposal, expected_revision=args.expected_revision)
                result = coordinator.status(intent_id)
        if args.operation != 'plan':
            phases = {1: 'stop_pending', 2: 'replace_pending', 3: 'commit_pending', 4: 'complete'}
            if (type(result) is not dict or set(result) != {'intent_id', 'revision', 'phase', 'proposal_sha256'}
                    or result['intent_id'] != intent_id or type(result['revision']) is not int
                    or result['revision'] not in phases or result['phase'] != phases[result['revision']]
                    or type(result['proposal_sha256']) is not str
                    or re.fullmatch('[0-9a-f]{64}', result['proposal_sha256']) is None):
                raise PackageError('recovery_invalid_status')
        print(json.dumps({**result, 'activation_changed': False, 'model_calls': 0}, allow_nan=False))
        return 0


def execute(args, manifest):
    """Text-host lifecycle, no-model doctor, and a separately requested nonce probe."""
    from blackoutburst_agents.cli import main as adapter_main
    base, configuration, journal = paths(args.owner)
    if args.action == 'managed-message-dispose-executed':
        return _message_executed_disposition(args, manifest)
    if args.action == 'managed-message-abandon':
        return _message_abandon(args, manifest)
    if args.action == 'managed-catalog-source':
        return _catalog_source(args, manifest)
    if args.action in ('managed-prepare','managed-run'):
        from blackoutburst_agents.host_journal import HostJournal
        from blackoutburst_agents.server_controller import ServerController
        from blackoutburst_agents.message_append_journal import effective_message_catalog
        from blackoutburst_agents.server_control_client import ServerControlClient
        config=_configuration(manifest,args.release,args.owner)
        with HostJournal(journal) as state:
            catalog=effective_message_catalog(state,manifest_id(manifest))
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
    recovery = actions.add_parser('managed-message-abandon',
        help='Explicit finite sealed abandonment; never starts or enables an agent.')
    recovery.add_argument('operation', choices=('plan', 'apply', 'status', 'resume'))
    for name in ('old-release-id', 'config-sha256', 'host-instance', 'target-id',
                 'batch-id', 'worker-id', 'device-id', 'request-key'):
        recovery.add_argument('--' + name, required=True)
    recovery.add_argument('--proposal-sha256')
    recovery.add_argument('--expected-revision', type=int)
    recovery.add_argument('--intent-id')
    recovery.add_argument('--retry-seal', action='store_true')
    disposition = actions.add_parser('managed-message-dispose-executed',
        help='Explicit finite disposition of a blocked executed message; no model retry or publication.')
    disposition.add_argument('operation', choices=('plan', 'apply', 'status', 'resume'))
    for name in ('old-release-id', 'config-sha256', 'host-instance', 'target-id',
                 'work-id', 'turn-id', 'device-id', 'request-key'):
        disposition.add_argument('--' + name, required=True)
    disposition.add_argument('--batch-id')
    disposition.add_argument('--worker-id')
    disposition.add_argument('--proposal-sha256')
    disposition.add_argument('--expected-revision', type=int)
    disposition.add_argument('--intent-id')
    disposition.add_argument('--retry-observation', action='store_true')
    catalog = actions.add_parser('managed-catalog-source',
        help='Explicit source catalogue change after confirmed Stops; no activation.')
    catalog.add_argument('operation', choices=('plan', 'apply', 'status', 'resume'))
    for name in ('old-release-id', 'config-sha256', 'host-instance', 'target-id',
                 'batch-id', 'worker-id', 'device-id', 'request-key', 'abandon-request-key'):
        catalog.add_argument('--' + name, required=True)
    catalog.add_argument('--proposal-sha256')
    catalog.add_argument('--expected-revision', type=int)
    catalog.add_argument('--intent-id')
    catalog.add_argument('--executed-request-key')
    args = parser.parse_args(argv)
    try:
        if (args.action in ('managed-message-abandon', 'managed-message-dispose-executed', 'managed-catalog-source')
                and Path(__file__).absolute() != args.release / 'owner_entry.py'):
            raise PackageError('recovery_entry_release_mismatch')
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
