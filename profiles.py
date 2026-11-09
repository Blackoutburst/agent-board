"""Human-owned agent profiles and explicit device bindings; no execution engine."""
from __future__ import annotations

import json
import secrets
import time

from projects import _fail, _json, _text, _version
from profile_refresh import ProfileRefreshMixin
from owner_powers import has_owner_power

TEXT_LIMITS = {'name': 200, 'avatar': 32, 'description': 4000, 'role': 200,
               'instructions': 12000, 'language': 32, 'response_style': 2000,
               'preferred_provider': 100, 'preferred_model': 200}
DEFAULT_FIELDS = {key: '' for key in TEXT_LIMITS}
DEFAULT_FIELDS.update(language='auto')
DEFAULT_POLICY = {'allow_read': False, 'allow_messages': False, 'allow_requests': False,
                  'actions': [], 'resources': [], 'autonomy': 'responses'}
LEGACY_POLICY = {'allow_read': True, 'allow_messages': True, 'allow_requests': True,
                 'actions': ['*'], 'resources': ['*'], 'autonomy': 'queue'}
COMPANION_POLICY = {'allow_read': True, 'allow_messages': True, 'allow_requests': True,
                    'actions': [], 'resources': [], 'autonomy': 'requested', 'allow_create_projects': False}


def _insert_profile(db, identifier, owner_id, name, *, legacy=False, now=None, kind='agent'):
    now = now or time.time()
    values = {**DEFAULT_FIELDS, 'name': name}
    db.execute('''INSERT INTO agent_profiles(id,owner_id,fields,policy,project_ids,status,version,legacy,created_at,updated_at)
        VALUES(?,?,?,?,?,'active',1,?,?,?)''', (identifier, owner_id, _json(values),
        _json(LEGACY_POLICY if legacy else DEFAULT_POLICY), 'null' if legacy else '[]', int(legacy), now, now))
    if kind == 'companion':
        db.execute("UPDATE agent_profiles SET kind='companion' WHERE id=?", (identifier,))
    _record_version(db, identifier)


def _record_version(db, identifier, *, replace_initial=False):
    row = db.execute('SELECT * FROM agent_profiles WHERE id=?', (identifier,)).fetchone()
    snapshot = {**dict(row), **json.loads(row['fields']), 'policy': json.loads(row['policy']),
                'project_ids': json.loads(row['project_ids']), 'legacy': bool(row['legacy'])}
    snapshot.pop('fields')
    # Schema19 must not turn an ordinary profile's next cosmetic edit into an
    # incompatible refresh merely by adding default identity metadata.
    if snapshot.get('kind', 'agent') == 'agent':
        snapshot.pop('kind', None)
    else:
        snapshot['acts_for'] = row['owner_id']
    verb = 'INSERT OR REPLACE' if replace_initial else 'INSERT'
    db.execute(f'{verb} INTO agent_profile_versions VALUES(?,?,?)', (identifier, row['version'], _json(snapshot)))


