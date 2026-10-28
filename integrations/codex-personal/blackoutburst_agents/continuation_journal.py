"""New logical-task metadata journal. Never opens or adopts execution journals.

Events are append-only and committed with their idempotency key in one SQLite
transaction. A lost acknowledgement rereads metadata; it never launches work.
There is no configured maximum event/cycle count; reads are bounded pages.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import os
from pathlib import Path
import re
import sqlite3
import threading

from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, canonical, digest, identity

MAX_DOCUMENT=64*1024
APPLICATION_ID=0x4242434E
SQL={
    'task': 'CREATE TABLE task (id INTEGER PRIMARY KEY CHECK(id=1), configuration TEXT NOT NULL, configuration_sha256 TEXT NOT NULL)',
    'events': 'CREATE TABLE events (revision INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE CHECK(length(request_id)<=200), request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64), previous_sha256 TEXT NOT NULL CHECK(length(previous_sha256)=64), state_json TEXT NOT NULL CHECK(length(state_json)<=65536), event_sha256 TEXT NOT NULL CHECK(length(event_sha256)=64))',
    'events_no_update': "CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append_only'); END",
    'task_no_update': "CREATE TRIGGER task_no_update BEFORE UPDATE ON task BEGIN SELECT RAISE(ABORT,'append_only'); END",
    'task_no_delete': "CREATE TRIGGER task_no_delete BEFORE DELETE ON task BEGIN SELECT RAISE(ABORT,'append_only'); END",
    'events_no_delete': "CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append_only'); END",
}


class ContinuationError(DevelopmentError):pass


def require(condition,code='continuation_invalid'):
    if not condition:raise ContinuationError(code)


def identifier(value):
    require(type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}',value) is not None)
    return value


def encoded(value):
    raw=canonical(value);require(0<len(raw)<=MAX_DOCUMENT)
    return raw


def document(text):
    require(type(text) is str and 0<len(text.encode('utf-8'))<=MAX_DOCUMENT)
    raw=text.encode('utf-8');value=decode(raw);require(encoded(value)==raw)
    return value


class ContinuationJournal:
    """Own fresh database only; schema/identity checks precede every transaction."""
    def __init__(self,directory):
        self.private=PrivateDirectory(directory);self.db=None;self.lock_fd=None
        self.pid=os.getpid();self.mutex=threading.Lock()
        try:
            self.lock_fd=os.open('continuation.lock',os.O_RDWR|os.O_NOFOLLOW,dir_fd=self.private.fd)
            self.private.validate_file(self.lock_fd);self.lock_identity=identity(os.fstat(self.lock_fd))
            fd=os.open('continuation.sqlite3',os.O_RDWR|os.O_NOFOLLOW,dir_fd=self.private.fd)
            try:self.private.validate_file(fd);self.database_identity=identity(os.fstat(fd))
            finally:os.close(fd)
            self._files()
            self.db=sqlite3.connect(str(self.private.path/'continuation.sqlite3'),isolation_level=None,timeout=0)
            self.db.row_factory=sqlite3.Row
            if hasattr(self.db,'enable_load_extension'):self.db.enable_load_extension(False)
            self.db.execute('PRAGMA trusted_schema=OFF')
            self.db.execute('PRAGMA recursive_triggers=ON')
            self._schema()
            row=self.db.execute('SELECT configuration,configuration_sha256 FROM task WHERE id=1').fetchone()
            require(row is not None,'continuation_journal_corrupt')
            self.configuration=document(row['configuration'])
            require(digest(encoded(self.configuration))==row['configuration_sha256'],'continuation_journal_corrupt')
            from .continuation import validate_configuration
            validate_configuration(self.configuration)
            self.configuration_sha256=row['configuration_sha256']
            self.snapshot()
        except BaseException:
            self.close();raise

    @classmethod
    def create(cls,directory,configuration,initial_state):
        from .continuation import validate_configuration,validate_state
        validate_configuration(configuration);validate_state(initial_state,configuration)
        path=Path(directory).absolute()
        try:os.mkdir(path,0o700)
        except OSError:raise ContinuationError('continuation_fresh_directory_required') from None
        with PrivateDirectory(path) as private:
            private.create('continuation.lock',b'')
            private.create('continuation.sqlite3',b'')
        db=None
        try:
            db=sqlite3.connect(str(path/'continuation.sqlite3'),isolation_level=None,timeout=0)
            db.execute('PRAGMA journal_mode=DELETE');db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            for sql in SQL.values():db.execute(sql)
            db.execute(f'PRAGMA application_id={APPLICATION_ID}');db.execute('PRAGMA user_version=1')
            raw=encoded(configuration);hashed=digest(raw)
            db.execute('INSERT INTO task VALUES(1,?,?)',(raw.decode(),hashed))
            state=deepcopy(initial_state);require(state['revision']==0)
            event={'revision':0,'request_id':'create','request_sha256':hashed,'previous_sha256':'0'*64,'state':state}
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',(0,'create',hashed,'0'*64,encoded(state).decode(),digest(encoded(event))))
            db.execute('COMMIT')
        except (OSError,sqlite3.Error):
            if db is not None and db.in_transaction:db.execute('ROLLBACK')
            raise ContinuationError('continuation_journal_unavailable') from None
        finally:
            if db is not None:db.close()
        with PrivateDirectory(path) as private:os.fsync(private.fd)
        return cls(path)

    @classmethod
    def open(cls,directory):return cls(directory)

    def close(self):
        if self.db is not None:self.db.close();self.db=None
        if self.lock_fd is not None:os.close(self.lock_fd);self.lock_fd=None
        self.private.close()

    def __enter__(self):return self
    def __exit__(self,*args):self.close()

    def _files(self):
        require(os.getpid()==self.pid,'continuation_process_changed')
        self.private.verify()
        require(set(os.listdir(self.private.fd)) <= {'continuation.lock','continuation.sqlite3','continuation.sqlite3-journal'},
            'continuation_journal_changed')
        require(identity(os.fstat(self.lock_fd))==self.lock_identity
            and identity(os.stat('continuation.lock',dir_fd=self.private.fd,follow_symlinks=False))==self.lock_identity,
            'continuation_journal_changed')
        require(identity(os.stat('continuation.sqlite3',dir_fd=self.private.fd,follow_symlinks=False))==self.database_identity,
            'continuation_journal_changed')
        for name in ('continuation.sqlite3','continuation.sqlite3-journal'):
            try:fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=self.private.fd)
            except FileNotFoundError:
                require(name.endswith('-journal'));continue
            try:self.private.validate_file(fd)
            finally:os.close(fd)

    def _schema(self):
        self._files()
        require(self.db.execute('PRAGMA application_id').fetchone()[0]==APPLICATION_ID
            and self.db.execute('PRAGMA user_version').fetchone()[0]==1
            and self.db.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
            and self.db.execute('PRAGMA synchronous').fetchone()[0]==2
            and self.db.execute('PRAGMA recursive_triggers').fetchone()[0]==1,'continuation_journal_schema')
        rows=self.db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        require({row['name']:row['sql'] for row in rows}==SQL,'continuation_journal_schema')
        require(self.db.execute('SELECT COUNT(*) FROM task').fetchone()[0]==1,'continuation_journal_schema')

    def _row(self,row):
        require(row is not None,'continuation_journal_corrupt')
        state=document(row['state_json'])
        from .continuation import validate_state
        validate_state(state,self.configuration)
        require(state['revision']==row['revision'],'continuation_journal_corrupt')
        event={key:row[key] for key in ('revision','request_id','request_sha256','previous_sha256')}
        event['state']=state
        require(digest(encoded(event))==row['event_sha256'],'continuation_journal_corrupt')
        if row['revision']:
            previous=self.db.execute('SELECT event_sha256 FROM events WHERE revision=?',(row['revision']-1,)).fetchone()
            require(previous is not None and previous['event_sha256']==row['previous_sha256'],'continuation_journal_corrupt')
        else:require(row['request_id']=='create' and row['previous_sha256']=='0'*64)
        return {'state':state,'state_sha256':digest(encoded(state)),'event_sha256':row['event_sha256']}

    def snapshot(self):
        self._schema()
        value=self._row(self.db.execute('SELECT * FROM events ORDER BY revision DESC LIMIT 1').fetchone())
        self._files();return deepcopy(value)

    def events(self,*,after_revision=-1,limit=50):
        require(type(after_revision) is int and after_revision>=-1 and type(limit) is int and 1<=limit<=100)
        self._schema()
        rows=self.db.execute('SELECT * FROM events WHERE revision>? ORDER BY revision LIMIT ?',(after_revision,limit)).fetchall()
        return [self._row(row) for row in rows]

    @contextmanager
    def _transaction(self):
        require(self.mutex.acquire(blocking=False),'continuation_busy')
        held=False
        try:
            self._schema()
            try:fcntl.flock(self.lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB);held=True
            except OSError:raise ContinuationError('continuation_busy') from None
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self._files();self.db.execute('COMMIT');os.fsync(self.private.fd);self._files()
            except BaseException:
                if self.db.in_transaction:self.db.execute('ROLLBACK')
                raise
        except sqlite3.Error:raise ContinuationError('continuation_journal_unavailable') from None
        finally:
            if held:fcntl.flock(self.lock_fd,fcntl.LOCK_UN)
            self.mutex.release()

    def _append(self,*,expected_revision,request_id,payload,transition):
        require(type(expected_revision) is int and expected_revision>=0);identifier(request_id)
        require(request_id!='create')
        request_sha=digest(encoded(payload))
        with self._transaction():
            head=self._row(self.db.execute('SELECT * FROM events ORDER BY revision DESC LIMIT 1').fetchone())
            old=self.db.execute('SELECT * FROM events WHERE request_id=?',(request_id,)).fetchone()
            if old is not None:
                require(old['request_sha256']==request_sha,'continuation_request_conflict')
                historical=self._row(old)
                return {'snapshot':head,'receipt':{'request_id':request_id,'revision':old['revision'],
                    'event_sha256':historical['event_sha256'],'state_sha256':historical['state_sha256']},
                    'historical':True,'launchable':False}
            require(head['state']['revision']==expected_revision,'continuation_revision_conflict')
            state=transition(deepcopy(head));state['revision']=expected_revision+1
            from .continuation import validate_state
            validate_state(state,self.configuration)
            event={'revision':state['revision'],'request_id':request_id,'request_sha256':request_sha,
                'previous_sha256':head['event_sha256'],'state':state}
            event_sha=digest(encoded(event))
            self.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',(state['revision'],request_id,request_sha,
                head['event_sha256'],encoded(state).decode(),event_sha))
            current={'state':state,'state_sha256':digest(encoded(state)),'event_sha256':event_sha}
            return {'snapshot':deepcopy(current),'receipt':{'request_id':request_id,'revision':state['revision'],
                'event_sha256':event_sha,'state_sha256':current['state_sha256']},'historical':False,'launchable':False}
