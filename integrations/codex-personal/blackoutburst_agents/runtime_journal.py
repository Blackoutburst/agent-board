"""Private immutable runtime identities and receipts, without model content."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import stat
import time
import uuid

from .work_journal import JournalError, _encode, _private_file, _scope


APPLICATION_ID = 0x42425254
PHASES = frozenset({'reserving', 'reserved', 'starting', 'active', 'finished', 'needs_review'})
COLUMNS = ('id','key','configuration','phase','receipt','revision','start_confirmed','created_at','updated_at')
NEXT = {'reserving': {'reserved', 'needs_review'}, 'reserved': {'starting', 'needs_review'},
    'starting': {'active', 'needs_review'}, 'active': {'finished', 'needs_review'},
    'needs_review': {'finished'}, 'finished': set()}


class RuntimeJournal:
    def __init__(self, directory):
        self.db = None
        try:
            self.directory = Path(directory).expanduser().absolute()
            self.directory.mkdir(mode=0o700, exist_ok=True)
            info = self.directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                    or info.st_uid != os.geteuid()):
                raise JournalError('insecure_runtime_directory')
            path = self.directory / 'runtime.sqlite3'
            os.close(_private_file(path))
            for suffix in ('-journal', '-wal', '-shm'):
                sidecar = Path(str(path) + suffix)
                if sidecar.exists() or sidecar.is_symlink(): os.close(_private_file(sidecar))
            self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
            self.db.row_factory = sqlite3.Row
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2): raise JournalError('unsupported_runtime_journal')
            if version == 0:
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                    raise JournalError('runtime_journal_corrupt')
            elif (self.db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                    or tuple(row['name'] for row in self.db.execute('PRAGMA table_info(executions)'))!=COLUMNS):
                raise JournalError('runtime_journal_corrupt')
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA synchronous=FULL')
            if version == 0:
                with self.transaction():
                    self.db.execute('''CREATE TABLE executions (
                        id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, configuration TEXT NOT NULL,
                        phase TEXT NOT NULL, receipt TEXT, revision INTEGER NOT NULL, start_confirmed INTEGER NOT NULL,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
                    self.db.execute(f'PRAGMA application_id={APPLICATION_ID}')
                    self.db.execute('PRAGMA user_version=1')
        except BaseException:
            self.close()
            raise

    def __enter__(self): return self
    def __exit__(self, *_): self.close()
    def close(self):
        if self.db is not None: self.db.close(); self.db = None

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            raise

    @staticmethod
    def configuration(scope, work_id, turn_id, model, *, provider='codex_chatgpt'):
        _scope(scope)
        try:
            for value in (work_id, turn_id):
                if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                    raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise JournalError('invalid_runtime_identity') from None
        if (not isinstance(model, str) or not model.strip() or len(model) > 200
                or any(ord(c) < 32 or ord(c) == 127 for c in model)):
            raise JournalError('runtime_model_required')
        if (provider not in ('codex_chatgpt', 'local_broker')
                or (provider == 'local_broker' and not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}', model))):
            raise JournalError('invalid_runtime_provider')
        return {'scope': dict(scope), 'work_id': work_id, 'turn_id': turn_id,
                'provider': provider, 'model': model}

    @staticmethod
    def key(configuration):
        return 'runtime-turn-' + hashlib.sha256(_encode(configuration, 8192, 'invalid_runtime_identity').encode()).hexdigest()

    @staticmethod
    def decode(row):
        if row is None: raise JournalError('runtime_operation_not_found')
        value = dict(row)
        try:
            config = json.loads(value['configuration'])
            if (config != RuntimeJournal.configuration(config['scope'], config['work_id'], config['turn_id'], config['model'], provider=config['provider'])
                    or value['key'] != RuntimeJournal.key(config) or value['phase'] not in PHASES
                    or type(value['revision']) is not int or value['revision'] < 1
                    or value['start_confirmed'] not in (0, 1)
                    or any(type(value[k]) not in (float,int) or not math.isfinite(value[k]) for k in ('created_at','updated_at'))
                    or str(uuid.UUID(value['id'])) != value['id']): raise ValueError
            value['configuration'] = config
            value['receipt'] = json.loads(value['receipt']) if value['receipt'] else None
            return value
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError): raise JournalError('runtime_journal_corrupt') from None

    def begin(self, configuration):
        if not isinstance(configuration,dict) or set(configuration)!={'scope','work_id','turn_id','provider','model'}:
            raise JournalError('invalid_runtime_identity')
        encoded = _encode(configuration, 8192, 'invalid_runtime_identity')
        if configuration!=self.configuration(configuration['scope'], configuration['work_id'], configuration['turn_id'], configuration['model'], provider=configuration['provider']):
            raise JournalError('invalid_runtime_identity')
        key = self.key(configuration)
        with self.transaction():
            row = self.db.execute('SELECT * FROM executions WHERE key=?', (key,)).fetchone()
            if row is not None:
                operation = self.decode(row)
                if operation['configuration'] != configuration: raise JournalError('runtime_configuration_conflict')
                return operation, False
            if configuration['provider'] == 'local_broker':
                # Old readers must refuse the provider extension explicitly,
                # rather than mistake a local-tool record for model capacity.
                self.db.execute('PRAGMA user_version=2')
            identifier, now = str(uuid.uuid4()), time.time()
            self.db.execute('INSERT INTO executions VALUES(?,?,?,?,?,?,?,?,?)',
                (identifier, key, encoded, 'reserving', None, 1, 0, now, now))
            return self.get(identifier), True

    def get(self, identifier):
        return self.decode(self.db.execute('SELECT * FROM executions WHERE id=?', (identifier,)).fetchone())

    def find(self, configuration):
        row = self.db.execute('SELECT * FROM executions WHERE key=?', (self.key(configuration),)).fetchone()
        return self.decode(row) if row else None

    def update(self, identifier, revision, phase, receipt=None, *, start_confirmed=False):
        if type(revision) is not int or revision<1: raise JournalError('invalid_revision')
        if phase not in PHASES: raise JournalError('invalid_runtime_phase')
        if type(start_confirmed) is not bool: raise JournalError('invalid_runtime_phase')
        with self.transaction():
            current = self.get(identifier)
            if current['revision'] != revision: raise JournalError('revision_conflict')
            if current['phase'] == 'finished': raise JournalError('runtime_already_finished')
            if phase != current['phase'] and phase not in NEXT[current['phase']]:
                raise JournalError('invalid_runtime_phase')
            if start_confirmed and phase != 'active': raise JournalError('invalid_runtime_phase')
            if phase=='active' and not (current['start_confirmed'] or start_confirmed):
                raise JournalError('runtime_start_unconfirmed')
            if phase=='finished' and (not isinstance(receipt,dict) or receipt.get('state')!='finished'
                    or receipt.get('cleanup_confirmed') is not True):
                raise JournalError('runtime_cleanup_unconfirmed')
            self.db.execute('UPDATE executions SET phase=?,receipt=?,revision=revision+1,start_confirmed=?,updated_at=? WHERE id=?',
                (phase, _encode(receipt, 16384, 'invalid_runtime_receipt') if receipt is not None else None,
                 int(bool(current['start_confirmed']) or start_confirmed), time.time(), identifier))
            return self.get(identifier)

    def status(self, identifier):
        operation = self.get(identifier)
        return {k: operation[k] for k in ('id', 'phase', 'revision', 'created_at', 'updated_at')}

    def list_status(self):
        return [self.status(row['id']) for row in self.db.execute('SELECT id FROM executions ORDER BY created_at,id')]