def migrate_profiles(db):
    for statement in (
        '''CREATE TABLE agent_profiles(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES owners(id),
            fields TEXT NOT NULL,policy TEXT NOT NULL,project_ids TEXT NOT NULL,status TEXT NOT NULL,
            version INTEGER NOT NULL,legacy INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL,updated_at REAL NOT NULL)''',
        '''CREATE TABLE agent_profile_bindings(profile_id TEXT NOT NULL REFERENCES agent_profiles(id),
            device_id TEXT NOT NULL REFERENCES devices(id),identity_key TEXT NOT NULL,agent_kind TEXT NOT NULL,
            created_at REAL NOT NULL,PRIMARY KEY(device_id,agent_kind,identity_key))''',
        '''CREATE TABLE agent_profile_versions(profile_id TEXT NOT NULL REFERENCES agent_profiles(id),
            version INTEGER NOT NULL,snapshot TEXT NOT NULL,PRIMARY KEY(profile_id,version))''',
        'CREATE INDEX profiles_owner ON agent_profiles(owner_id,status)',
        'ALTER TABLE devices ADD COLUMN profiles_managed INTEGER NOT NULL DEFAULT 0',
        'ALTER TABLE sessions ADD COLUMN profile_version INTEGER',
    ):
        db.execute(statement)
    for row in db.execute('SELECT * FROM agent_identities').fetchall():
        _insert_profile(db, row['id'], row['owner_id'], row['name'], legacy=True, now=row['created_at'])
        db.execute('INSERT INTO agent_profile_bindings VALUES(?,?,?,?,?)',
                   (row['id'], row['device_id'], row['identity_key'], row['agent_kind'], row['created_at']))
    # Old read_inbox/register_session clients also receive a pinned identity.
    # Every formerly valid session remains valid; none becomes an unscoped path.
    for device in db.execute('SELECT DISTINCT d.* FROM devices d JOIN sessions s ON s.device_id=d.id WHERE s.agent_id IS NULL').fetchall():
        binding = db.execute("SELECT profile_id FROM agent_profile_bindings WHERE device_id=? AND agent_kind='agent' AND identity_key='default'", (device['id'],)).fetchone()
        identifier = binding['profile_id'] if binding else secrets.token_hex(16)
        if not binding:
            _insert_profile(db, identifier, device['owner_id'], 'Agent · ' + device['label'], legacy=True)
            db.execute("INSERT INTO agent_profile_bindings VALUES(?,?,'default','agent',?)", (identifier, device['id'], time.time()))
        db.execute('UPDATE sessions SET agent_id=? WHERE device_id=? AND agent_id IS NULL', (identifier, device['id']))
    db.execute('UPDATE sessions SET profile_version=1 WHERE agent_id IS NOT NULL')
    db.execute('UPDATE devices SET profiles_managed=1 WHERE id IN (SELECT device_id FROM agent_profile_bindings)')


