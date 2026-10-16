"""Explicit local pins of source-backed decisions; never an authority or a search engine."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid

from .board_reader import _endpoint
from .context import ContextError, ContextScope, _hash, _identifier, _json, _select, _MESSAGE, _required, _profile, _ticket_sources
from .host_config import private_directory
from .work_journal import _private_file

MAX_SCOPES = 64
MAX_WORK_BINDINGS = 256
MAX_DECISIONS = 8
MAX_SUMMARY_BYTES = 2048
MAX_SNAPSHOT_BYTES = 20000
MAX_REVISIONS = 10
MAX_SOURCE_BYTES = 256 * 1024
_APPLICATION_ID = 0x4242444D


class DecisionMemoryError(ContextError):
    pass


def memory_scope(endpoint, scope):
    if not isinstance(scope, ContextScope):
        raise DecisionMemoryError('decision_scope_invalid')
    try:
        _endpoint(endpoint)
    except Exception:
        raise DecisionMemoryError('decision_scope_invalid') from None
    return {'board_endpoint':endpoint, **asdict(scope)}


def scope_key(scope):
    try:
        if set(scope) != {'board_endpoint','owner_id','agent_id','project_id','ticket_id'}:
            raise ValueError
        memory_scope(scope['board_endpoint'],ContextScope(**{k:scope[k] for k in ('owner_id','agent_id','project_id','ticket_id')}))
        return _hash(scope)
    except (ValueError, TypeError, KeyError, ContextError):
        raise DecisionMemoryError('decision_scope_invalid') from None


def exact_source(call, scope, thread_id, message_id):
    """Read one selected MCP message; no pagination, external URL or attachment read."""
    try:
        _identifier(thread_id);_identifier(message_id)
        response = call('get_message',{'thread_id':thread_id,'message_id':message_id})
        if not isinstance(response,dict):raise ValueError
        source = _select(response.get('message'),_MESSAGE)
        _required(source,('id','thread_id','body','author','created_at'))
        if (source['id']!=message_id or source['thread_id']!=thread_id
                or source['author'].get('kind')!='human'):
            raise ValueError
        if len(_json(source))>MAX_SOURCE_BYTES:raise ValueError
        return source
    except DecisionMemoryError:raise
    except Exception:
        raise DecisionMemoryError('decision_source_unavailable_or_changed') from None


def _uuid(value):
    if not isinstance(value,str):raise DecisionMemoryError('decision_reference_invalid')
    try:
        if str(uuid.UUID(value))!=value:raise ValueError
    except (ValueError,TypeError):raise DecisionMemoryError('decision_reference_invalid') from None
    return value


def _summary(value):
    try:
        if not isinstance(value,str) or not value.strip() or '\x00' in value or len(value.encode())>MAX_SUMMARY_BYTES:
            raise ValueError
        return value
    except (ValueError,UnicodeError):raise DecisionMemoryError('decision_summary_invalid') from None


class DecisionMemory:
    def __init__(self,directory,*,create=True):
        self.db=None
        try:
            self.directory=Path(directory).expanduser().absolute()
            if not create and (not self.directory.is_dir() or not (self.directory/'decisions.sqlite3').is_file()):
                raise DecisionMemoryError('decision_memory_missing')
            self.directory=private_directory(self.directory)
            self.path=self.directory/'decisions.sqlite3'
            os.close(_private_file(self.path))
            self.inode=self.path.stat().st_ino
            for suffix in ('-journal','-wal','-shm'):
                sidecar=Path(str(self.path)+suffix)
                if sidecar.exists() or sidecar.is_symlink():os.close(_private_file(sidecar))
            self.db=sqlite3.connect(self.path,isolation_level=None,timeout=5)
            self.db.row_factory=sqlite3.Row
            version=self.db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0,1):raise DecisionMemoryError('decision_storage_version')
            if version==0 and self.db.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                raise DecisionMemoryError('decision_storage_corrupt')
            if version==1 and self.db.execute('PRAGMA application_id').fetchone()[0]!=_APPLICATION_ID:
                raise DecisionMemoryError('decision_storage_corrupt')
            self.db.execute('PRAGMA journal_mode=DELETE');self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA secure_delete=ON')
            with self._tx() as db:
                if version==0:
                    db.execute('CREATE TABLE meta(id INTEGER PRIMARY KEY CHECK(id=1), instance_id TEXT NOT NULL)')
                    db.execute('INSERT INTO meta VALUES(1,?)',(str(uuid.uuid4()),))
                    db.execute('CREATE TABLE scopes(key TEXT PRIMARY KEY, scope TEXT NOT NULL, generation INTEGER NOT NULL, revision INTEGER NOT NULL, snapshot TEXT NOT NULL, sha256 TEXT NOT NULL)')
                    db.execute('CREATE TABLE revisions(key TEXT NOT NULL,revision INTEGER NOT NULL,snapshot TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(key,revision))')
                    db.execute('CREATE TABLE work_bindings(work_id TEXT PRIMARY KEY,scope_key TEXT NOT NULL,descriptor TEXT NOT NULL)')
                    db.execute(f'PRAGMA application_id={_APPLICATION_ID}');db.execute('PRAGMA user_version=1')
            self.instance_id=_uuid(self.db.execute('SELECT instance_id FROM meta WHERE id=1').fetchone()[0])
        except Exception as error:
            self.close()
            if isinstance(error,DecisionMemoryError):raise
            raise DecisionMemoryError('decision_storage_unavailable') from None

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
    def close(self):
        if self.db is not None:self.db.close();self.db=None

    def _ready(self):
        if self.db is None:raise DecisionMemoryError('decision_storage_closed')
        try:
            info=self.path.lstat()
            directory=self.directory.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_ino!=self.inode or info.st_uid!=os.geteuid()
                    or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o600
                    or not stat.S_ISDIR(directory.st_mode) or directory.st_uid!=os.geteuid()
                    or stat.S_IMODE(directory.st_mode)!=0o700
                    or any(p.is_symlink() for p in (self.directory,*self.directory.parents))):raise ValueError
        except (OSError,ValueError):raise DecisionMemoryError('decision_storage_replaced') from None

    @contextmanager
    def _tx(self):
        try:
            self.db.execute('BEGIN IMMEDIATE');yield self.db;self.db.execute('COMMIT')
        except BaseException as error:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            if isinstance(error,sqlite3.Error):raise DecisionMemoryError('decision_storage_unavailable') from None
            raise

    def _decode(self,row):
        try:
            if row is None:raise DecisionMemoryError('decision_memory_scope_missing')
            snapshot=json.loads(row['snapshot'])
            if (set(snapshot)!={'scope','generation','revision','decisions'}
                    or scope_key(snapshot['scope'])!=row['key']
                    or snapshot['generation']!=row['generation'] or snapshot['revision']!=row['revision']
                    or type(snapshot['generation']) is not int or snapshot['generation']<1
                    or type(snapshot['revision']) is not int or snapshot['revision']<1
                    or _hash(snapshot)!=row['sha256'] or len(_json(snapshot))>MAX_SNAPSHOT_BYTES
                    or not isinstance(snapshot['decisions'],list) or len(snapshot['decisions'])>MAX_DECISIONS):raise ValueError
            ids=set()
            for item in snapshot['decisions']:
                if set(item)!={'id','summary','source'} or _uuid(item['id']) in ids:raise ValueError
                ids.add(item['id']);_summary(item['summary'])
                source=item['source']
                if set(source)!={'tool','thread_id','message_id','sha256'} or source['tool']!='get_message':raise ValueError
                _identifier(source['thread_id']);_identifier(source['message_id'])
                if not isinstance(source['sha256'],str) or len(source['sha256'])!=64 or any(c not in '0123456789abcdef' for c in source['sha256']):raise ValueError
            return snapshot
        except DecisionMemoryError:raise
        except Exception:raise DecisionMemoryError('decision_storage_corrupt') from None

    def _row(self,key):
        self._ready()
        try:
            return self.db.execute('SELECT * FROM scopes WHERE key=?',(key,)).fetchone()
        except sqlite3.Error:raise DecisionMemoryError('decision_storage_unavailable') from None

    def inspect(self,scope):
        return self._decode(self._row(scope_key(scope)))

    def descriptor(self,scope):
        snapshot=self.inspect(scope)
        return {'instance_id':self.instance_id,'scope_key':scope_key(scope),
                'generation':snapshot['generation'],'revision':snapshot['revision'],'sha256':_hash(snapshot)}

    def require(self,scope,descriptor):
        if self.descriptor(scope)!=descriptor:raise DecisionMemoryError('decision_memory_changed')
        return self.inspect(scope)

    def require_descriptor(self,context_scope,descriptor):
        if not isinstance(context_scope,ContextScope) or not isinstance(descriptor,dict):
            raise DecisionMemoryError('decision_reference_invalid')
        key=descriptor.get('scope_key')
        if not isinstance(key,str) or len(key)!=64 or any(c not in '0123456789abcdef' for c in key):
            raise DecisionMemoryError('decision_reference_invalid')
        snapshot=self._decode(self._row(key))
        if any(snapshot['scope'][k]!=v for k,v in asdict(context_scope).items()):
            raise DecisionMemoryError('decision_scope_invalid')
        return self.require(snapshot['scope'],descriptor)

    def bind_work(self,context_scope,descriptor,work):
        self.require_descriptor(context_scope,descriptor)
        if not isinstance(work,dict) or any(work.get(k)!=v for k,v in asdict(context_scope).items()):
            raise DecisionMemoryError('decision_scope_invalid')
        work_id=_uuid(work.get('id'));encoded=_json(descriptor).decode()
        with self._tx() as db:
            old=db.execute('SELECT * FROM work_bindings WHERE work_id=?',(work_id,)).fetchone()
            if old:
                if old['scope_key']!=descriptor['scope_key'] or old['descriptor']!=encoded:
                    raise DecisionMemoryError('decision_work_context_changed')
            else:
                if work.get('provider_session_id') is not None:
                    raise DecisionMemoryError('decision_work_session_unbound')
                if db.execute('SELECT COUNT(*) FROM work_bindings').fetchone()[0]>=MAX_WORK_BINDINGS:
                    raise DecisionMemoryError('decision_memory_full')
                db.execute('INSERT INTO work_bindings VALUES(?,?,?)',(work_id,descriptor['scope_key'],encoded))
        self.require_descriptor(context_scope,descriptor)

    def _write(self,db,snapshot):
        encoded=_json(snapshot).decode();key=scope_key(snapshot['scope']);hashed=_hash(snapshot)
        if len(encoded.encode())>MAX_SNAPSHOT_BYTES:raise DecisionMemoryError('decision_memory_full')
        db.execute('INSERT INTO scopes VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET generation=excluded.generation,revision=excluded.revision,snapshot=excluded.snapshot,sha256=excluded.sha256',
                   (key,_json(snapshot['scope']).decode(),snapshot['generation'],snapshot['revision'],encoded,hashed))
        db.execute('INSERT INTO revisions VALUES(?,?,?,?)',(key,snapshot['revision'],encoded,hashed))
        db.execute('DELETE FROM revisions WHERE key=? AND revision<=?',(key,snapshot['revision']-MAX_REVISIONS))

    def pin(self,scope,*,expected_revision,summary,thread_id,message_id,call,registration,decision_id=None):
        key=scope_key(scope);_summary(summary);self._ready()
        context_scope=ContextScope(**{k:scope[k] for k in ('owner_id','agent_id','project_id','ticket_id')})
        _profile(registration,context_scope)
        _,ticket=_ticket_sources(call('get_ticket',{'ticket_id':scope['ticket_id']}),context_scope)
        if ticket['thread_id']!=thread_id:raise DecisionMemoryError('decision_source_unavailable_or_changed')
        if type(expected_revision) is not int or expected_revision<0:raise DecisionMemoryError('decision_revision_invalid')
        identifier=_uuid(decision_id) if decision_id is not None else str(uuid.uuid4())
        source=exact_source(call,scope,thread_id,message_id)
        with self._tx() as db:
            row=self._row(key)
            snapshot=self._decode(row) if row else {'scope':scope,'generation':1,'revision':0,'decisions':[]}
            matching=next((item for item in snapshot['decisions'] if item['source']['thread_id']==thread_id and item['source']['message_id']==message_id),None)
            wanted_source={'tool':'get_message','thread_id':thread_id,'message_id':message_id,'sha256':_hash(source)}
            identical=matching and matching['summary']==summary and matching['source']==wanted_source and (decision_id is None or matching['id']==decision_id)
            if identical and (snapshot['revision']==expected_revision or (
                    snapshot['revision']==expected_revision+1 and snapshot['decisions'][-1]['id']==matching['id'])):
                return {'decision_id':matching['id'],**self.descriptor(scope)}
            if snapshot['revision']!=expected_revision:raise DecisionMemoryError('decision_revision_conflict')
            if matching and matching['id']!=decision_id:raise DecisionMemoryError('decision_already_pinned')
            if row is None and db.execute('SELECT COUNT(*) FROM scopes').fetchone()[0]>=MAX_SCOPES:
                raise DecisionMemoryError('decision_memory_full')
            pins=[item for item in snapshot['decisions'] if item['id']!=identifier]
            if len(pins)>=MAX_DECISIONS:raise DecisionMemoryError('decision_memory_full')
            pins.append({'id':identifier,'summary':summary,'source':{'tool':'get_message','thread_id':thread_id,'message_id':message_id,'sha256':_hash(source)}})
            snapshot.update(revision=snapshot['revision']+1,decisions=pins)
            self._write(db,snapshot)
        return {'decision_id':identifier,**self.descriptor(scope)}

    def forget(self,scope,*,expected_revision,decision_id=None):
        key=scope_key(scope);self._ready()
        if type(expected_revision) is not int or expected_revision<1:raise DecisionMemoryError('decision_revision_invalid')
        if decision_id is not None:_uuid(decision_id)
        with self._tx() as db:
            snapshot=self._decode(self._row(key))
            if snapshot['revision']!=expected_revision:raise DecisionMemoryError('decision_revision_conflict')
            pins=[item for item in snapshot['decisions'] if item['id']!=decision_id] if decision_id else []
            if decision_id and len(pins)==len(snapshot['decisions']):raise DecisionMemoryError('decision_not_found')
            snapshot.update(generation=snapshot['generation']+1,revision=snapshot['revision']+1,decisions=pins)
            # A reset/withdrawal removes historical private pin content, while
            # retaining the generation tombstone that invalidates old caches.
            db.execute('DELETE FROM revisions WHERE key=?',(key,))
            self._write(db,snapshot)
        return self.descriptor(scope)

    def verified(self,scope,descriptor,call,thread_id):
        snapshot=self.require(scope,descriptor)
        for item in snapshot['decisions']:
            if item['source']['thread_id']!=thread_id:raise DecisionMemoryError('decision_source_unavailable_or_changed')
            source=exact_source(call,scope,item['source']['thread_id'],item['source']['message_id'])
            if _hash(source)!=item['source']['sha256']:
                raise DecisionMemoryError('decision_source_unavailable_or_changed')
        self.require(scope,descriptor)
        return snapshot
