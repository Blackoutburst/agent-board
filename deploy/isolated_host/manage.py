#!/usr/bin/env python3
"""Prepare offline bundles; plan by default, install only with explicit --apply."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile

from .package import (BASE, OWNER_NAMES, RELEASES, UNIT_DIR, PackageError, absolute,
                      elf, encoded, home, load_bundle, manifest_id, no_symlinks,
                      protected, read_regular, render_unit, unit_name, validate_manifest,
                      endpoint)


def command(argv):
    try:
        return subprocess.run(argv, check=True, capture_output=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        raise PackageError('system_command_failed') from None


def git(source, *arguments):
    return command(['git', '-C', str(source), *arguments])


def prepare(source, output, *, codex, bwrap, codex_version, architecture,
            python, python_sha256, board_endpoint, owners, unit_mode='legacy'):
    """Only tracked public code and explicitly supplied ELF tools enter a bundle."""
    source, output = no_symlinks(source), no_symlinks(output)
    if output.exists():
        raise PackageError('output_exists')
    if codex is None or bwrap is None:
        raise PackageError('linux_tools_required')
    if type(unit_mode) is not str or unit_mode not in ('legacy', 'managed'):
        raise PackageError('invalid_unit_mode')
    if git(source, 'status', '--porcelain'):
        raise PackageError('clean_source_required')
    revision = git(source, 'rev-parse', 'HEAD').decode().strip()
    tracked = git(source, 'ls-tree', '-r', '--name-only', 'HEAD').decode().splitlines()
    selected = {name: 'source/' + name.removeprefix('integrations/codex-personal/')
                for name in tracked if re.fullmatch(r'integrations/codex-personal/blackoutburst_agents/[A-Za-z0-9_]+\.py', name)}
    selected.update({'connect.py': 'source/connect.py',
                     'deploy/isolated_host/package.py': 'package.py',
                     'deploy/isolated_host/owner_entry.py': 'owner_entry.py',
                     'deploy/isolated_host/' + ('managed-unit.service.in' if unit_mode == 'managed'
                                               else 'unit.service.in'): 'unit.service.in'})
    if not set(selected) <= set(tracked):
        raise PackageError('incomplete_source')
    toml = git(source, 'show', revision + ':integrations/codex-personal/pyproject.toml').decode()
    versions = re.findall(r'^version = "(0\.[0-9]+\.[0-9]+)"$', toml, re.MULTILINE)
    if len(versions) != 1:
        raise PackageError('adapter_version_unavailable')
    data = {destination: git(source, 'show', revision + ':' + name)
            for name, destination in selected.items()}
    data['tools/codex'] = read_regular(codex)
    data['tools/codex-resources/bwrap'] = read_regular(bwrap)
    for name in ('tools/codex', 'tools/codex-resources/bwrap'):
        elf(data[name], architecture)
    manifest = validate_manifest({
        'schema_version': 1, 'revision': revision, 'adapter_version': versions[0],
        'architecture': architecture, 'python': {'path': str(python), 'sha256': python_sha256},
        'codex_version': codex_version, 'board_endpoint': endpoint(board_endpoint), 'owners': owners,
        'files': {name: {'size': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
                  for name, value in data.items()}})
    # No target is published until the complete private bundle verifies.
    no_symlinks(output.parent)
    stage = Path(tempfile.mkdtemp(prefix='.host-bundle-', dir=output.parent))
    try:
        for name, value in {**data, 'manifest.json': encoded(manifest)}.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(value); target.chmod(0o600)
        load_bundle(stage)
        os.rename(stage, output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {'status': 'prepared', 'bundle_id': manifest_id(manifest), 'revision': revision,
            'adapter_version': versions[0], 'unit_mode': unit_mode,
            'execution_qualified': False, 'activated': False}


def plan(directory):
    manifest = load_bundle(directory)
    return {'status': 'plan', 'bundle_id': manifest_id(manifest), 'revision': manifest['revision'],
            'architecture': manifest['architecture'], 'python': manifest['python']['path'],
            'owners': list(OWNER_NAMES), 'services': [unit_name(name) for name in OWNER_NAMES],
            'actions': ['create_two_locked_unix_accounts', 'install_pinned_public_source_and_tools',
                        'create_private_empty_owner_directories', 'write_disabled_services', 'reload_unit_metadata'],
            'human_steps': ['pair_separate_board_devices', 'official_personal_codex_login',
                            'choose_private_host_configuration', 'explicit_finite_activation'],
            'execution_qualified': False, 'activated': False}


def _root_directory(path, mode=0o755):
    no_symlinks(path)
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    protected(path)
    return path


def _new_file(path, data, mode):
    no_symlinks(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    os.chmod(path, mode)


def _preflight(manifest):
    if os.geteuid() != 0:
        raise PackageError('administrator_required')
    if platform.system() != 'Linux' or platform.machine() != manifest['architecture']:
        raise PackageError('linux_host_mismatch')
    python = Path(manifest['python']['path'])
    protected(python)
    binary = read_regular(python)
    elf(binary, manifest['architecture'])
    if hashlib.sha256(binary).hexdigest() != manifest['python']['sha256']:
        raise PackageError('python_pin_changed')
    for executable in (Path('/usr/sbin/useradd'), Path('/usr/bin/systemctl')):
        protected(executable)
    version = command([str(python), '-I', '-c', 'import sys; print("%s.%s" % sys.version_info[:2])']).decode().strip()
    if not re.fullmatch(r'3\.(?:1[0-9]|[2-9][0-9])', version):
        raise PackageError('python_310_required')
    lines = command(['/usr/bin/systemctl', '--version']).decode().splitlines()
    systemd = lines[0] if lines else ''
    match = re.match(r'^systemd ([0-9]+)', systemd)
    if not match or int(match[1]) < 249:
        raise PackageError('systemd_249_required')
    for name in OWNER_NAMES:
        try:
            pwd.getpwnam(name)
        except KeyError:
            pass
        else:
            raise PackageError('existing_account_requires_review')
        if home(name).exists() or home(name).is_symlink() or (UNIT_DIR / unit_name(name)).exists():
            raise PackageError('existing_installation_requires_review')
        # Refuse pre-existing enablement/aliases, including dangling wants links.
        for unit_root in (UNIT_DIR, Path('/run/systemd/system'), Path('/usr/lib/systemd/system')):
            if unit_root.exists() and any(unit_root.rglob(unit_name(name))):
                raise PackageError('existing_installation_requires_review')


def install(directory):
    manifest = load_bundle(directory)
    _preflight(manifest)
    _root_directory(BASE)
    lock = BASE / '.install.lock'
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise PackageError('insecure_install_lock')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PackageError('installation_in_progress') from None
        # Another installer may have completed while the first preflight ran.
        _preflight(manifest)
        return _install_locked(directory, manifest)
    finally:
        os.close(descriptor)


def _install_locked(directory, manifest):
    """First installation only; never adopt accounts, overwrite state or start units."""
    identity = manifest_id(manifest)
    release = RELEASES / identity
    if release.exists() or release.is_symlink():
        raise PackageError('existing_installation_requires_review')
    for path in (RELEASES, BASE / 'owners', BASE / 'installations'):
        _root_directory(path)
    # Copy into root-controlled storage, then hash again before trusting anything.
    stage = Path(tempfile.mkdtemp(prefix='.install-', dir=RELEASES))
    state = {'schema_version': 1, 'bundle_id': identity, 'status': 'installing', 'created_accounts': [],
             'written_units': [], 'activated': False}
    receipt = BASE / 'installations' / (identity + '.json')
    _new_file(receipt, encoded(state), 0o600)

    def save():
        temporary = receipt.with_suffix('.tmp')
        _new_file(temporary, encoded(state), 0o600)
        os.replace(temporary, receipt)

    try:
        for name in ('manifest.json', *manifest['files']):
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            _new_file(target, read_regular(directory / name), 0o755 if name.startswith('tools/') else 0o444)
        copied = load_bundle(stage)
        if copied != manifest:
            raise PackageError('pin_changed')
        for root, dirs, _files in os.walk(stage):
            Path(root).chmod(0o555)
        os.rename(stage, release)
        template = (release / 'unit.service.in').read_text()
        for name in OWNER_NAMES:
            command(['/usr/sbin/useradd', '--system', '--user-group', '--no-create-home',
                     '--home-dir', str(home(name)), '--shell', '/bin/bash', name])
            account = pwd.getpwnam(name)
            if account.pw_uid == 0 or account.pw_dir != str(home(name)):
                raise PackageError('account_creation_mismatch')
            state['created_accounts'].append({'name': name, 'uid': account.pw_uid, 'gid': account.pw_gid})
            save()
            # Empty directories only: no host journal, pairing, login or budget.
            for part in ('', '.config', '.config/agent-board', '.config/blackoutburst-agent-host',
                         '.local', '.local/state', '.local/state/blackoutburst-agents', 'workspaces'):
                path = home(name) / part
                path.mkdir(mode=0o700)
                os.chown(path, account.pw_uid, account.pw_gid); path.chmod(0o700)
            _new_file(UNIT_DIR / unit_name(name), render_unit(template, manifest, release, name).encode(), 0o644)
            state['written_units'].append(unit_name(name)); save()
        command(['/usr/bin/systemctl', 'daemon-reload'])
        state['status'] = 'installed_disabled'; save()
        return {'status': 'installed_disabled', 'bundle_id': identity, 'activated': False,
                'owners': list(OWNER_NAMES), 'human_setup_required': True, 'execution_qualified': False}
    except Exception:
        state['status'] = 'incomplete_requires_review'
        try:
            save()
        except Exception:
            pass
        raise
    finally:
        if stage.exists():
            # This temporary stage contains public code only, never private state.
            for root, dirs, _files in os.walk(stage):
                Path(root).chmod(0o700)
            shutil.rmtree(stage)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('prepare', help='Offline public bundle; no downloads or execution.')
    for name in ('source', 'output', 'python'):
        build.add_argument('--' + name, required=True, type=Path)
    for name in ('codex', 'bwrap'):
        build.add_argument('--' + name, type=Path)
    for name in ('python-sha256', 'codex-version', 'board-endpoint', 'thescreamer-owner', 'blackout-owner'):
        build.add_argument('--' + name, required=True)
    build.add_argument('--architecture', choices=('x86_64', 'aarch64'), required=True)
    build.add_argument('--unit-mode', choices=('legacy', 'managed'), default='legacy')
    setup = commands.add_parser('install', help='Print plan unless --apply is supplied.')
    setup.add_argument('--bundle', required=True, type=Path)
    setup.add_argument('--apply', action='store_true')
    update = commands.add_parser('update', help='Existing disabled installation only; plan unless --apply.')
    update.add_argument('--bundle', required=True, type=Path)
    update.add_argument('--expected-old-release', required=True)
    update.add_argument('--expected-new-release', required=True)
    update.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            result = prepare(args.source, args.output, codex=args.codex, bwrap=args.bwrap,
                             codex_version=args.codex_version, architecture=args.architecture,
                             python=args.python, python_sha256=args.python_sha256,
                             board_endpoint=args.board_endpoint, owners={
                                 OWNER_NAMES[0]: args.thescreamer_owner, OWNER_NAMES[1]: args.blackout_owner},
                             unit_mode=args.unit_mode)
        elif args.command == 'update':
            from .update import update_existing, plan_update
            function = update_existing if args.apply else plan_update
            result = function(args.bundle, expected_old_release=args.expected_old_release,
                              expected_new_release=args.expected_new_release)
        else:
            result = install(args.bundle) if args.apply else plan(args.bundle)
        print(json.dumps(result, sort_keys=True)); return 0
    except PackageError as error:
        print(json.dumps({'status': 'blocked', 'reason': error.code, 'activated': False})); return 2
    except (OSError, ValueError, KeyError):
        print(json.dumps({'status': 'blocked', 'reason': 'installation_unavailable', 'activated': False})); return 2


if __name__ == '__main__':
    raise SystemExit(main())
