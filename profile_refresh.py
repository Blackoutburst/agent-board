"""Explicit, same-session configuration refresh after checked process cleanup.

No Runtime execution or delegation plan is ever repinned. Cleanup evidence is a
client declaration, not an OS attestation; absence of Runtime rows proves nothing.
"""
import hashlib
import json
import math
import re
import time

from projects import _fail, _json, _same_claim, _text, _version


def _opaque(value, name):
    value = _text(value, name, 1, 200)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*', value):
        _fail('Invalid ' + name)
    return value


def _compatible(before, after):
    """Conservative allowlist; every intermediate saved revision is checked."""
    ignored = {'name', 'avatar', 'description', 'version', 'updated_at', 'project_ids'}
    if {k: v for k, v in before.items() if k not in ignored} != {
            k: v for k, v in after.items() if k not in ignored}:
        return False
    old, new = before['project_ids'], after['project_ids']
    return new is None or old is not None and set(old).issubset(new)


class ProfileRefreshMixin:
    def _refresh_identity(self, db, actor, session_id):
        session_id = _opaque(session_id, 'session_id')
        raw = {k: actor.get(k) for k in ('kind', 'owner_id', 'device_id')} if isinstance(actor, dict) else actor
        actor = self._actor(db, raw, 'allow_read', session=False)
        if actor['kind'] != 'agent':
            _fail('Device credentials required', 403)
        session = db.execute('SELECT * FROM sessions WHERE id=? AND owner_id=? AND device_id=?',
            (session_id, actor['owner_id'], actor['device_id'])).fetchone()
        if not session or not session['agent_id']:
            _fail('Unknown bound session for this device', 403)
        profile = self._profile_owned(db, actor, session['agent_id'])
        if not db.execute('''SELECT 1 FROM agent_profile_bindings WHERE profile_id=?
            AND device_id=? AND agent_kind=?''', (profile['id'], actor['device_id'], session['agent_kind'])).fetchone():
            _fail('Session profile binding is unavailable', 403)
        policy = self._effective_profile_policy(db, actor, profile)
        if not policy['enabled'] or not policy['allow_read']:
            _fail('Current profile does not authorize reading', 403)
        return actor | {'session_id': session_id}, session, profile, policy

    def _refresh_plan(self, db, actor, session, profile, policy):
        rows, claims, reasons = [], [], []
        for table, kind, terminal, version in (
                ('tickets', 'ticket', 'done', 'version'), ('tasks', 'task', 'complete', 'claim_version')):
            for row in db.execute(f'''SELECT * FROM {table} WHERE
                json_extract(claimant,'$.session_id')=? AND status!=? ORDER BY id''', (session['id'], terminal)):
                claim = json.loads(row['claimant'])
                if (not _same_claim(claim, actor)
                        or claim.get('agent_id', profile['id']) != profile['id']
                        or ('profile_version' in claim and (type(claim['profile_version']) is not int
                            or claim['profile_version'] != session['profile_version']))):
                    _fail('Profile refresh claim provenance mismatch', 409)
                if kind == 'ticket':
                    project = db.execute('SELECT archived FROM projects WHERE id=?', (row['project_id'],)).fetchone()
                    authorized = (project and not project['archived']
                        and self._profile_project_allowed(db, actor, row['project_id'])
                        and db.execute('SELECT 1 FROM project_members WHERE project_id=? AND owner_id=?',
                            (row['project_id'], actor['owner_id'])).fetchone()
                        and row['assignee_owner_id'] in (None, actor['owner_id'])
                        and row['assignee_agent_id'] in (None, profile['id']))
                else:
                    authorized = (self._profile_thread_allowed(db, actor, row['thread_id'])
                        and db.execute('SELECT 1 FROM thread_members WHERE thread_id=? AND owner_id=?',
                            (row['thread_id'], actor['owner_id'])).fetchone()
                        and self._eligible(db, actor, row) and self._task_authorized(db, actor, row))
                if not authorized:
                    reasons.append('claim_authorization_changed')
                expiry = row['lease_expires_at']
                if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= time.time():
                    reasons.append('claim_expired_use_existing_recovery')
                rows.append((table, row))
                claims.append({'kind': kind, 'identifier': row['id'], 'version': row[version]})
        if not policy['allow_requests']:
            reasons.append('work_permission_unavailable')
        if db.execute('''SELECT 1 FROM runtime_executions WHERE owner_id=? AND agent_id=?
            AND (session_id=? OR ticket_id IN (SELECT id FROM tickets WHERE
                json_extract(claimant,'$.session_id')=?))
            AND (state!='finished' OR cleanup_confirmed!=1) LIMIT 1''',
            (actor['owner_id'], profile['id'], session['id'], session['id'])).fetchone():
            reasons.append('runtime_cleanup_required')
        for family in db.execute('''SELECT DISTINCT f.* FROM delegation_families f LEFT JOIN delegation_members m ON m.family_id=f.id
            WHERE f.owner_id=? AND f.device_id=? AND (f.session_id=? OR m.session_id=?)
            ''', (actor['owner_id'], actor['device_id'], session['id'], session['id'])):
            unfinished = db.execute('''SELECT 1 FROM runtime_executions WHERE
                (ticket_id=? OR ticket_id IN (SELECT ticket_id FROM delegation_members WHERE family_id=?))
                AND (state!='finished' OR cleanup_confirmed!=1) LIMIT 1''',
                (family['parent_ticket_id'], family['id'])).fetchone()
            if family['state'] in ('running', 'integrating') or unfinished:
                reasons.append('delegation_cleanup_required')
        snapshots = [json.loads(r[0]) for r in db.execute('''SELECT snapshot FROM agent_profile_versions
            WHERE profile_id=? AND version>=? AND version<=? ORDER BY version''',
            (profile['id'], session['profile_version'], profile['version']))]
        if (type(session['profile_version']) is not int or session['profile_version'] < 1
                or len(snapshots) != profile['version'] - session['profile_version'] + 1):
            _fail('Profile refresh version history is unavailable', 409)
        compatible = all(_compatible(a, b) and b['status'] == 'active' for a, b in zip(snapshots, snapshots[1:]))
        configuration = {'profile': self._profile_agent_view(profile), 'policy': policy}
        digest = hashlib.sha256(_json(configuration).encode('utf-8')).hexdigest()
        plan = {'session_id': session['id'], 'profile_id': profile['id'],
            'from_profile_version': session['profile_version'], 'to_profile_version': profile['version'],
            'configuration_sha256': digest,
            'mode': 'current' if session['profile_version'] == profile['version'] else
                'preserve_claims' if compatible else 'release_claims',
            'blocked_reasons': sorted(set(reasons)),
            # Revoked work stays private even through this narrowly scoped read.
            'claims': [] if 'claim_authorization_changed' in reasons else claims,
            **configuration,
            'instruction': 'Read the full configuration. Stop and inspect prior processes and external effects. '
                'Finish Runtime/family cleanup before refresh. Every refresh requires a new model operation; '
                'never resume an old model operation under this configuration.'}
        return plan, rows

    def inspect_profile_refresh(self, actor, session_id):
        with self._connection() as db:
            actor, session, profile, policy = self._refresh_identity(db, actor, session_id)
            return self._refresh_plan(db, actor, session, profile, policy)[0]

    def refresh_agent_session(self, actor, session_id, from_profile_version, expected_profile_version,
                              configuration_sha256, expected_claims, idempotency_key, recovery_evidence):
        _version(from_profile_version)
        _version(expected_profile_version)
        idempotency_key = _opaque(idempotency_key, 'idempotency_key')
        recovery_evidence = _text(recovery_evidence, 'recovery_evidence', 1, 20000)
        if type(configuration_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', configuration_sha256):
            _fail('Invalid configuration_sha256')
        if type(expected_claims) is not list or len(expected_claims) > 100:
            _fail('Supply at most 100 expected claims')
        claims = []
        for claim in expected_claims:
            if type(claim) is not dict or set(claim) != {'kind', 'identifier', 'version'} or claim['kind'] not in ('ticket', 'task'):
                _fail('Invalid expected claim')
            claims.append({'kind': claim['kind'], 'identifier': _opaque(claim['identifier'], 'identifier'),
                'version': _version(claim['version'])})
        claims.sort(key=lambda c: (c['kind'], c['identifier']))
        if len({(c['kind'], c['identifier']) for c in claims}) != len(claims):
            _fail('Duplicate expected claim')
        payload = dict(from_profile_version=from_profile_version, expected_profile_version=expected_profile_version,
            configuration_sha256=configuration_sha256, expected_claims=claims, recovery_evidence=recovery_evidence)
        with self._tx() as db:
            actor, session, profile, policy = self._refresh_identity(db, actor, session_id)
            if not policy['allow_requests']:
                _fail('Current profile does not authorize work', 403)
            if profile['version'] != expected_profile_version:
                _fail('Profile changed; inspect refresh again', 409)
            previous = self._retry(db, actor, 'refresh_agent_session', idempotency_key, payload)
            if previous:
                # Recheck live authorization, but never reapply changes to later claims.
                if hashlib.sha256(_json({'profile': self._profile_agent_view(profile), 'policy': policy}).encode('utf-8')).hexdigest() != configuration_sha256:
                    _fail('Configuration changed; inspect refresh again', 409)
                result = json.loads(previous)
                for claim in result['claims']:
                    if claim['kind'] == 'ticket':
                        ticket = self._ticket_member(db, actor, claim['identifier'])
                        if self._project_member(db, actor, ticket['project_id'])['archived']:
                            _fail('Project is archived', 409)
                    else:
                        task = db.execute('SELECT thread_id FROM tasks WHERE id=?', (claim['identifier'],)).fetchone()
                        if not task:
                            _fail('Task not found', 404)
                        self._member(db, actor, task['thread_id'])
                return result | {'session': self._session(session), 'profile': self._profile_agent_view(profile), 'policy': policy}
            plan, rows = self._refresh_plan(db, actor, session, profile, policy)
            if session['profile_version'] != from_profile_version or plan['mode'] == 'current':
                _fail('Session changed; inspect refresh again', 409)
            if plan['configuration_sha256'] != configuration_sha256:
                _fail('Configuration changed; inspect refresh again', 409)
            if plan['blocked_reasons']:
                _fail('Profile refresh blocked: ' + ', '.join(plan['blocked_reasons']), 409)
            if sorted(plan['claims'], key=lambda c: (c['kind'], c['identifier'])) != claims:
                _fail('Claims changed; inspect refresh again', 409)
            now, changed = time.time(), []
            for table, row in rows:
                kind = 'ticket' if table == 'tickets' else 'task'
                version = 'version' if kind == 'ticket' else 'claim_version'
                if plan['mode'] == 'preserve_claims':
                    claimant = json.loads(row['claimant'])
                    if 'profile_version' in claimant:
                        claimant['profile_version'] = profile['version']
                    db.execute(f'UPDATE {table} SET claimant=?,{version}={version}+1,updated_at=? WHERE id=?',
                        (_json(claimant), now, row['id']))
                elif kind == 'ticket':
                    status = 'todo' if row['status'] == 'in_progress' else row['status']
                    db.execute('''UPDATE tickets SET status=?,claimant=NULL,claim_started_version=NULL,
                        lease_expires_at=NULL,version=version+1,updated_at=? WHERE id=?''', (status, now, row['id']))
                else:
                    db.execute('''UPDATE tasks SET status='recovery',claimant=NULL,lease_expires_at=NULL,
                        claim_version=claim_version+1,updated_at=? WHERE id=?''', (now, row['id']))
                    db.execute('DELETE FROM resource_locks WHERE task_id=?', (row['id'],))
                if kind == 'ticket':
                    db.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, row['project_id']))
                changed.append({'kind': kind, 'identifier': row['id'], 'version': row[version] + 1})
            db.execute('UPDATE sessions SET profile_version=?,autonomous=0,last_seen_at=? WHERE id=?',
                (profile['version'], now, session['id']))
            result = {'mode': plan['mode'], 'from_profile_version': from_profile_version,
                'profile_version': profile['version'], 'claims': changed,
                'requires_new_model_operation': True}
            self._remember(db, actor, 'refresh_agent_session', idempotency_key, payload, _json(result))
            self._audit(db, actor, 'agent_session_profile_refreshed', dict(payload, mode=plan['mode'], claims=changed))
            for changed_claim in changed:
                if changed_claim['kind'] == 'ticket':
                    self._audit(db, actor, 'ticket_profile_refreshed', {'ticket_id': changed_claim['identifier'],
                        'version': changed_claim['version'], 'mode': plan['mode'], 'profile_version': profile['version']})
            session = db.execute('SELECT * FROM sessions WHERE id=?', (session['id'],)).fetchone()
            return result | {'session': self._session(session), 'profile': self._profile_agent_view(profile), 'policy': policy}
