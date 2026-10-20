"""Bounded observations of exact queued Stops, never execution/cleanup proof.

The real Host owner lock is required. This helper performs only SQLite reads:
it neither applies commands nor acknowledges/reports them, opens no connection,
and does not inspect configuration paths. Callers retain their own transaction.
Settled historical rows are not adopted or changed. A current remote Stop and
its cleanup still require the concrete controller's separate authenticated work.
"""
import json

from .host_config import HostError, digest
from .managed_host import _opaque, _sha, _TABLES
from .message_abandon_protocol import guard_sql
from .server_control_protocol import arguments as validate_arguments

MAX_COMMANDS = 16
MAX_EFFECTS = 32
MAX_JSON = 4096
MAX_OUTPUT = 192 * 1024
TABLES = ('control', 'targets', 'dispatches', *_TABLES)


def _fail(kind='invalid'):
    raise HostError('stopped_controls_' + kind)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False)


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: _fail()
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if type(value) is not dict or _canonical(value) != raw: _fail()
    return value


def _integer(value, maximum=2**53-1):
    if type(value) is not int or not 0 <= value <= maximum: _fail()


def _rows(db, table, fields, maximum, where='1', *, empty=()):
    """Bound values in SQLite, before materializing even a corrupted text row."""
    expressions = []
    for key, size in fields:
        if size is None:
            expressions.append(f"CASE WHEN typeof({key})='integer' THEN {key} END AS {key}")
        else:
            low = 0 if key in empty else 1
            expressions.append(f"CASE WHEN typeof({key})='text' AND length(CAST({key} AS BLOB))"
                               f" BETWEEN {low} AND {size} THEN {key} END AS {key}")
    rows = db.execute('SELECT ' + ','.join(expressions) + ' FROM main.' + table
                      + ' WHERE ' + where + ' ORDER BY rowid LIMIT ?', (maximum+1,)).fetchall()
    if len(rows) > maximum: _fail('bounds')
    result = [dict(row) for row in rows]
    if any(value is None for row in result for value in row.values()): _fail()
    return result


def _stamp(db):
    return tuple(db.execute('PRAGMA main.' + name).fetchone()[0] for name in
                 ('user_version', 'application_id', 'schema_version', 'data_version'))


def stopped_control_rows(host):
    """Return raw pending Stop rows in stable ID order, without settling any.

    At most one queued Stop per target and one ACK/report per Stop is accepted.
    The return contains raw JSON strings, suitable for the caller's exact CAS
    fingerprint. It is observation data, not authority or a received remote ACK.
    """
    try: host._require_lock()
    except Exception: _fail('lock')
    try:
        db = host._db
        initial = _stamp(db); changes = db.total_changes; transaction = db.in_transaction
        if initial[0] not in (2, 3, 4) or initial[1] != 0x4242484A: _fail('schema')
        guard_sql(db, TABLES)
        for name, columns in _TABLES.items():
            if tuple(row['name'] for row in db.execute('PRAGMA main.table_info('+name+')')) != columns:
                _fail('schema')
        settings = _rows(db, 'managed_settings', (('host_id', 800), ('catalog_sha256', 64)), 1)
        if len(settings) != 1: _fail('schema')
        host_id = _opaque(settings[0]['host_id']); _sha(settings[0]['catalog_sha256'])
        targets = _rows(db, 'managed_targets', (('target_id', 800), ('instance_id', 800),
            ('launchspec_sha256', 64), ('generation', None)), MAX_COMMANDS, empty=('instance_id',))
        if not targets or len({row['target_id'] for row in targets}) != len(targets): _fail('schema')
        by_target = {}
        for target in targets:
            _opaque(target['target_id'])
            if target['instance_id']: _opaque(target['instance_id'])
            _sha(target['launchspec_sha256']); _integer(target['generation'])
            by_target[target['target_id']] = target
        commands = _rows(db, 'managed_commands', (('id', 800), ('payload', MAX_JSON),
            ('payload_sha256', 64), ('target_id', 800), ('generation', None), ('action', 16), ('state', 16)),
            MAX_COMMANDS, "typeof(state)!='text' OR state NOT IN ('applied','superseded')")
        selected = {}; seen_targets = set()
        for row in commands:
            _opaque(row['id']); _opaque(row['target_id']); _sha(row['payload_sha256']); _integer(row['generation'])
            payload = _decode(row['payload'])
            if (set(payload) != {'id','target_id','instance_id','generation','action','launchspec_sha256','budget'}
                    or row['state'] != 'queued' or row['action'] != 'stop' or payload['action'] != 'stop'
                    or any(payload[k] != row[k] for k in ('id','target_id','generation','action'))
                    or type(payload['generation']) is not int or type(payload['budget']) is not int
                    or payload['budget'] != 0 or row['payload_sha256'] != digest(payload)
                    or row['target_id'] in seen_targets or row['id'] in selected): _fail()
            _opaque(payload['instance_id']); _sha(payload['launchspec_sha256'])
            target = by_target.get(row['target_id'])
            if (target is None or payload['instance_id'] != target['instance_id']
                    or payload['launchspec_sha256'] != target['launchspec_sha256']
                    or row['generation'] < target['generation']): _fail()
            seen_targets.add(row['target_id']); selected[row['id']] = payload
        effects = _rows(db, 'managed_effects', (('id', 800), ('method', 64), ('arguments', MAX_JSON),
            ('payload_sha256', 64), ('state', 16), ('result', 2)), MAX_EFFECTS,
            "typeof(state)!='text' OR state NOT IN ('complete','superseded')")
        seen_effects = set()
        for row in effects:
            _opaque(row['id']); _sha(row['payload_sha256'])
            if row['state'] != 'pending' or row['result'] != '{}': _fail()
            method = row['method']; args = _decode(row['arguments'])
            if method not in ('ack_instance_command', 'report_instance_state'): _fail()
            validate_arguments(method, args)
            command = selected.get(args['command_id'])
            marker = (method, args['command_id'])
            if (command is None or marker in seen_effects or args['host_id'] != host_id
                    or row['payload_sha256'] != digest(args)): _fail()
            if method == 'report_instance_state' and (
                    args['state'] != 'stopped' or args['cleanup_confirmed'] is not True
                    or any(args[k] != command[k] for k in ('instance_id','generation','launchspec_sha256'))): _fail()
            seen_effects.add(marker)
        result = {'commands': sorted(commands, key=lambda row: row['id']),
                  'effects': sorted(effects, key=lambda row: row['id'])}
        if len(_canonical(result).encode('ascii')) > MAX_OUTPUT: _fail('bounds')
        if (_stamp(db) != initial or db.total_changes != changes or db.in_transaction != transaction):
            _fail()
        return result
    except HostError as error:
        if error.code.startswith('stopped_controls_'): raise
        _fail('schema' if error.code == 'message_abandon_schema' else 'invalid')
    except Exception:
        _fail()
