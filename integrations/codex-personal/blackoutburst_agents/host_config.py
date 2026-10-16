"""Private, immutable configuration for a finite explicitly enabled host."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

from .board_reader import _connection_file, _endpoint, _json
from .event_journal import _configuration
from .work_journal import JournalError, _encode, _identifier, _scope

HostError = JournalError
MAX_BYTES = 256 * 1024


def digest(value):
    return hashlib.sha256(_encode(value, MAX_BYTES, 'invalid_host_configuration').encode()).hexdigest()


def private_directory(path, *, parents=False):
    path = Path(path).expanduser().absolute()
    # Do not follow any symlink ancestor (including an absent child's parent).
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise HostError('insecure_host_path')
    if parents and not path.parent.exists():
        private_directory(path.parent, parents=True)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise HostError('insecure_host_directory')
    return path


def private_bytes(path, maximum=MAX_BYTES):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > maximum):
            raise HostError('insecure_host_file')
        result = bytearray()
        while len(result) <= maximum:
            part = os.read(descriptor, min(8192, maximum + 1 - len(result)))
            if not part:
                return bytes(result)
            result.extend(part)
        raise HostError('host_file_too_large')
    except OSError:
        raise HostError('host_file_unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def connection_digest(path):
    return hashlib.sha256(private_bytes(path, 65536)).hexdigest()


def scope_for(config, target):
    return {'owner_id': config['owner_id'], 'board_endpoint': config['board_endpoint'],
            **{key: target[key] for key in ('agent_id', 'project_id', 'ticket_id', 'board_session_id')}}


def worker_configuration(target):
    value = {key: target[key] for key in ('instruction', 'workspace', 'executable', 'model', 'timeout')}
    value['runtime_managed'] = True
    if target.get('human_owner_id') is not None:
        value['human_owner_id'] = target['human_owner_id']
    if 'routing' in target:
        value['routing'] = target['routing']
    _configuration(value)
    return value


def validate(config, *, bind_connections=False):
    """Normalize once on configure; later validate without reading credentials."""
    if isinstance(config,dict) and type(config.get('schema_version')) is int and config['schema_version']==2:
        from .message_config import validate as validate_messages
        return validate_messages(config,bind_connections=bind_connections)
    try:
        required = {'schema_version', 'owner_id', 'board_endpoint', 'interval_seconds',
                    'max_pages', 'page_limit', 'dispatch_budget', 'targets'}
        if not isinstance(config, dict) or set(config) != required or type(config['schema_version']) is not int or config['schema_version'] != 1:
            raise ValueError
        result = json.loads(_encode(config, MAX_BYTES, 'invalid_host_configuration'))
        _identifier(result['owner_id'], 'invalid_host_configuration')
        _endpoint(result['board_endpoint'])
        for key, low, high in (('interval_seconds', 5, 300), ('max_pages', 1, 4),
                               ('page_limit', 1, 200), ('dispatch_budget', 1, 20)):
            if type(result[key]) is not int or not low <= result[key] <= high:
                raise ValueError
        if not isinstance(result['targets'], list) or not 1 <= len(result['targets']) <= 16:
            raise ValueError
        seen_ids, seen_scopes, profiles = set(), set(), {}
        for target in result['targets']:
            fields = {'id', 'agent_id', 'project_id', 'ticket_id', 'board_session_id', 'profile_version',
                      'connection_file', 'instruction', 'workspace', 'executable', 'model', 'timeout', 'after'}
            optional = {'human_owner_id', 'routing', 'connection_sha256', 'instruction_sha256', 'routing_sha256'}
            if not isinstance(target, dict) or not fields <= set(target) or set(target) - fields - optional:
                raise ValueError
            for key in ('id', 'agent_id', 'project_id', 'ticket_id', 'board_session_id'):
                _identifier(target[key], 'invalid_host_configuration')
            if type(target['profile_version']) is not int or target['profile_version'] < 1:
                raise ValueError
            if type(target['after']) is not int or not 0 <= target['after'] < 2 ** 63:
                raise ValueError
            for key in ('workspace', 'executable', 'connection_file'):
                path = Path(target[key]).expanduser()
                if not path.is_absolute() or path.is_symlink():
                    raise ValueError
                if bind_connections:
                    path = path.resolve(strict=True)
                target[key] = str(path)
            if bind_connections:
                if (not Path(target['workspace']).is_dir() or not Path(target['executable']).is_file()
                        or not os.access(target['executable'], os.X_OK)):
                    raise ValueError
                connection = _connection_file(target['connection_file'])
                if connection['mcp_url'] != result['board_endpoint']:
                    raise ValueError
                target['connection_sha256'] = connection_digest(target['connection_file'])
            if target['model'] is None and 'routing' not in target:
                raise ValueError
            worker_configuration(target)
            if 'routing' in target and target['routing']['policy']['owner_id'] != result['owner_id']:
                raise ValueError
            expected = {'instruction_sha256': digest(target['instruction']),
                        'routing_sha256': digest(target.get('routing'))}
            for key, value in expected.items():
                if not bind_connections and target.get(key) != value:
                    raise ValueError
                target[key] = value
            if (not isinstance(target.get('connection_sha256'), str)
                    or len(target['connection_sha256']) != 64
                    or any(c not in '0123456789abcdef' for c in target['connection_sha256'])):
                raise ValueError
            scope_json, _ = _scope(scope_for(result, target))
            if target['id'] in seen_ids or scope_json in seen_scopes:
                raise ValueError
            seen_ids.add(target['id']); seen_scopes.add(scope_json)
            # A persistent profile uses one fixed connection/session/version.
            profile = tuple(target[key] for key in ('board_session_id', 'profile_version', 'connection_sha256'))
            if profiles.setdefault(target['agent_id'], profile) != profile:
                raise ValueError
        return result
    except HostError:
        raise
    except Exception:
        raise HostError('invalid_host_configuration') from None


def load(path):
    try:
        return validate(_json(private_bytes(path), 'invalid_host_configuration'), bind_connections=True)
    except HostError:
        raise
    except Exception:
        raise HostError('invalid_host_configuration') from None
