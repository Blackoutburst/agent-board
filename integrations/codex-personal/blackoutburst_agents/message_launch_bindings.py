"""Pure consistency of versioned message launch origins, never launch authority.

Inputs must separately be authenticated and checked against the live owner,
filesystem, Board and journal by a future caller. No IO or migration occurs here.
"""
from __future__ import annotations

import hashlib
import json
import math
from urllib.parse import urlsplit

SCHEMA = 'messages-launch-bindings-v1'
MAX_CONFIGURATION_BYTES = 256 * 1024
MAX_ORIGIN_BYTES = 4 * 1024 * 1024
MAX_STATE_BYTES = MAX_ORIGIN_BYTES + MAX_CONFIGURATION_BYTES
MAX_TARGETS = 16
MAX_NODES = 50000
CONFIG_KEYS = frozenset('schema_version owner_id board_endpoint interval_seconds max_pages page_limit dispatch_budget targets'.split())
TARGET_KEYS = frozenset('id agent_id board_session_id profile_version connection_file instruction workspace executable model timeout thread_ids start_at connection_sha256 instruction_sha256'.split())
ORIGIN_KEYS = frozenset('kind operation_id configuration_sha256 configuration'.split())
BINDING_KEYS = frozenset('kind operation_id target_id target_sha256 origin_configuration_sha256 source_release_sha256 launchspec_sha256'.split())
STATE_KEYS = frozenset('schema configuration_sha256 source_release_sha256 origins bindings origins_sha256 bindings_sha256 state_sha256'.split())
ENTRY_KEYS = frozenset('catalog_key profile_id profile_version launchspec_sha256 mode'.split())


class BindingConsistencyError(ValueError):
    def __init__(self):
        super().__init__('message_launch_bindings_inconsistent')


def _fail():
    raise BindingConsistencyError()


def _exact(value, fields):
    if type(value) is not dict or any(type(key) is not str for key in value) or set(value) != fields:
        _fail()


def _canonical(value, maximum):
    """Bound the tree and strings before collecting canonical UTF-8 chunks."""
    nodes = 0
    characters = 0
    def visit(item, depth=0):
        nonlocal nodes, characters
        nodes += 1
        if nodes > MAX_NODES or depth > 20:
            _fail()
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    _fail()
                visit(key, depth+1); visit(child, depth+1)
        elif type(item) is list:
            for child in item:
                visit(child, depth+1)
        elif type(item) is str:
            characters += len(item)
            if len(item) > MAX_CONFIGURATION_BYTES or characters > maximum:
                _fail()
        elif type(item) is float:
            if not math.isfinite(item):
                _fail()
        elif item is not None and type(item) not in (int, bool):
            _fail()
    try:
        visit(value)
        parts = []; size = 0
        for chunk in json.JSONEncoder(sort_keys=True, ensure_ascii=False, allow_nan=False,
                separators=(',', ':')).iterencode(value):
            data = chunk.encode('utf-8'); size += len(data)
            if size > maximum:
                _fail()
            parts.append(data)
        return b''.join(parts)
    except (ValueError, TypeError, RecursionError, UnicodeError, OverflowError):
        _fail()


def _sha(value):
    if type(value) is not str or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        _fail()


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        _fail()


def _text(value, maximum, *, allow_lines=False, byte_limit=None):
    if type(value) is not str or not value or len(value) > maximum or '\0' in value:
        _fail()
    if not allow_lines and any(ord(c) < 32 or ord(c) == 127 for c in value):
        _fail()
    try:
        if len(value.encode('utf-8')) > (byte_limit if byte_limit is not None else maximum*4):
            _fail()
    except UnicodeError:
        _fail()


def _operation(value):
    # Canonical UUID text without UUID generation or host discovery imports.
    if type(value) is not str or len(value) != 36:
        _fail()
    for index, char in enumerate(value):
        if (index in (8,13,18,23) and char != '-') or (index not in (8,13,18,23) and char not in '0123456789abcdef'):
            _fail()


def _digest(value, maximum=MAX_CONFIGURATION_BYTES):
    return hashlib.sha256(_canonical(value, maximum)).hexdigest()


