"""Dated owner-private subscription observations; never execution authority."""
import hashlib
import json
import math
import re
import time

from projects import _fail

PROVIDER = 'codex_chatgpt'
SOURCE = 'codex_app_server_account_rate_limits'
TTL_SECONDS = 120
UNAVAILABLE_REASONS = frozenset({'auth_unavailable', 'unsupported_auth', 'unsupported_cli',
    'timeout', 'transport_error', 'invalid_response', 'cleanup_unconfirmed', 'process_exited'})
REACHED_REASONS = frozenset({'rate_limit_reached', 'workspace_owner_credits_depleted',
    'workspace_member_credits_depleted', 'workspace_owner_usage_limit_reached',
    'workspace_member_usage_limit_reached'})


class UsageError(ValueError):
    def __init__(self, code='invalid_usage_observation'):
        self.code = code
        super().__init__(code)


def _exact(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise UsageError()


def _identifier(value, maximum=200):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,' + str(maximum - 1) + '}', value):
        raise UsageError()
    return value


def _nullable_bool(value):
    if value is not None and type(value) is not bool:
        raise UsageError()
    return value


def _window(value):
    if value is None:
        return None
    _exact(value, ('used_percent', 'window_minutes', 'resets_at'))
    if type(value['used_percent']) is not int or not 0 <= value['used_percent'] <= 100:
        raise UsageError()
    for key in ('window_minutes', 'resets_at'):
        if value[key] is not None and (type(value[key]) is not int or not 1 <= value[key] < 2**63):
            raise UsageError()
    return dict(value)


def validate_snapshot(value):
    _exact(value, ('schema_version', 'provider', 'source', 'cli_version', 'data_status',
        'unavailable_reason', 'ordinary_usage_allowed', 'buckets'))
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise UsageError()
    if value['provider'] != PROVIDER or value['source'] != SOURCE:
        raise UsageError()
    _identifier(value['cli_version'], 80)
    if value['data_status'] not in ('observed', 'unavailable'):
        raise UsageError()
    _nullable_bool(value['ordinary_usage_allowed'])
    buckets = value['buckets']
    if type(buckets) is not list or len(buckets) > 32:
        raise UsageError()
    if value['data_status'] == 'unavailable':
        if (value['unavailable_reason'] not in UNAVAILABLE_REASONS or buckets
                or value['ordinary_usage_allowed'] is not None):
            raise UsageError()
    elif value['unavailable_reason'] is not None or not buckets:
        raise UsageError()
    seen, normalized, windows = set(), [], 0
    for bucket in buckets:
        _exact(bucket, ('limit_id', 'source_view', 'primary', 'secondary', 'reached_reason',
            'spend_control_reached', 'normal_model_slug'))
        if bucket['source_view'] not in ('multi_bucket', 'legacy'):
            raise UsageError()
        if bucket['limit_id'] is None:
            if bucket['source_view'] != 'legacy':
                raise UsageError()
        else:
            _identifier(bucket['limit_id'])
        if bucket['limit_id'] in seen:
            raise UsageError()
        seen.add(bucket['limit_id'])
        if bucket['reached_reason'] is not None and bucket['reached_reason'] not in REACHED_REASONS:
            raise UsageError()
        _nullable_bool(bucket['spend_control_reached'])
        if bucket['normal_model_slug'] is not None:
            _identifier(bucket['normal_model_slug'])
        primary, secondary = _window(bucket['primary']), _window(bucket['secondary'])
        windows += int(primary is not None) + int(secondary is not None)
        normalized.append({**bucket, 'primary': primary, 'secondary': secondary})
    if value['data_status'] == 'observed' and not windows:
        raise UsageError()
    if any(b['source_view'] == 'legacy' for b in normalized) and len(normalized) != 1:
        raise UsageError()
    return {**value, 'buckets': normalized}


def validate_time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 2**53:
        raise UsageError()
    return float(value)


def connection_identifier(endpoint, owner_id, agent_id, device_id):
    fields = {'board_endpoint': endpoint, 'owner_id': owner_id, 'agent_id': agent_id, 'device_id': device_id}
    value = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return 'codex-connection-' + hashlib.sha256(value.encode('utf-8')).hexdigest()


def migrate_usage_observations(db):
    db.execute("""CREATE TABLE usage_observations(
        connection_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
        agent_id TEXT NOT NULL REFERENCES agent_profiles(id), device_id TEXT NOT NULL REFERENCES devices(id),
        profile_version INTEGER NOT NULL, observed_at REAL NOT NULL, received_at REAL NOT NULL,
        snapshot TEXT NOT NULL)""")
    db.execute('CREATE INDEX usage_owner ON usage_observations(owner_id)')


class UsageObservationsMixin:
    def _usage_view(self, db, row, now):
        snapshot = json.loads(row['snapshot'])
        expiry = min(row['observed_at'] + TTL_SECONDS, row['received_at'] + TTL_SECONDS)
        status = snapshot['data_status']
        if status == 'observed' and now >= expiry:
            status = 'stale'
        profile = db.execute('SELECT fields FROM agent_profiles WHERE id=?', (row['agent_id'],)).fetchone()
        device = db.execute('SELECT label FROM devices WHERE id=?', (row['device_id'],)).fetchone()
        return {**{key: row[key] for key in ('connection_id', 'owner_id', 'agent_id', 'device_id',
                    'profile_version', 'observed_at', 'received_at')},
                'agent_name': json.loads(profile['fields'])['name'] if profile else '',
                'device_label': device['label'] if device else '', 'expires_at': expiry,
                'data_status': status, 'snapshot': snapshot}

    def _usage_for_owner(self, db, owner_id):
        now = time.time()
        return [self._usage_view(db, row, now) for row in db.execute(
            'SELECT * FROM usage_observations WHERE owner_id=? ORDER BY received_at DESC,connection_id', (owner_id,))]

    def report_usage_observation(self, actor, connection_id, observed_at, snapshot):
        try:
            snapshot = validate_snapshot(snapshot)
            observed_at = validate_time(observed_at)
        except (UsageError, TypeError):
            _fail('invalid_usage_observation')
        encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        with self._tx() as db:
            actor = self._actor(db, actor, 'allow_requests')
            if actor['kind'] != 'agent':
                _fail('agent_authentication_required', 403)
            profile = self._profile_for_actor(db, actor)
            expected = connection_identifier(self.base_url + '/mcp', actor['owner_id'], profile['id'], actor['device_id'])
            if type(connection_id) is not str or connection_id != expected:
                _fail('usage_connection_mismatch', 403)
            now = time.time()
            if observed_at > now + 5:
                _fail('usage_observation_in_future')
            row = db.execute('SELECT * FROM usage_observations WHERE connection_id=?', (expected,)).fetchone()
            if row and observed_at <= row['observed_at']:
                if observed_at == row['observed_at'] and encoded == row['snapshot']:
                    return self._usage_view(db, row, now)
                _fail('usage_observation_out_of_order', 409)
            db.execute("""INSERT INTO usage_observations VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(connection_id) DO UPDATE SET profile_version=excluded.profile_version,
                observed_at=excluded.observed_at,received_at=excluded.received_at,snapshot=excluded.snapshot""",
                (expected, actor['owner_id'], profile['id'], actor['device_id'], profile['version'], observed_at, now, encoded))
            return self._usage_view(db, db.execute('SELECT * FROM usage_observations WHERE connection_id=?', (expected,)).fetchone(), now)
