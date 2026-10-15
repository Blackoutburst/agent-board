"""Bounded private revisions of explicitly selected board context, separate from Codex history."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .run_store import _private_file, StoreError

MAX_ARTIFACT_BYTES = 48000
MAX_REVISIONS = 10


class ContextCacheError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def validate_artifact(artifact):
    try:
        if not isinstance(artifact, dict) or artifact.get('schema') != 'blackoutburst-ticket-context-v1':
            raise ValueError
        scope = artifact['scope']
        if set(scope) != {'owner_id', 'agent_id', 'project_id', 'ticket_id'}:
            raise ValueError
        if any(not isinstance(v, str) or not v or len(v) > 256 or any(ord(c) < 32 for c in v)
               for v in scope.values()):
            raise ValueError
        encoded = canonical(artifact)
        if len(encoded.encode('utf-8')) > MAX_ARTIFACT_BYTES:
            raise ValueError
        unsigned = {k: v for k, v in artifact.items() if k != 'fingerprint'}
        fingerprint = hashlib.sha256(canonical(unsigned).encode('utf-8')).hexdigest()
        if artifact['fingerprint'] != fingerprint:
            raise ValueError
        # A digest detects changed/corrupt local data; it is not a signature or permission.
        return encoded, hashlib.sha256(canonical(scope).encode('utf-8')).hexdigest()
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ContextCacheError('invalid_context_artifact') from None


class ContextCache:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().absolute()
        self.db = None
        try:
            try:
                self.directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = self.directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise ContextCacheError('insecure_context_directory')
            path = self.directory / 'contexts.sqlite3'
            os.close(_private_file(path))
            for suffix in ('-journal', '-wal', '-shm'):
                sidecar = Path(str(path) + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    os.close(_private_file(sidecar))
            self.db = sqlite3.connect(path, timeout=5, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            if self.db.execute('PRAGMA user_version').fetchone()[0] not in (0, 1):
                raise ContextCacheError('unsupported_context_version')
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA secure_delete=ON')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS snapshots (
                    context_key TEXT NOT NULL, revision INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL, artifact_json TEXT NOT NULL,
                    created_at REAL NOT NULL, PRIMARY KEY(context_key, revision)
                );
                PRAGMA user_version=1;
            ''')
        except (OSError, sqlite3.Error, StoreError, ContextCacheError) as exc:
            self.close()
            if isinstance(exc, ContextCacheError):
                raise
            raise ContextCacheError('context_storage_unavailable') from None

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _ready(self):
        if self.db is None:
            raise ContextCacheError('context_storage_closed')

    @staticmethod
    def _key(key):
        if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{64}', key):
            raise ContextCacheError('invalid_context_key')

    def _latest(self, key):
        self._ready()
        self._key(key)
        return self.db.execute('SELECT * FROM snapshots WHERE context_key=? ORDER BY revision DESC LIMIT 1',
                               (key,)).fetchone()

    def save(self, artifact):
        encoded, key = validate_artifact(artifact)
        self._ready()
        try:
            self.db.execute('BEGIN IMMEDIATE')
            previous = self._latest(key)
            changed = previous is None or previous['fingerprint'] != artifact['fingerprint']
            revision = (previous['revision'] if previous else 0) + int(changed)
            if changed:
                self.db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?)',
                                (key, revision, artifact['fingerprint'], encoded, time.time()))
                self.db.execute('DELETE FROM snapshots WHERE context_key=? AND revision<=?',
                                (key, revision - MAX_REVISIONS))
            self.db.execute('COMMIT')
            return {'context_key': key, 'revision': revision, 'fingerprint': artifact['fingerprint'],
                    'changed': changed, 'bytes': len(encoded.encode('utf-8')),
                    'omission_count': len(artifact.get('omissions', []))}
        except sqlite3.Error:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise ContextCacheError('context_storage_unavailable') from None

    def get(self, key, fingerprint=None, revision=None):
        self._ready()
        self._key(key)
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ContextCacheError('invalid_context_revision')
        try:
            row = (self._latest(key) if revision is None else self.db.execute(
                'SELECT * FROM snapshots WHERE context_key=? AND revision=?', (key, revision)).fetchone())
            if row is None:
                raise ContextCacheError('context_not_found')
            artifact = json.loads(row['artifact_json'])
            _, calculated_key = validate_artifact(artifact)
            if calculated_key != key or row['fingerprint'] != artifact['fingerprint']:
                raise ContextCacheError('invalid_context_artifact')
            if fingerprint is not None and fingerprint != artifact['fingerprint']:
                raise ContextCacheError('context_fingerprint_changed')
            return artifact
        except (sqlite3.Error, json.JSONDecodeError):
            raise ContextCacheError('context_storage_unavailable') from None

    def forget(self, key, fingerprint):
        self._ready()
        try:
            self.db.execute('BEGIN IMMEDIATE')
            self.get(key, fingerprint=fingerprint)
            self.db.execute('DELETE FROM snapshots WHERE context_key=?', (key,))
            self.db.execute('COMMIT')
        except BaseException as exc:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            if isinstance(exc, sqlite3.Error):
                raise ContextCacheError('context_storage_unavailable') from None
            raise