def structural_configuration(value):
    """Detached structural data only: no path/credential/existence validation."""
    _exact(value, CONFIG_KEYS)
    _integer(value['schema_version'], 2, 2)
    _text(value['owner_id'], 256)
    endpoint = value['board_endpoint']; _text(endpoint, 2048)
    try:
        parts = urlsplit(endpoint); port = parts.port
        if (any(c.isspace() for c in endpoint) or parts.username is not None or parts.password is not None
                or parts.path != '/mcp' or parts.query or parts.fragment or '?' in endpoint or '#' in endpoint
                or '\\' in endpoint or not parts.hostname or (port is not None and not 1 <= port <= 65535)
                or not (parts.scheme == 'https' or (parts.scheme == 'http' and parts.hostname in ('localhost','127.0.0.1','::1')))):
            _fail()
    except ValueError:
        _fail()
    for field, low, high in (('interval_seconds',5,300),('max_pages',1,4),('page_limit',1,200),('dispatch_budget',1,20)):
        _integer(value[field],low,high)
    targets = value['targets']
    if type(targets) is not list or not 1 <= len(targets) <= MAX_TARGETS:
        _fail()
    identifiers = {field:set() for field in ('id','agent_id','board_session_id')}
    for target in targets:
        _exact(target, TARGET_KEYS)
        for field in identifiers:
            _text(target[field],256)
            if target[field] in identifiers[field]:
                _fail()
            identifiers[field].add(target[field])
        _integer(target['profile_version'],1,2**63-1)
        for field in ('workspace','connection_file','executable'):
            _text(target[field],4096,byte_limit=4096)
            if not target[field].startswith('/'):
                _fail()
        _text(target['instruction'],12000,allow_lines=True,byte_limit=12000)
        _text(target['model'],200)
        if not target['instruction'].strip() or not target['model'].strip():
            _fail()
        timeout = target['timeout']
        if type(timeout) not in (int,float) or not 1 <= timeout <= 300 or not math.isfinite(timeout):
            _fail()
        if type(target['start_at']) is not str or target['start_at'] not in ('now','beginning'):
            _fail()
        threads = target['thread_ids']
        if type(threads) is not list or not 1 <= len(threads) <= 32:
            _fail()
        for thread in threads:
            _text(thread,256)
        if len(set(threads)) != len(threads):
            _fail()
        _sha(target['connection_sha256']); _sha(target['instruction_sha256'])
        if target['instruction_sha256'] != _digest(target['instruction']):
            _fail()
    return json.loads(_canonical(value, MAX_CONFIGURATION_BYTES))


def _launch(target, origin, release):
    # Byte-for-byte v1 formula. The existing launch_catalog remains unchanged.
    return _digest({'release':release,'configuration':_digest(origin),'target':target})


def _entry(target, launchspec):
    return dict(catalog_key=target['id'],profile_id=target['agent_id'],profile_version=target['profile_version'],
        mode='messages',launchspec_sha256=launchspec)


def _catalog(entries):
    if type(entries) is not list or not 1 <= len(entries) <= MAX_TARGETS:
        _fail()
    seen = set(); profiles = set()
    for row in entries:
        _exact(row, ENTRY_KEYS)
        for field in ('catalog_key','profile_id'):
            _text(row[field],256)
        _integer(row['profile_version'],1,2**63-1); _sha(row['launchspec_sha256'])
        if type(row['mode']) is not str or row['mode'] != 'messages' or row['catalog_key'] in seen or row['profile_id'] in profiles:
            _fail()
        seen.add(row['catalog_key']); profiles.add(row['profile_id'])
    return _canonical(sorted(entries,key=lambda r:r['catalog_key']),65536)


def _append(previous, candidate):
    if (len(candidate['targets']) != len(previous['targets'])+1
            or _canonical({k:v for k,v in candidate.items() if k!='targets'},MAX_CONFIGURATION_BYTES) !=
               _canonical({k:v for k,v in previous.items() if k!='targets'},MAX_CONFIGURATION_BYTES)
            or _canonical(candidate['targets'][:-1],MAX_CONFIGURATION_BYTES) != _canonical(previous['targets'],MAX_CONFIGURATION_BYTES)):
        _fail()
    added = candidate['targets'][-1]
    if added['start_at'] != 'now' or any(added[key] == old[key] for old in previous['targets'] for key in ('workspace','connection_file')):
        _fail()


def _manifest(origins):
    return [{key:row[key] for key in ('kind','operation_id','configuration_sha256')} for row in origins]


def _state(origins, bindings, release):
    value = dict(schema=SCHEMA, configuration_sha256=origins[-1]['configuration_sha256'],source_release_sha256=release,
        origins=origins,bindings=bindings,origins_sha256=_digest(_manifest(origins)),bindings_sha256=_digest(bindings))
    value['state_sha256'] = _digest({k:v for k,v in value.items() if k not in ('origins','bindings')})
    return value


def _binding(kind, operation_id, target, origin, release):
    return dict(kind=kind,operation_id=operation_id,target_id=target['id'],target_sha256=_digest(target),
        origin_configuration_sha256=_digest(origin),source_release_sha256=release,launchspec_sha256=_launch(target,origin,release))