class ProfilesMixin(ProfileRefreshMixin):
    def _agent_presence(self, db, profile_id):
        last_seen = db.execute('''SELECT MAX(s.last_seen_at) FROM sessions s
            JOIN devices d ON d.id=s.device_id
            WHERE s.agent_id=? AND d.revoked_at IS NULL''', (profile_id,)).fetchone()[0]
        online = last_seen is not None and last_seen > time.time() - 900
        return {'presence': 'online' if online else 'idle' if last_seen is not None else 'offline',
                'online': online, 'last_seen_at': last_seen, 'idle_after_seconds': 900}

    def _profile_owned(self, db, actor, profile_id):
        row = db.execute('SELECT * FROM agent_profiles WHERE id=? AND owner_id=?', (profile_id, actor['owner_id'])).fetchone()
        if not row:
            _fail('Agent profile not found', 404)
        return row

    def _profile_public(self, row):
        fields = json.loads(row['fields'])
        return {'id': row['id'], 'owner_id': row['owner_id'], 'name': fields['name'],
                'avatar': fields['avatar'], 'status': row['status'], 'version': row['version'],
                'kind': row['kind'], 'acts_for': row['owner_id'] if row['kind'] == 'companion' else None}

    def _profile(self, db, row):
        bindings = [dict(item) for item in db.execute('''SELECT b.device_id,d.label AS device_label,b.identity_key,
            b.agent_kind,b.created_at,d.revoked_at IS NOT NULL AS revoked FROM agent_profile_bindings b
            JOIN devices d ON d.id=b.device_id WHERE b.profile_id=? ORDER BY b.created_at,b.device_id,b.agent_kind,b.identity_key''', (row['id'],))]
        for binding in bindings:
            binding['revoked'] = bool(binding['revoked'])
        active = []
        for table, kind in (('tickets', 'ticket'), ('tasks', 'task')):
            for item in db.execute(f'''SELECT t.id,t.title,t.status,t.lease_expires_at,
                json_extract(t.claimant,'$.session_id') AS session_id,s.profile_version FROM {table} t JOIN sessions s
                ON s.id=json_extract(t.claimant,'$.session_id') AND s.device_id=json_extract(t.claimant,'$.device_id')
                WHERE s.agent_id=? AND t.claimant IS NOT NULL AND t.status NOT IN ('done','complete')''', (row['id'],)):
                active.append({'kind': kind, **dict(item)})
        return {**dict(row), **json.loads(row['fields']), 'acts_for': row['owner_id'] if row['kind'] == 'companion' else None,
                'policy': json.loads(row['policy']),
                'project_ids': json.loads(row['project_ids']), 'legacy': bool(row['legacy']),
                'bindings': bindings, 'active_work': active,
                **self._agent_presence(db, row['id'])}

    def _profile_view(self, db, row):
        result = self._profile(db, row)
        result.pop('fields', None)
        return result

    def _profile_agent_view(self, row):
        # Runtime configuration only. Human connection/claim management can
        # contain work from a scope that was revoked after an earlier run.
        result = {**dict(row), **json.loads(row['fields']), 'acts_for': row['owner_id'] if row['kind'] == 'companion' else None,
                  'policy': json.loads(row['policy']),
                  'project_ids': json.loads(row['project_ids']), 'legacy': bool(row['legacy'])}
        result.pop('fields')
        return result

    def _profile_values(self, db, actor, changes):
        if not isinstance(changes, dict) or set(changes) - (set(TEXT_LIMITS) | {'policy', 'project_ids', 'status'}):
            _fail('Supply only editable agent profile fields')
        result = {}
        for key, value in changes.items():
            if key in TEXT_LIMITS:
                value = _text(value, key, 1 if key == 'name' else 0, TEXT_LIMITS[key]).strip()
                if any(ord(char) < 32 and char not in '\n\t' for char in value) or '\x7f' in value:
                    _fail('Profile text contains invalid control characters')
                if key == 'avatar' and any(char in value for char in ('/', ':', '<', '>', '\\', '\n', '\t')):
                    _fail('Avatar must be short text or emoji, not a URL or markup')
                if key == 'language' and value not in ('auto', 'fr', 'en', 'de', 'nl'):
                    _fail('Language must be auto, fr, en, de, or nl')
                result[key] = value
            elif key == 'status':
                if value not in ('active', 'paused', 'archived'):
                    _fail('Profile status must be active, paused, or archived')
                result[key] = value
            elif key == 'project_ids':
                if not isinstance(value, list) or len(value) > 100 or any(not isinstance(item, str) or not 1 <= len(item) <= 200 for item in value):
                    _fail('project_ids must contain at most 100 project IDs')
                value = list(dict.fromkeys(value))
                for identifier in value:
                    self._project_member(db, actor, identifier)
                result[key] = value
            elif key == 'policy':
                if (not isinstance(value, dict) or not set(DEFAULT_POLICY).issubset(value)
                        or set(value) - (set(DEFAULT_POLICY) | {'allow_create_projects'})):
                    _fail('Supply every profile policy field')
                if (any(type(value[key]) is not bool for key in ('allow_read', 'allow_messages', 'allow_requests'))
                        or ('allow_create_projects' in value and type(value['allow_create_projects']) is not bool)):
                    _fail('Profile permission switches must be booleans')
                if value['autonomy'] not in ('responses', 'requested', 'queue'):
                    _fail('Profile autonomy must be responses, requested, or queue')
                policy = dict(value)
                for field in ('actions', 'resources'):
                    items = value[field]
                    if not isinstance(items, list) or len(items) > 100 or any(not isinstance(item, str) or not item.strip() or len(item) > 256 for item in items):
                        _fail('Profile actions and resources must contain at most 100 nonempty strings')
                    policy[field] = list(dict.fromkeys(items))
                result[key] = policy
        return result

    def list_agent_profiles(self, actor, include_archived=False):
        if type(include_archived) is not bool:
            _fail('include_archived must be a boolean')
        with self._connection() as db:
            actor = self._human(db, actor)
            return [self._profile_view(db, row) for row in db.execute('SELECT * FROM agent_profiles WHERE owner_id=? AND (?=1 OR status!=?) ORDER BY created_at,id',
                    (actor['owner_id'], int(include_archived), 'archived'))]

    def get_agent_profile(self, actor, profile_id):
        with self._connection() as db:
            actor = self._human(db, actor)
            return self._profile_view(db, self._profile_owned(db, actor, profile_id))

    def get_agent_profile_version(self, actor, profile_id, version):
        _version(version)
        with self._connection() as db:
            actor = self._human(db, actor)
            self._profile_owned(db, actor, profile_id)
            row = db.execute('SELECT snapshot FROM agent_profile_versions WHERE profile_id=? AND version=?', (profile_id, version)).fetchone()
            if not row:
                _fail('Agent profile version not found', 404)
            return json.loads(row['snapshot'])

    def create_agent_profile(self, actor, name, idempotency_key='', kind='agent', **fields):
        if kind not in ('agent', 'companion'):
            _fail('Invalid agent profile kind')
        with self._tx() as db:
            actor = self._human(db, actor)
            values = self._profile_values(db, actor, {'name': name, **fields})
            if kind == 'companion':
                values.setdefault('project_ids', None)
                values['policy'] = dict(values.get('policy', COMPANION_POLICY))
                if values['policy'].get('allow_create_projects') is True:
                    _fail('Companions cannot create projects', 403)
                values['policy']['allow_create_projects'] = False
            payload = values | ({'kind': kind} if kind == 'companion' else {})
            previous = self._retry(db, actor, 'create_agent_profile', idempotency_key, payload)
            if previous:
                return self._profile_view(db, self._profile_owned(db, actor, previous))
            if kind == 'companion' and db.execute("SELECT 1 FROM agent_profiles WHERE owner_id=? AND kind='companion'",
                                                (actor['owner_id'],)).fetchone():
                _fail('This owner already has a companion; resume its existing identity', 409)
            identifier = secrets.token_hex(16)
            _insert_profile(db, identifier, actor['owner_id'], values['name'], kind=kind)
            self._write_profile(db, identifier, values, increment=False)
            self._remember(db, actor, 'create_agent_profile', idempotency_key, payload, identifier)
            self._audit(db, actor, 'agent_profile_created', {'profile_id': identifier})
            return self._profile_view(db, self._profile_owned(db, actor, identifier))

    def _write_profile(self, db, identifier, values, *, increment=True):
        if values.get('status') in ('active', 'paused') and db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hierarchy_team_profiles'").fetchone():
            if db.execute('SELECT 1 FROM hierarchy_team_profiles WHERE profile_id=? AND released_at IS NOT NULL',
                          (identifier,)).fetchone():
                _fail('hierarchy_archived_reentry_requires_accounting', 409)
        if values.get('status') in ('active', 'paused') and db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hierarchy_receipts'").fetchone():
            if db.execute("SELECT 1 FROM hierarchy_receipts WHERE operation='archive_project_lead' "
                          "AND json_extract(receipt,'$.profile_id')=?", (identifier,)).fetchone():
                _fail('hierarchy_archived_reentry_requires_accounting', 409)
        row = db.execute('SELECT * FROM agent_profiles WHERE id=?', (identifier,)).fetchone()
        fields = {**json.loads(row['fields']), **{key: value for key, value in values.items() if key in TEXT_LIMITS}}
        previous_policy = json.loads(row['policy'])
        policy = dict(values.get('policy', previous_policy))
        # Older clients replace the six original policy fields. Omission of an
        # additive grant must neither revoke nor recreate its stored value.
        if 'allow_create_projects' not in policy and 'allow_create_projects' in previous_policy:
            policy['allow_create_projects'] = previous_policy['allow_create_projects']
        db.execute('UPDATE agent_profiles SET fields=?,policy=?,project_ids=?,status=?,version=version+?,updated_at=? WHERE id=?',
                   (_json(fields), _json(policy),
                    _json(values.get('project_ids', json.loads(row['project_ids']))), values.get('status', row['status']), int(increment), time.time(), identifier))
        # Initial construction is one transaction; published versions are never rewritten.
        _record_version(db, identifier, replace_initial=not increment)

    def update_agent_profile(self, actor, profile_id, expected_version, **fields):
        _version(expected_version)
        if not fields:
            _fail('Supply at least one profile change')
        with self._tx() as db:
            actor = self._human(db, actor)
            row = self._profile_owned(db, actor, profile_id)
            if row['version'] != expected_version:
                _fail('Agent profile changed; reload before saving', 409)
            values = self._profile_values(db, actor, fields)
            self._write_profile(db, profile_id, values)
            self._audit(db, actor, 'agent_profile_updated', {'profile_id': profile_id, 'version': expected_version + 1, 'fields': sorted(fields)})
            return self._profile_view(db, self._profile_owned(db, actor, profile_id))

    def duplicate_agent_profile(self, actor, profile_id, expected_version, name='', idempotency_key=''):
        _version(expected_version)
        name = _text(name, 'name', 0, TEXT_LIMITS['name']).strip()
        with self._tx() as db:
            actor = self._human(db, actor)
            source = self._profile_owned(db, actor, profile_id)
            payload = {'profile_id': profile_id, 'expected_version': expected_version, 'name': name}
            previous = self._retry(db, actor, 'duplicate_agent_profile', idempotency_key, payload)
            if previous:
                return self._profile_view(db, self._profile_owned(db, actor, previous))
            if source['version'] != expected_version:
                _fail('Agent profile changed; reload before duplicating', 409)
            fields = json.loads(source['fields'])
            fields['name'] = name or (fields['name'][:190] + ' (copy)')
            fields = self._profile_values(db, actor, fields)
            identifier = secrets.token_hex(16)
            _insert_profile(db, identifier, actor['owner_id'], fields['name'])
            self._write_profile(db, identifier, {**fields, 'status': 'paused'}, increment=False)
            self._remember(db, actor, 'duplicate_agent_profile', idempotency_key, payload, identifier)
            self._audit(db, actor, 'agent_profile_duplicated', {'profile_id': identifier, 'source_id': profile_id})
            return self._profile_view(db, self._profile_owned(db, actor, identifier))

    def bind_agent_profile(self, actor, profile_id, expected_version, device_id, identity_key, agent_kind='agent'):
        _version(expected_version)
        identity_key = _text(identity_key, 'identity_key', 1, 200).strip()
        agent_kind = self._normalize_agent_kind(agent_kind)
        with self._tx() as db:
            actor = self._human(db, actor)
            profile = self._profile_owned(db, actor, profile_id)
            if profile['status'] == 'archived':
                _fail('Reactivate the profile before binding a device', 409)
            device = db.execute('SELECT * FROM devices WHERE id=? AND owner_id=? AND revoked_at IS NULL', (device_id, actor['owner_id'])).fetchone()
            if not device:
                _fail('Device not found', 404)
            previous = db.execute('SELECT * FROM agent_profile_bindings WHERE device_id=? AND agent_kind=? AND identity_key=?', (device_id, agent_kind, identity_key)).fetchone()
            if previous:
                if previous['profile_id'] != profile_id:
                    _fail('This device identity is already bound to another profile', 409)
                return self._profile_view(db, profile)
            if profile['version'] != expected_version:
                _fail('Agent profile changed; reload before binding', 409)
            db.execute('INSERT INTO agent_profile_bindings VALUES(?,?,?,?,?)', (profile_id, device_id, identity_key, agent_kind, time.time()))
            db.execute('UPDATE devices SET profiles_managed=1 WHERE id=?', (device_id,))
            self._write_profile(db, profile_id, {})
            self._audit(db, actor, 'agent_profile_bound', {'profile_id': profile_id, 'device_id': device_id, 'agent_kind': agent_kind})
            return self._profile_view(db, self._profile_owned(db, actor, profile_id))

    def unbind_agent_profile(self, actor, profile_id, expected_version, device_id, identity_key, agent_kind):
        """Remove one device/client/key connection; the device credential is kept."""
        _version(expected_version)
        identity_key = _text(identity_key, 'identity_key', 1, 200).strip()
        agent_kind = self._normalize_agent_kind(agent_kind)
        with self._tx() as db:
            actor = self._human(db, actor)
            profile = self._profile_owned(db, actor, profile_id)
            binding = db.execute('''SELECT b.* FROM agent_profile_bindings b JOIN devices d ON d.id=b.device_id
                WHERE b.profile_id=? AND b.device_id=? AND b.agent_kind=? AND b.identity_key=? AND d.owner_id=?''',
                (profile_id, device_id, agent_kind, identity_key, actor['owner_id'])).fetchone()
            if not binding:
                _fail('Connection not found', 404)
            if profile['version'] != expected_version:
                _fail('Agent profile changed; reload before removing a connection', 409)
            for table in ('tickets', 'tasks'):
                active = db.execute(f'''SELECT 1 FROM {table} t JOIN sessions s
                    ON s.id=json_extract(t.claimant,'$.session_id') AND s.device_id=json_extract(t.claimant,'$.device_id')
                    WHERE s.agent_id=? AND s.device_id=? AND s.agent_kind=? AND t.claimant IS NOT NULL
                    AND t.status NOT IN ('done','complete') LIMIT 1''', (profile_id, device_id, agent_kind)).fetchone()
                if active:
                    _fail('This connection has active work; reconcile or release it before removing the connection', 409)
            db.execute('DELETE FROM agent_profile_bindings WHERE profile_id=? AND device_id=? AND agent_kind=? AND identity_key=?',
                       (profile_id, device_id, agent_kind, identity_key))
            # The version bump forces sessions on this connection to register
            # again; the device stays profiles_managed, so that registration is
            # refused instead of silently creating a legacy profile.
            self._write_profile(db, profile_id, {})
            self._audit(db, actor, 'agent_profile_unbound', {'profile_id': profile_id, 'device_id': device_id, 'agent_kind': agent_kind})
            return self._profile_view(db, self._profile_owned(db, actor, profile_id))

    @staticmethod
    def _normalize_agent_kind(value):
        value = value.strip().lower().replace('_', '-') if isinstance(value, str) else value
        if value == 'claude':
            value = 'claude-code'
        return _text(value, 'agent_kind', 1, 100)

    def _binding_profile(self, db, actor, identity_key, agent_kind, name=''):
        binding = db.execute('SELECT profile_id FROM agent_profile_bindings WHERE device_id=? AND agent_kind=? AND identity_key=?',
                             (actor['device_id'], agent_kind, identity_key)).fetchone()
        if binding:
            return self._profile_owned(db, actor, binding['profile_id'])
        device = db.execute('SELECT * FROM devices WHERE id=?', (actor['device_id'],)).fetchone()
        if device['profiles_managed'] or db.execute("""SELECT 1 FROM agent_profile_bindings b
                JOIN agent_profiles p ON p.id=b.profile_id WHERE b.device_id=? AND p.kind='companion'""",
                (actor['device_id'],)).fetchone():
            _fail('This device identity is not bound to a profile; ask its owner to create an explicit binding', 403)
        identifier = secrets.token_hex(16)
        titles = {'codex': 'Codex', 'claude-code': 'Claude Code', 'agent': 'Agent'}
        _insert_profile(db, identifier, actor['owner_id'], name or titles.get(agent_kind, agent_kind) + ' · ' + device['label'], legacy=True)
        db.execute('INSERT INTO agent_profile_bindings VALUES(?,?,?,?,?)', (identifier, actor['device_id'], identity_key, agent_kind, time.time()))
        db.execute('UPDATE devices SET profiles_managed=1 WHERE id=?', (actor['device_id'],))
        return self._profile_owned(db, actor, identifier)

    def _profile_for_actor(self, db, actor):
        if actor['kind'] != 'agent':
            return None
        return db.execute('SELECT p.* FROM agent_profiles p JOIN sessions s ON s.agent_id=p.id WHERE s.id=? AND s.device_id=? AND p.owner_id=?',
                          (actor.get('session_id'), actor['device_id'], actor['owner_id'])).fetchone()

    def _profile_authorize(self, db, actor, permission=None):
        profile = self._profile_for_actor(db, actor)
        if not profile:
            _fail('Register a bound agent profile for this session first', 403)
        if profile['status'] != 'active':
            _fail('Agent profile is paused or archived; active claims require human reconciliation', 403)
        session = db.execute('SELECT profile_version FROM sessions WHERE id=?', (actor['session_id'],)).fetchone()
        if session['profile_version'] != profile['version']:
            _fail('Agent profile changed; explicitly register again before continuing', 409)
        if profile['kind'] == 'companion' and not db.execute("""SELECT 1 FROM agent_profile_bindings b
                JOIN sessions s ON s.agent_id=b.profile_id AND s.device_id=b.device_id AND s.agent_kind=b.agent_kind
                WHERE s.id=? AND b.profile_id=? AND b.device_id=?""",
                (actor['session_id'], profile['id'], actor['device_id'])).fetchone():
            _fail('Companion profile binding is unavailable', 403)
        policy = json.loads(profile['policy'])
        mapped = 'allow_messages' if permission == 'allow_reply' else permission
        if mapped and (not policy.get(mapped) or (mapped == 'allow_requests' and policy['autonomy'] == 'responses')):
            _fail('Agent profile does not authorize this operation', 403)
        from hierarchy_access import profile_ceiling
        profile_ceiling(db, self, actor, profile, permission)
        return profile

    def _profile_project_allowed(self, db, actor, project_id):
        if actor['kind'] == 'human' or has_owner_power(actor, 'dispatch'):
            return True
        profile = self._profile_for_actor(db, actor)
        if not profile:
            return False
        from hierarchy_access import project_allowed
        return project_allowed(db, self, actor, profile, project_id)

    def _profile_thread_allowed(self, db, actor, thread_id, *, allow_standalone_invitation=True):
        if actor['kind'] == 'human' or has_owner_power(actor, 'dispatch'):
            return True
        profile = self._profile_for_actor(db, actor)
        if not profile:
            return False
        project_ids = json.loads(profile['project_ids'])
        if project_ids is None:
            return True
        ticket = db.execute('SELECT project_id FROM tickets WHERE thread_id=?', (thread_id,)).fetchone()
        if not ticket:
            from hierarchy_access import creation_delegation
            if creation_delegation(db, profile) is not None:
                return False
            return allow_standalone_invitation and self._profile_standalone_thread_invited(db, profile, thread_id)
        return bool(ticket and self._profile_project_allowed(db, actor, ticket['project_id']) and db.execute(
            'SELECT 1 FROM project_members WHERE project_id=? AND owner_id=?', (ticket['project_id'], actor['owner_id'])).fetchone())

    @staticmethod
    def _profile_standalone_thread_invited(db, profile, thread_id):
        # An invitation is an owner decision for this conversation only. The
        # ticket path above never falls back to a standalone invitation.
        if not db.execute('SELECT 1 FROM thread_members WHERE thread_id=? AND owner_id=?',
                          (thread_id, profile['owner_id'])).fetchone():
            return False
        return bool(db.execute('''SELECT 1 FROM audit a JOIN messages x
            ON x.id=json_extract(a.detail,'$.message_id')
            WHERE a.event='standalone_agent_invited' AND x.thread_id=?
            AND json_extract(a.detail,'$.thread_id')=x.thread_id
            AND json_extract(a.detail,'$.agent_id')=? AND json_extract(a.actor,'$.owner_id')=?
            LIMIT 1''', (thread_id, profile['id'], profile['owner_id'])).fetchone())

    @staticmethod
    def _profile_can_create_projects(profile):
        policy = json.loads(profile['policy'])
        # Unscoped legacy profiles already had creation authority. Scoped
        # profiles require a dedicated owner grant; explicit false always wins.
        return policy.get('allow_create_projects', json.loads(profile['project_ids']) is None)

    def _effective_profile_policy(self, db, actor, profile):
        owner = json.loads(self._owner(db, actor['owner_id'])['policy'])
        policy = json.loads(profile['policy'])
        result = dict(owner)
        result['enabled'] = owner['enabled'] and profile['status'] == 'active'
        for field, profile_field in (('allow_read', 'allow_read'), ('allow_reply', 'allow_messages'), ('allow_requests', 'allow_requests')):
            result[field] = owner[field] and policy[profile_field]
        result['allow_requests'] = result['allow_requests'] and policy['autonomy'] != 'responses'
        # Preserve unchanged policy snapshots for existing pinned clients.
        # Explicit grants/revocations already require a new profile version.
        if 'allow_create_projects' in policy:
            result['allow_create_projects'] = (result['enabled'] and result['allow_requests']
                                               and self._profile_can_create_projects(profile))
        for field in ('actions', 'resources'):
            result[field] = owner[field] if profile['legacy'] and policy[field] == ['*'] else [item for item in owner[field] if item in policy[field]]
        result['autonomy'] = policy['autonomy']
        from hierarchy_access import profile_ceiling, creation_delegation
        from hierarchy_store import HierarchyStore
        from board import BoardError
        try:
            ceiling = profile_ceiling(db, self, actor, profile)
        except BoardError:
            # Policy inspection also serves human reconciliation and stale
            # assignment displays. Return a closed ceiling, not an exception
            # hiding historical work; actor authorization still rejects it.
            result.update(enabled=False, allow_read=False, allow_reply=False,
                          allow_requests=False, actions=[], resources=[], autonomy='responses')
            if 'allow_create_projects' in result:
                result['allow_create_projects'] = False
            return result
        if ceiling is not None:
            for field in ('actions', 'resources'):
                result[field] = sorted(set(result[field]) & set(ceiling[field]))
            result['autonomy'] = ceiling['autonomy']
            for field, source in (('allow_read', 'allow_read'), ('allow_reply', 'allow_messages'),
                                  ('allow_requests', 'allow_requests')):
                for row in HierarchyStore(self)._chain(db, profile['owner_id'],
                        creation_delegation(db, profile)):
                    ancestor = json.loads(db.execute('SELECT policy FROM agent_profiles WHERE id=?',
                                                     (row['beneficiary_id'],)).fetchone()[0])
                    result[field] = result[field] and ancestor[source]
            result['allow_requests'] = result['allow_requests'] and ceiling['autonomy'] != 'responses'
        return result

    def _profile_session_claims(self, db, session_id):
        return any(db.execute(f"SELECT 1 FROM {table} WHERE json_extract(claimant,'$.session_id')=? AND status NOT IN ('done','complete')", (session_id,)).fetchone() for table in ('tickets', 'tasks'))

    def _register_profile_session(self, db, actor, profile, session_id, label, capabilities, skills, agent_kind, autonomous, *, seen=True):
        _text(session_id, 'session_id', 1, 200)
        _text(label, 'label', 1, 200)
        if profile['status'] != 'active':
            _fail('Agent profile is paused or archived', 403)
        policy = self._effective_profile_policy(db, actor, profile)
        if not policy['allow_read']:
            _fail('Agent profile does not authorize reading', 403)
        granted = json.loads(db.execute('SELECT capabilities FROM devices WHERE id=?', (actor['device_id'],)).fetchone()[0])
        if not set(capabilities).issubset(granted):
            _fail('Session capabilities exceed device grant', 403)
        row = db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
        if row:
            if row['device_id'] != actor['device_id'] or row['owner_id'] != actor['owner_id']:
                _fail('Session ID belongs to another device', 409)
            if row['agent_id'] != profile['id']:
                _fail('A registered session cannot change its agent profile', 409)
            if row['profile_version'] != profile['version'] and self._profile_session_claims(db, session_id):
                _fail('Profile changed during active work; call inspect_profile_refresh and explicitly '
                      'refresh_agent_session after cleanup, or reconcile its claims before registering again', 409)
            db.execute("""UPDATE sessions SET label=?,capabilities=?,skills=?,agent_kind=?,autonomous=?,
                last_seen_at=?,profile_version=? WHERE id=?""", (label.strip(), _json(capabilities), _json(skills), agent_kind,
                int(autonomous and policy['autonomy'] == 'queue' and policy['allow_requests']), time.time() if seen else row['last_seen_at'], profile['version'], session_id))
        else:
            db.execute("""INSERT INTO sessions(id,owner_id,device_id,label,capabilities,created_at,last_seen_at,skills,
                agent_kind,autonomous,agent_id,profile_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id,actor['owner_id'],actor['device_id'],label.strip(),_json(capabilities),time.time(),time.time() if seen else None,
                 _json(skills),agent_kind,int(autonomous and policy['autonomy'] == 'queue' and policy['allow_requests']),profile['id'],profile['version']))
