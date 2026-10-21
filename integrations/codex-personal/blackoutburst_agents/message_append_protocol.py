"""Closed append control data; none of these hashes establishes private authority."""
from copy import deepcopy
import hashlib
import json
import uuid

from .host_config import HostError
from .server_control_protocol import _bounded, _exact, _int, entry
from .managed_host import _opaque, _sha

TOOLS = frozenset({'poll_server_messages_appends', 'prepare_server_messages_append',
    'precommit_server_messages_append', 'commit_server_messages_append',
    'complete_server_messages_append'})
PHASES = {'prepared': 2, 'precommit': 3, 'committed': 4, 'complete': 5}
PIN_FIELDS = ('source_release_sha256', 'old_configuration_sha256', 'new_configuration_sha256',
    'old_catalog_sha256', 'new_catalog_sha256', 'bindings_sha256', 'children_roster_sha256')
PROOF_FIELDS = ('schema operation_id request_id request_sha256 owner_id host_id device_id '
    'grant_version request_revision expected_host_version expected_catalog_version '
    'expected_instances_sha256 ' + ' '.join(PIN_FIELDS) +
    ' phase instance_id catalog_version instances_sha256')
REQUEST_FIELDS = ('schema expected_host_version expected_catalog_version expected_instances_sha256 '
    'grant_version profile_id profile_version profile_sha256 provider_id requested_model slot_id idempotency_key')
VIEW_FIELDS = ('schema request_id request_sha256 owner_id host_id device_id revision state reason '
    'barrier_held profile_id profile_version provider_id requested_model slot_id can_cancel instance_id '
    'proof connection_status model_catalog_status model_access_status')
SLOT_FIELDS = 'slot_id device_id session_id agent_kind identity_key profile_id profile_version'


def _fail():
    raise HostError('messages_append_protocol_invalid')


def _ascii_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest()


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            _fail()
    except (ValueError, AttributeError):
        _fail()


def proof(value):
    _exact(value, PROOF_FIELDS); _bounded(value, 8192)
    if value['schema'] != 'server-messages-append-proof-v1':
        _fail()
    _uuid(value['operation_id'])
    for key in ('request_id', 'owner_id', 'host_id', 'device_id'):
        _opaque(value[key])
    for key in ('request_sha256', 'expected_instances_sha256', *PIN_FIELDS):
        _sha(value[key])
    for key in ('grant_version', 'expected_host_version', 'expected_catalog_version'):
        _int(value[key], 1, 2**63-2)
    phase = value['phase']
    if (type(phase) is not str or phase not in PHASES or type(value['request_revision']) is not int
            or value['request_revision'] != PHASES[phase]
            or value['old_configuration_sha256'] == value['new_configuration_sha256']
            or value['old_catalog_sha256'] == value['new_catalog_sha256']):
        _fail()
    if phase in ('prepared', 'precommit'):
        if any(value[k] is not None for k in ('instance_id', 'catalog_version', 'instances_sha256')):
            _fail()
    else:
        _opaque(value['instance_id']); _sha(value['instances_sha256'])
        if type(value['catalog_version']) is not int or value['catalog_version'] != value['expected_catalog_version']+1:
            _fail()
    return deepcopy(value)


def request(value):
    _exact(value, REQUEST_FIELDS); _bounded(value, 8192)
    if value['schema'] != 'server-messages-append-request-v1' or value['provider_id'] != 'codex_chatgpt':
        _fail()
    for key in ('expected_host_version', 'expected_catalog_version', 'grant_version', 'profile_version'):
        _int(value[key], 1, 2**63-2)
    for key in ('expected_instances_sha256', 'profile_sha256', 'slot_id'):
        _sha(value[key])
    for key in ('profile_id', 'requested_model', 'idempotency_key'):
        _opaque(value[key])
    return value


def slot(value):
    _exact(value, SLOT_FIELDS)
    _sha(value['slot_id'])
    for key in ('device_id', 'session_id', 'agent_kind', 'identity_key', 'profile_id'):
        _opaque(value[key])
    _int(value['profile_version'], 1)
    if value['agent_kind'] != 'codex':
        _fail()
    if value['slot_id'] != _ascii_hash({k:v for k,v in value.items() if k != 'slot_id'}):
        _fail()
    return value