def check_binding_set(value, *, current_configuration, expected_release_sha256):
    """Consistency only. The caller's expected release is not verified here."""
    _sha(expected_release_sha256); _exact(value,STATE_KEYS)
    current = structural_configuration(current_configuration)
    _sha(value['source_release_sha256'])
    if type(value['schema']) is not str or value['schema'] != SCHEMA or value['source_release_sha256'] != expected_release_sha256:
        _fail()
    origins = value['origins']; bindings = value['bindings']
    if type(origins) is not list or not 1 <= len(origins) <= MAX_TARGETS or type(bindings) is not list or len(bindings) != len(current['targets']):
        _fail()
    operations = set(); hashes = set(); expected_bindings=[]; normalized=[]; total=0
    previous = None
    for index, origin in enumerate(origins):
        _exact(origin,ORIGIN_KEYS); _operation(origin['operation_id']); _sha(origin['configuration_sha256'])
        if type(origin['kind']) is not str or origin['kind'] != ('anchor' if index==0 else 'append'):
            _fail()
        config = structural_configuration(origin['configuration'])
        encoded = _canonical(config,MAX_CONFIGURATION_BYTES); total += len(encoded)
        if total > MAX_ORIGIN_BYTES or origin['operation_id'] in operations or origin['configuration_sha256'] in hashes:
            _fail()
        if _digest(config) != origin['configuration_sha256']:
            _fail()
        if previous is not None:
            _append(previous,config)
        newborn = config['targets'] if index==0 else config['targets'][-1:]
        expected_bindings.extend(_binding(origin['kind'],origin['operation_id'],target,config,expected_release_sha256) for target in newborn)
        operations.add(origin['operation_id']); hashes.add(origin['configuration_sha256'])
        normalized.append(dict(kind=origin['kind'],operation_id=origin['operation_id'],configuration_sha256=origin['configuration_sha256'],configuration=config))
        previous = config
    if _canonical(previous,MAX_CONFIGURATION_BYTES) != _canonical(current,MAX_CONFIGURATION_BYTES):
        _fail()
    for binding in bindings:
        _exact(binding,BINDING_KEYS)
    expected = _state(normalized,expected_bindings,expected_release_sha256)
    # Canonical equality distinguishes bool/int and 1/1.0, even in nested data.
    if _canonical(value,MAX_STATE_BYTES) != _canonical(expected,MAX_STATE_BYTES):
        _fail()
    return json.loads(_canonical(expected,MAX_STATE_BYTES))


def seed_legacy_anchor(configuration, *, expected_release_sha256, expected_predecessor_entries, anchor_operation_id):
    """Label the current multi-target predecessor, never invent an old birth."""
    _sha(expected_release_sha256); _operation(anchor_operation_id)
    config = structural_configuration(configuration)
    derived = [_entry(t,_launch(t,config,expected_release_sha256)) for t in config['targets']]
    if _catalog(derived) != _catalog(expected_predecessor_entries):
        _fail()
    origin = dict(kind='anchor',operation_id=anchor_operation_id,configuration_sha256=_digest(config),configuration=config)
    bindings = [_binding('anchor',anchor_operation_id,t,config,expected_release_sha256) for t in config['targets']]
    return check_binding_set(_state([origin],bindings,expected_release_sha256),current_configuration=config,expected_release_sha256=expected_release_sha256)


def binding_catalog(value, *, current_configuration, expected_release_sha256):
    state = check_binding_set(value,current_configuration=current_configuration,expected_release_sha256=expected_release_sha256)
    config = structural_configuration(current_configuration)
    return [_entry(t,b['launchspec_sha256']) for t,b in zip(config['targets'],state['bindings'])]


def append_binding(value, candidate, *, previous_configuration, expected_release_sha256,
                   expected_predecessor_entries, append_operation_id):
    """Calculate detached data, not a journal transition or executable request."""
    previous = structural_configuration(previous_configuration)
    state = check_binding_set(value,current_configuration=previous,expected_release_sha256=expected_release_sha256)
    _operation(append_operation_id)
    if append_operation_id in {o['operation_id'] for o in state['origins']}:
        _fail()
    if _catalog(binding_catalog(state,current_configuration=previous,expected_release_sha256=expected_release_sha256)) != _catalog(expected_predecessor_entries):
        _fail()
    config = structural_configuration(candidate); _append(previous,config)
    origin = dict(kind='append',operation_id=append_operation_id,configuration_sha256=_digest(config),configuration=config)
    origins = [*state['origins'],origin]
    bindings = [*state['bindings'],_binding('append',append_operation_id,config['targets'][-1],config,expected_release_sha256)]
    return check_binding_set(_state(origins,bindings,expected_release_sha256),current_configuration=config,expected_release_sha256=expected_release_sha256)
