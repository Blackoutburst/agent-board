"""Small host control journal; child journals remain the effect authorities."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
import pwd
from pathlib import Path
import sqlite3
import threading
import uuid
import urllib.parse
import ipaddress

from .host_config import HostError, digest, private_directory, validate
from .managed_host import ManagedHostJournalMixin
from .host_append_stage import HostAppendStageMixin
from .message_abandon_journal import HostMessageAbandonMixin
from .message_executed_disposition import HostExecutedDispositionMixin
from .message_append_journal import HostMessagesAppendMixin
from .work_journal import _private_file, _encode

_APPLICATION_ID = 0x4242484A


def canonical_directory():
    # Intentionally not selectable through CLI or environment variables.
    return Path(pwd.getpwuid(os.geteuid()).pw_dir) / '.local' / 'state' / 'blackoutburst-agents' / 'hosts'


class HostJournal(HostMessagesAppendMixin, HostExecutedDispositionMixin, HostMessageAbandonMixin, HostAppendStageMixin, ManagedHostJournalMixin):
    def __init__(self, directory):
        self._db = None
        self._held = False
        self._owner_lock_token = None
        self._append_active_attempt = None
        try:
            self.directory = private_directory(directory)
            database = self.directory / 'host.sqlite3'
            os.close(_private_file(database))
            for suffix in ('-journal', '-wal', '-shm'):
                path = Path(str(database) + suffix)
                if path.exists() or path.is_symlink():
                    os.close(_private_file(path))
            self._db = sqlite3.connect(database, isolation_level=None, timeout=5)
            self._db.row_factory = sqlite3.Row
            version = self._db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5):
                raise HostError('unsupported_host_version')
            if version == 0 and self._db.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                raise HostError('host_corrupt')
            if version in (1,2,3,4,5) and self._db.execute('PRAGMA application_id').fetchone()[0] != _APPLICATION_ID:
                raise HostError('host_corrupt')
            self._db.execute('PRAGMA journal_mode=DELETE')
            self._db.execute('PRAGMA synchronous=FULL')
            with self._tx() as db:
                if version == 0:
                    db.execute('CREATE TABLE control (id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL, hash TEXT NOT NULL, revision INTEGER NOT NULL, enabled INTEGER NOT NULL, activation TEXT NOT NULL, remaining INTEGER NOT NULL, reason TEXT NOT NULL, next_target INTEGER NOT NULL, instance_id TEXT NOT NULL)')
                    db.execute('CREATE TABLE targets (id TEXT PRIMARY KEY, profile_hash TEXT NOT NULL, subscription TEXT NOT NULL, receipt TEXT NOT NULL)')
                    db.execute('CREATE TABLE dispatches (batch_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, request_id TEXT NOT NULL, subscription_id TEXT NOT NULL, worker_id TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT NOT NULL, activation TEXT NOT NULL)')
                    db.execute(f'PRAGMA application_id={_APPLICATION_ID}')
                    db.execute('PRAGMA user_version=1')
            self._check()
        except Exception as error:
            if self._db is not None:
                self._db.close(); self._db = None
            if isinstance(error, HostError):
                raise
            raise HostError('host_unavailable') from None

    def _check(self):
        expected = {'control': ('id','config','hash','revision','enabled','activation','remaining','reason','next_target','instance_id'),
                    'targets': ('id','profile_hash','subscription','receipt'),
                    'dispatches': ('batch_id','target_id','request_id','subscription_id','worker_id','state','receipt','activation')}
        for table, columns in expected.items():
            if tuple(row['name'] for row in self._db.execute(f'PRAGMA table_info({table})')) != columns:
                raise HostError('host_corrupt')
        self._check_managed()
        self._check_abandonments()
        self._check_executed_dispositions()
        self._check_messages_appends()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()
    def close(self):
        if self._held: raise HostError('host_locked')
        if self._db is not None: self._db.close(); self._db = None

    @contextmanager
    def _tx(self):
        try:
            self._db.execute('BEGIN IMMEDIATE')
            yield self._db
            self._db.execute('COMMIT')
        except BaseException as error:
            if self._db.in_transaction: self._db.execute('ROLLBACK')
            if isinstance(error, sqlite3.Error): raise HostError('host_unavailable') from None
            raise

    def configure(self, configuration):
        if self._messages_append_version()==5:raise HostError('messages_append_legacy_mutation')
        configuration = validate(configuration)
        encoded, hashed = _encode(configuration, 262144, 'invalid_host_configuration'), digest(configuration)
        with self._tx() as db:
            row = db.execute('SELECT * FROM control').fetchone()
            if row:
                if row['hash'] != hashed or row['config'] != encoded: raise HostError('host_configuration_conflict')
            else:
                db.execute('INSERT INTO control VALUES(1,?,?,1,0,\'\',0,\'not_enabled\',0,?)', (encoded, hashed, str(uuid.uuid4())))
                db.executemany('INSERT INTO targets VALUES(?,\'\',\'\',\'{}\')', [(target['id'],) for target in configuration['targets']])
        return self.status()

    def control(self):
        try:
            row = self._db.execute('SELECT * FROM control WHERE id=1').fetchone()
            if row is None: raise HostError('host_not_configured')
            value = dict(row); config = validate(json.loads(row['config']))
            if (digest(config) != row['hash'] or type(row['revision']) is not int or row['revision'] < 1
                    or row['enabled'] not in (0,1) or not 0 <= row['remaining'] <= config['dispatch_budget']
                    or not 0 <= row['next_target'] < len(config['targets'])
                    or str(uuid.UUID(row['instance_id'])) != row['instance_id']): raise ValueError
            value['configuration'] = config
            value['enabled'] = bool(value['enabled'])
            del value['config']
            return value
        except HostError: raise
        except Exception: raise HostError('host_corrupt') from None

    @contextmanager
    def owner_lock(self, *, lock_directory=None):
        if self._held: raise HostError('host_busy')
        config = self.control()['configuration']
        root = private_directory(lock_directory or canonical_directory(), parents=True)
        parts = urllib.parse.urlsplit(config['board_endpoint'])
        hostname = parts.hostname.lower().rstrip('.')
        try: hostname = str(ipaddress.ip_address(hostname))
        except ValueError: hostname = hostname.encode('idna').decode('ascii')
        if ':' in hostname: hostname = '[' + hostname + ']'
        port = parts.port
        if port == (443 if parts.scheme == 'https' else 80): port = None
        endpoint = parts.scheme.lower() + '://' + hostname + (':' + str(port) if port else '') + '/mcp'
        key = digest({'owner_id':config['owner_id'], 'board_endpoint':endpoint})
        descriptor = _private_file(root / (key + '.lock'))
        try:
            try: fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise HostError('host_busy') from None
            # Pin the state root as well as locking: another --journal must not
            # forget an uncertain dispatch after the old process exits.
            binding = json.dumps({'directory':str(self.directory),
                'instance_id':self.control()['instance_id']},sort_keys=True,separators=(',',':')).encode()
            current = os.read(descriptor, 8193)
            if current and current != binding: raise HostError('host_directory_conflict')
            if not current:
                os.write(descriptor, binding); os.fsync(descriptor)
            self._held = (os.getpid(),threading.get_ident())
            self._owner_lock_token = object()
            yield
        finally:
            self._owner_lock_token = None
            self._held = False
            os.close(descriptor)

    def _require_lock(self):
        if self._held != (os.getpid(),threading.get_ident()): raise HostError('host_lock_required')

    def enable(self, expected_revision):
        self._require_lock()
        if self.managed():raise HostError('managed_target_control_required')
        with self._tx() as db:
            value = self.control()
            if type(expected_revision) is not int or value['revision'] != expected_revision:
                raise HostError('host_revision_conflict')
            if value['enabled']: raise HostError('host_already_enabled')
            if any(row['state'] == 'needs_review' or (value['reason'] == 'needs_review' and row['state'] != 'complete')
                   for row in self.dispatches()):
                raise HostError('host_needs_review')
            db.execute('UPDATE control SET enabled=1,revision=revision+1,activation=?,remaining=?,reason=\'enabled\'',
                       (str(uuid.uuid4()), value['configuration']['dispatch_budget']))
        return self.status()

    def stop(self, reason='stopped'):
        if reason not in ('stopped','needs_review','budget_exhausted','configuration_changed','authorization_changed'):
            raise HostError('invalid_host_reason')
        with self._tx() as db:
            value = self.control()
            if value['reason'] == 'needs_review' and any(row['state'] != 'complete' for row in self.dispatches()):
                reason = 'needs_review'
            self._managed_stop_all(db,reason)
            db.execute('UPDATE control SET enabled=0,revision=revision+1,reason=? WHERE enabled=1 OR reason!=?', (reason,reason))
        return self.status()

    def cancelled(self, activation):
        value = self.control()
        return not value['enabled'] or value['activation'] != activation

    def target(self, target_id):
        row = self._db.execute('SELECT * FROM targets WHERE id=?', (target_id,)).fetchone()
        if row is None: raise HostError('host_target_unknown')
        result = dict(row); result['receipt'] = json.loads(result['receipt'])
        return result

    def pin_profile(self, target_id, value):
        self._require_lock()
        self.require_no_messages_append()
        with self._tx() as db:
            old = self.target(target_id)['profile_hash']
            if old and old != value: raise HostError('host_profile_changed')
            db.execute('UPDATE targets SET profile_hash=? WHERE id=?', (value,target_id))

    def save_target(self, target_id, receipt):
        self._require_lock()
        self.require_no_messages_append()
        with self._tx() as db:
            old = self.target(target_id)
            subscription = receipt.get('subscription_id', old['subscription'])
            if old['subscription'] and subscription != old['subscription']: raise HostError('host_reference_conflict')
            db.execute('UPDATE targets SET subscription=?,receipt=? WHERE id=?',
                       (subscription,_encode(receipt,8192,'invalid_host_receipt'),target_id))

    def dispatches(self):
        rows = [dict(row) for row in self._db.execute('SELECT * FROM dispatches ORDER BY rowid')]
        for row in rows:
            try:
                if str(uuid.UUID(row['worker_id'])) != row['worker_id']:
                    raise ValueError
                row['receipt'] = json.loads(row['receipt'])
            except (TypeError, ValueError, AttributeError):
                raise HostError('host_corrupt') from None
        return rows

    def reserve_dispatch(self, target_id, batch, activation):
        self._require_lock()
        self.require_no_messages_append()
        with self._tx() as db:
            value = self.control()
            if self.cancelled(activation): raise HostError('host_stopped')
            old = next((row for row in self.dispatches() if row['batch_id'] == batch['id']), None)
            if old:
                if any(old[key] != wanted for key,wanted in {'target_id':target_id, 'request_id':batch['request_id'],
                        'subscription_id':batch['subscription_id']}.items()): raise HostError('host_reference_conflict')
                return old
            if any(row['state'] != 'complete' for row in self.dispatches()): raise HostError('host_work_pending')
            if value['remaining'] < 1: raise HostError('host_budget_exhausted')
            self._charge_managed_dispatch(db,target_id)
            # Choose the child ID before creating any child row. Recovery can
            # prove ownership even if all later refusal/stop writes fail.
            db.execute('INSERT INTO dispatches VALUES(?,?,?,?,?,\'pending\',\'{}\',?)',
                       (batch['id'], target_id, batch['request_id'], batch['subscription_id'], str(uuid.uuid4()), activation))
            db.execute('UPDATE control SET remaining=remaining-1')
        return next(row for row in self.dispatches() if row['batch_id'] == batch['id'])

    def link_worker(self, batch_id, operation_id):
        self._require_lock()
        self.require_no_messages_append()
        with self._tx() as db:
            row = next((row for row in self.dispatches() if row['batch_id'] == batch_id), None)
            if row is None or row['worker_id'] != operation_id: raise HostError('host_reference_conflict')
            db.execute('UPDATE dispatches SET worker_id=? WHERE batch_id=?', (operation_id,batch_id))

    def finish_dispatch(self, batch_id, state, receipt):
        self._require_lock()
        self.require_no_messages_append()
        if state not in ('pending','complete','needs_review'): raise HostError('invalid_host_receipt')
        with self._tx() as db:
            row = next((row for row in self.dispatches() if row['batch_id'] == batch_id), None)
            if row is None or (row['state'] == 'complete' and state != 'complete'): raise HostError('host_reference_conflict')
            if receipt.get('worker_operation_id',row['worker_id']) != row['worker_id']: raise HostError('host_reference_conflict')
            db.execute('UPDATE dispatches SET state=?,receipt=? WHERE batch_id=?',
                       (state,_encode(receipt,8192,'invalid_host_receipt'),batch_id))

    def rotate(self):
        self._require_lock()
        self.require_no_messages_append()
        with self._tx() as db:
            size = len(self.control()['configuration']['targets'])
            db.execute('UPDATE control SET next_target=(next_target+1)%?', (size,))

    def status(self):
        value = self.control()
        return {'status': 'enabled' if value['enabled'] else 'disabled', 'reason': value['reason'],
                'revision':value['revision'], 'configuration_sha256':value['hash'], 'remaining_dispatches':value['remaining'],
                'activation_id':value['activation'], 'instance_id':value['instance_id'], 'target_count':len(value['configuration']['targets']),
                'dispatches':[{key:row[key] for key in ('batch_id','target_id','worker_id','state')} for row in self.dispatches()]}