def view(value):
    _exact(value, VIEW_FIELDS); _bounded(value, 16384)
    if value['schema'] != 'server-messages-append-view-v1' or value['provider_id'] != 'codex_chatgpt':
        _fail()
    for key in ('request_id', 'owner_id', 'host_id', 'device_id', 'profile_id', 'requested_model'):
        _opaque(value[key])
    for key in ('request_sha256', 'slot_id'):
        _sha(value[key])
    _int(value['profile_version'], 1)
    phase = value['state']
    revisions = {'requested': 1, 'cancelled': 2, **PHASES}
    if (type(phase) is not str or phase not in revisions or type(value['revision']) is not int
            or value['revision'] != revisions[phase]
            or type(value['barrier_held']) is not bool or type(value['can_cancel']) is not bool
            or value['can_cancel'] is not (phase == 'requested')
            or value['barrier_held'] is not (phase not in ('complete', 'cancelled'))
            or value['reason'] != ('cancelled_by_owner' if phase == 'cancelled' else phase)):
        _fail()
    if any(value[k] != 'not_attested' for k in ('connection_status', 'model_catalog_status', 'model_access_status')):
        _fail()
    if phase in ('requested', 'cancelled'):
        if value['proof'] is not None or value['instance_id'] is not None:
            _fail()
    else:
        p = proof(value['proof'])
        if (p['phase'] != phase or p['request_revision'] != value['revision']
                or any(p[k] != value[k] for k in ('request_id', 'request_sha256', 'owner_id',
                    'host_id', 'device_id', 'instance_id'))):
            _fail()
    return value


def arguments(name, args):
    if name not in TOOLS:
        _fail()
    _bounded(args, 16384)
    if name == 'poll_server_messages_appends':
        _exact(args, 'host_id limit'); _opaque(args['host_id']); _int(args['limit'], 1, 16)
        return args
    keys = 'host_id request_id expected_revision idempotency_key '
    if name == 'prepare_server_messages_append':
        keys += 'operation_id ' + ' '.join(PIN_FIELDS)
    else:
        keys += 'proof' + (' entry' if name == 'commit_server_messages_append' else '')
    _exact(args, keys)
    for key in ('host_id', 'request_id', 'idempotency_key'):
        _opaque(args[key])
    expected = {'prepare_server_messages_append': 1, 'precommit_server_messages_append': 2,
        'commit_server_messages_append': 3, 'complete_server_messages_append': 4}[name]
    if type(args['expected_revision']) is not int or args['expected_revision'] != expected:
        _fail()
    if name == 'prepare_server_messages_append':
        _uuid(args['operation_id'])
        for key in PIN_FIELDS:
            _sha(args[key])
        if (args['old_configuration_sha256'] == args['new_configuration_sha256']
                or args['old_catalog_sha256'] == args['new_catalog_sha256']):
            _fail()
    else:
        p = proof(args['proof'])
        if (p['request_revision'] != expected or p['host_id'] != args['host_id']
                or p['request_id'] != args['request_id']):
            _fail()
        if 'entry' in args:
            entry(args['entry'])
            if args['entry']['mode'] != 'messages':
                _fail()
    return args


def response(name, value, args):
    """Validate correlation too, so a valid foreign receipt cannot advance a host."""
    arguments(name, args); _bounded(value, 65536)
    if name == 'poll_server_messages_appends':
        _exact(value, 'schema host_id requests')
        if (value['schema'] != 'server-messages-append-poll-v1' or value['host_id'] != args['host_id']
                or type(value['requests']) is not list or len(value['requests']) > args['limit']):
            _fail()
        ids = set()
        for item in value['requests']:
            _exact(item, 'request body slot')
            v = view(item['request']); b = request(item['body']); s = slot(item['slot'])
            if (v['host_id'] != args['host_id'] or v['request_id'] in ids
                    or v['state'] in ('cancelled', 'complete')
                    or any(v[k] != b[k] for k in ('profile_id', 'profile_version', 'provider_id', 'requested_model', 'slot_id'))
                    or any(s[k] != b[k] for k in ('profile_id', 'profile_version', 'slot_id'))):
                _fail()
            if v['request_sha256'] != _ascii_hash(dict(purpose='messages_append_v1',
                    owner_id=v['owner_id'], host_id=v['host_id'], device_id=v['device_id'], body=b)):
                _fail()
            if v['proof'] is not None and any(v['proof'][k] != b[k] for k in
                    ('grant_version', 'expected_host_version', 'expected_catalog_version', 'expected_instances_sha256')):
                _fail()
            ids.add(v['request_id'])
    else:
        p = proof(value)
        phase = {'prepare_server_messages_append': 'prepared', 'precommit_server_messages_append': 'precommit',
            'commit_server_messages_append': 'committed', 'complete_server_messages_append': 'complete'}[name]
        if p['host_id'] != args['host_id'] or p['request_id'] != args['request_id'] or p['phase'] != phase:
            _fail()
        before = args if name == 'prepare_server_messages_append' else args['proof']
        preserved = ('operation_id', *PIN_FIELDS)
        if name != 'prepare_server_messages_append':
            preserved += ('request_sha256', 'owner_id', 'device_id', 'grant_version',
                'expected_host_version', 'expected_catalog_version', 'expected_instances_sha256')
        if any(p[k] != before[k] for k in preserved):
            _fail()
        if name == 'complete_server_messages_append' and any(p[k] != before[k]
                for k in ('instance_id', 'catalog_version', 'instances_sha256')):
            _fail()
    return deepcopy(value)
