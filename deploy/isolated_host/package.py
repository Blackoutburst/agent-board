"""Pinned, credential-free host bundles. No network or subprocess execution here."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import urllib.parse

OWNER_NAMES = ('bb-agent-thescreamer', 'bb-agent-blackout')
BASE = Path('/var/lib/blackoutburst-agent-host')
RELEASES = Path('/opt/blackoutburst-agent-host/releases')
UNIT_DIR = Path('/etc/systemd/system')
MAX_FILE = 384 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024
SHA = re.compile(r'^[0-9a-f]{64}$')
IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$')
SAFE_PATH = re.compile(r'^/[A-Za-z0-9_./-]+$')


class PackageError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n').encode()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise PackageError('invalid_manifest')
        result[key] = value
    return result


def decode(data):
    try:
        return json.loads(data, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PackageError('invalid_manifest') from None


def absolute(value):
    if (not isinstance(value, str) or not SAFE_PATH.fullmatch(value)
            or len(value) > 1024 or value == '/' or str(PurePosixPath(value)) != value
            or '..' in PurePosixPath(value).parts):
        raise PackageError('invalid_path')
    return Path(value)


def relative(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_./-]+', value)
            or value.startswith('/') or '..' in PurePosixPath(value).parts
            or str(PurePosixPath(value)) != value or value == '.'):
        raise PackageError('invalid_inventory_path')
    return value


def endpoint(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        if (not isinstance(value, str) or parsed.scheme != 'https' or not parsed.hostname
                or parsed.username or parsed.password or parsed.path != '/mcp'
                or parsed.query or parsed.fragment or any(c.isspace() for c in value)
                or any(c in value for c in ('\\', '?', '#', '%'))):
            raise ValueError
        host = parsed.hostname.rstrip('.').encode('idna').decode().lower()
        if not re.fullmatch(r'[a-z0-9.-]+', host):
            raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        return 'https://' + host + (':' + str(port) if port not in (None, 443) else '') + '/mcp'
    except (AttributeError, TypeError, UnicodeError, ValueError):
        raise PackageError('invalid_board_endpoint') from None


def no_symlinks(path):
    path = absolute(str(path))
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise PackageError('symlink_path')
    return path


def read_regular(path, *, maximum=MAX_FILE):
    """Bounded no-follow read; neither source nor credential paths are printed."""
    path = no_symlinks(path)
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 <= before.st_size <= maximum:
            raise PackageError('invalid_regular_file')
        chunks, count = [], 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, maximum + 1 - count))
            if not chunk:
                break
            chunks.append(chunk); count += len(chunk)
            if count > maximum:
                raise PackageError('file_too_large')
        after = os.fstat(fd)
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise PackageError('file_changed')
        return b''.join(chunks)
    except OSError:
        raise PackageError('file_unavailable') from None
    finally:
        if fd is not None:
            os.close(fd)


def elf(data, architecture):
    """Verify a Linux-compatible ELF header, not execution or sandbox qualification."""
    machine = {'x86_64': 62, 'aarch64': 183}.get(architecture)
    if (machine is None or len(data) < 64 or data[:7] != b'\x7fELF\x02\x01\x01'
            or data[7] not in (0, 3) or struct.unpack_from('<H', data, 16)[0] not in (2, 3)
            or struct.unpack_from('<H', data, 18)[0] != machine):
        raise PackageError('linux_elf_required')


def home(username):
    if username not in OWNER_NAMES:
        raise PackageError('unknown_owner')
    return BASE / 'owners' / username


def unit_name(username):
    home(username)
    return 'blackoutburst-agent-host-' + username + '.service'


def validate_manifest(value):
    keys = {'schema_version', 'revision', 'adapter_version', 'architecture', 'python',
            'codex_version', 'board_endpoint', 'owners', 'files'}
    if not isinstance(value, dict) or set(value) != keys or type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise PackageError('invalid_manifest')
    if (not isinstance(value['revision'], str) or not re.fullmatch(r'[0-9a-f]{40}', value['revision'])
            or not isinstance(value['adapter_version'], str) or not re.fullmatch(r'0\.[0-9]+\.[0-9]+', value['adapter_version'])
            or value['architecture'] not in ('x86_64', 'aarch64')
            or not isinstance(value['codex_version'], str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9.-]+)?', value['codex_version'])
            or endpoint(value['board_endpoint']) != value['board_endpoint']):
        raise PackageError('invalid_manifest')
    python = value['python']
    if (not isinstance(python, dict) or set(python) != {'path', 'sha256'}
            or not isinstance(python['sha256'], str) or not SHA.fullmatch(python['sha256'])):
        raise PackageError('invalid_python_pin')
    path = absolute(python['path'])
    if not str(path).startswith('/usr/bin/python3.'):
        raise PackageError('explicit_system_python_required')
    if (not isinstance(value['owners'], dict) or set(value['owners']) != set(OWNER_NAMES)
            or any(not isinstance(v, str) or not IDENTIFIER.fullmatch(v) for v in value['owners'].values())
            or len(set(value['owners'].values())) != 2):
        raise PackageError('distinct_board_owners_required')
    files = value['files']
    if not isinstance(files, dict) or not 6 <= len(files) <= 300:
        raise PackageError('invalid_inventory')
    required = {'owner_entry.py', 'package.py', 'unit.service.in', 'source/connect.py',
                'tools/codex', 'tools/codex-resources/bwrap',
                'source/blackoutburst_agents/__init__.py', 'source/blackoutburst_agents/cli.py'}
    if not required <= set(files):
        raise PackageError('incomplete_inventory')
    total = 0
    for name, info in files.items():
        relative(name)
        allowed = name in required or bool(re.fullmatch(r'source/blackoutburst_agents/[A-Za-z0-9_]+\.py', name))
        if (not allowed or not isinstance(info, dict) or set(info) != {'size', 'sha256'}
                or type(info['size']) is not int or not 0 < info['size'] <= MAX_FILE
                or not isinstance(info['sha256'], str) or not SHA.fullmatch(info['sha256'])):
            raise PackageError('invalid_inventory')
        if not name.startswith('tools/') and info['size'] > 2 * 1024 * 1024:
            raise PackageError('source_too_large')
        total += info['size']
    if total > MAX_TOTAL:
        raise PackageError('bundle_too_large')
    return value


def manifest_id(manifest):
    validate_manifest(manifest)
    return hashlib.sha256(encoded(manifest)).hexdigest()


def load_bundle(directory):
    directory = no_symlinks(directory)
    manifest = validate_manifest(decode(read_regular(directory / 'manifest.json', maximum=256 * 1024)))
    expected = set(manifest['files']) | {'manifest.json'}
    actual = set()
    for base, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs:
            if (Path(base) / name).is_symlink():
                raise PackageError('symlink_path')
        for name in files:
            actual.add(str((Path(base) / name).relative_to(directory)))
    if actual != expected:
        raise PackageError('inventory_mismatch')
    for name, info in manifest['files'].items():
        data = read_regular(directory / name)
        if len(data) != info['size'] or hashlib.sha256(data).hexdigest() != info['sha256']:
            raise PackageError('pin_changed')
        if name.startswith('tools/'):
            elf(data, manifest['architecture'])
    return manifest


def protected(path, *, uid=0, stop_at=None):
    """Root-owned installed files and ancestors must not be agent-writable."""
    path = no_symlinks(path)
    for candidate in (path, *path.parents):
        info = candidate.lstat()
        if info.st_uid != uid or info.st_mode & 0o022:
            raise PackageError('untrusted_installation')
        if stop_at is not None and candidate == stop_at:
            break


def verify_release(directory):
    manifest = load_bundle(directory)
    if directory != RELEASES / manifest_id(manifest):
        raise PackageError('unexpected_release_directory')
    for name in ('manifest.json', *manifest['files']):
        protected(directory / name)
    python = Path(manifest['python']['path'])
    protected(python)
    data = read_regular(python)
    elf(data, manifest['architecture'])
    if hashlib.sha256(data).hexdigest() != manifest['python']['sha256']:
        raise PackageError('python_pin_changed')
    return manifest


def render_unit(template, manifest, release, username):
    if release != RELEASES / manifest_id(manifest):
        raise PackageError('unexpected_release_directory')
    values = {'@USER@': username, '@HOME@': str(home(username)), '@RELEASE@': str(release),
              '@PYTHON@': manifest['python']['path'],
              '@OTHER_HOME@': str(home(next(name for name in OWNER_NAMES if name != username)))}
    for key, value in values.items():
        template = template.replace(key, value)
    if '@' in template:
        raise PackageError('invalid_unit_template')
    return template
