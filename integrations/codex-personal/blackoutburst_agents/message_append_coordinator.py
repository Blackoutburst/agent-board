"""Finite append reconciliation in the actual owner worker, without inference.

The outbox is inert data saved before the first RPC. It cannot establish an
owner acquisition or reconstruct missing child authorities. Every local effect
still checks the exact journals, private source/slot and original Host CAS.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from .host_config import HostError, private_directory
from .message_append_protocol import PIN_FIELDS, arguments, response, view, request, slot

NAMESPACE = uuid.UUID('b23f1c87-733e-40e3-9eca-e93ce34f22f3')
MAX_OUTBOX = 18*1024*1024


def _fail(reason='conflict'):
    raise HostError('messages_append_coordinator_'+reason)


def _encoded(value):
    try:
        raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
        if len(raw)>MAX_OUTBOX:_fail('bounds')
        return raw
    except (TypeError,ValueError,UnicodeError,RecursionError):
        _fail('invalid')


def _read_exact(path):
    descriptor=None
    try:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        before=os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid()
                or stat.S_IMODE(before.st_mode)!=0o600 or before.st_nlink!=1
                or not 0<before.st_size<=MAX_OUTBOX):_fail('outbox')
        chunks=[];size=0
        while True:
            chunk=os.read(descriptor,min(65536,MAX_OUTBOX+1-size))
            if not chunk:break
            chunks.append(chunk);size+=len(chunk)
            if size>MAX_OUTBOX:_fail('bounds')
        after=os.fstat(descriptor);named=os.stat(path,follow_symlinks=False)
        fields=('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
        if any(getattr(before,k)!=getattr(after,k) or getattr(before,k)!=getattr(named,k) for k in fields):
            _fail('outbox_changed')
        raw=b''.join(chunks)
        def pairs(items):
            result={}
            for key,value in items:
                if key in result:_fail('outbox')
                result[key]=value
            return result
        result=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,
            parse_constant=lambda _:_fail('outbox'))
        if _encoded(result)!=raw:_fail('outbox')
        return result,hashlib.sha256(raw).hexdigest()
    except (OSError,ValueError,UnicodeError,RecursionError):
        _fail('outbox')
    finally:
        if descriptor is not None:os.close(descriptor)


@contextmanager
def existing_append_children(host,operation_id=None):
    """Require existing versioned authorities before their constructors run."""
    from .message_append_children import locked_message_append_children
    from .message_journal import MessageJournal
    from .run_store import RunStore
    from .runtime_journal import RuntimeJournal
    identities=(('messages','messages.sqlite3',0x42424D46,3),
        ('message-runs','runs.sqlite3',0,2),('message-runtime','runtime.sqlite3',0x42425254,4))
    for directory,filename,application,version in identities:
        path=host.directory/directory/filename
        # Do not create directories or silently migrate a missing/old journal.
        for ancestor in (path,*path.parents):
            if ancestor.is_symlink():_fail('children_identity')
        fd=None
        try:
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            info=os.fstat(fd);header=os.read(fd,100)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid()
                    or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1
                    or len(header)!=100 or header[:16]!=b'SQLite format 3\0'
                    or int.from_bytes(header[60:64],'big')!=version
                    or int.from_bytes(header[68:72],'big')!=application):_fail('children_identity')
        except OSError:_fail('children_identity')
        finally:
            if fd is not None:os.close(fd)
    with ExitStack() as resources:
        messages=resources.enter_context(MessageJournal(host.directory/'messages'))
        runs=RunStore(host.directory/'message-runs');resources.callback(runs.close)
        runtime=resources.enter_context(RuntimeJournal(host.directory/'message-runtime'))
        with locked_message_append_children(host,messages,runs,runtime,operation_id=operation_id) as children:
            yield children


class MessagesAppendCoordinator:
    def __init__(self,host,client,*,source_release_sha256,candidate_factory,source_guard,
                 children_factory=existing_append_children):
        if not callable(candidate_factory) or not callable(source_guard):_fail('guard_required')
        self.host,self.client=host,client
        self.release=source_release_sha256
        self.candidate_factory,self.source_guard=candidate_factory,source_guard
        self.children_factory=children_factory

    def _guard(self,record,plan=None):
        self.host._require_lock()
        if self.host._db.in_transaction and plan is None:_fail('transaction')
        if self.source_guard() is not None:_fail('source')
        v,b,s=record['request'],record['body'],record['slot']
        view(v);request(b);slot(s)
        if (v['host_id']!=self.host.managed_settings()['host_id']
                or v['owner_id']!=self.host.control()['configuration']['owner_id']):_fail('identity')
        if plan is not None:
            if plan['source_release_sha256']!=self.release:_fail('source')
            candidate=self.candidate_factory(record,deepcopy(plan['old_configuration']))
            if _encoded(candidate)!=_encoded(plan['new_configuration']):_fail('slot_changed')

    def _outbox(self,record):
        identifier=record['request']['request_id']
        if type(identifier) is not str or len(identifier)!=32 or any(c not in '0123456789abcdef' for c in identifier):
            _fail('identifier')
        directory=self.host.directory/'message-append-outbox'
        # This directory contains inert proposals, never a lock or authority.
        private_directory(directory)
        return directory/(identifier+'.json')

    def _load(self,record):
        path=self._outbox(record)
        try:path.lstat()
        except FileNotFoundError:return None
        data,pin=_read_exact(path)
        if (type(data) is not dict or set(data)!={'schema','request_id','request_sha256','body','slot','plan'}
                or data['schema']!='message-append-inert-outbox-v1'
                or data['request_id']!=record['request']['request_id']
                or data['request_sha256']!=record['request']['request_sha256']
                or _encoded(data['body'])!=_encoded(record['body']) or _encoded(data['slot'])!=_encoded(record['slot'])):
            _fail('outbox_changed')
        from .message_append_journal import _plan
        plan=_plan(data['plan'])
        self._guard(record,plan)
        return plan,pin

    def _save(self,record,plan):
        path=self._outbox(record)
        data=dict(schema='message-append-inert-outbox-v1',request_id=record['request']['request_id'],
            request_sha256=record['request']['request_sha256'],body=record['body'],slot=record['slot'],plan=plan)
        raw=_encoded(data);fd=None
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            offset=0
            while offset<len(raw):offset+=os.write(fd,raw[offset:])
            os.fsync(fd)
        except OSError:_fail('outbox')
        finally:
            if fd is not None:os.close(fd)
        d=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(d)
        finally:os.close(d)
        return self._load(record)

    def _call(self,name,args,record,plan,pin):
        if self.host._db.in_transaction:_fail('transaction')
        self._guard(record,plan)
        loaded=self._load(record)
        if loaded is None or loaded[1]!=pin or _encoded(loaded[0])!=_encoded(plan):_fail('outbox_changed')
        arguments(name,args)
        result=self.client.call(name,args)
        response(name,result,args)
        self._guard(record,plan)
        loaded=self._load(record)
        if loaded is None or loaded[1]!=pin:_fail('outbox_changed')
        return result

    def advance(self,record):
        """One finite phase only, with no global retry, Start, feed or model."""
        self._guard(record)
        loaded=self._load(record)
        v=record['request']
        if loaded is None and v['state']!='requested':_fail('original_proposal_required')
        operation=None if loaded is None else loaded[0]['operation_id']
        with self.children_factory(self.host,operation) as children:
            if loaded is None:
                c=self.host.control()
                seed=c['instance_id']+':'+v['request_sha256']
                operation=str(uuid.uuid5(NAMESPACE,'append:'+seed))
                anchor=None if self.host._db.execute('PRAGMA user_version').fetchone()[0]==5 else str(uuid.uuid5(NAMESPACE,'anchor:'+seed))
                candidate=self.candidate_factory(record,deepcopy(c['configuration']))
                plan=self.host.propose_messages_append(candidate,operation_id=operation,anchor_operation_id=anchor,
                    expected_host_revision=c['revision'],expected_configuration_sha256=c['hash'],
                    expected_catalog_sha256=self.host.managed_settings()['catalog_sha256'],
                    expected_release_sha256=self.release,children=children)
                loaded=self._save(record,plan)
            plan,pin=loaded
            local=self.host.messages_append_record(operation_id=operation)
            recheck=lambda:self._guard(record,plan)
            base=dict(host_id=v['host_id'],request_id=v['request_id'])
            if local is None:
                if v['state'] not in ('requested','prepared'):_fail('local_intent_required')
                args=dict(**base,expected_revision=1,operation_id=operation,
                    idempotency_key='messages-append-prepare-'+plan['plan_sha256'],
                    **{key:plan[key] for key in PIN_FIELDS})
                p=self._call('prepare_server_messages_append',args,record,plan,pin)
                local=self.host.begin_messages_append(plan,p,children=children,recheck=recheck)
            else:
                if _encoded(local['plan'])!=_encoded(plan):_fail('outbox_changed')
                revision=local['revision']
                if revision==4:return {'status':'complete','request_id':v['request_id'],'operation_id':operation,'revision':4}
                methods={1:('precommit_server_messages_append','commit_messages_append'),
                    2:('commit_server_messages_append','bind_messages_append_instance'),
                    3:('complete_server_messages_append','complete_messages_append')}
                remote,method=methods[revision]
                args=dict(**base,expected_revision=revision+1,proof=local['proofs'][-1],
                    idempotency_key='messages-append-'+str(revision+1)+'-'+plan['plan_sha256'])
                if revision==2:
                    target=plan['new_configuration']['targets'][-1]
                    args['entry']=next(e for e in plan['new_catalog'] if e['catalog_key']==target['id'])
                p=self._call(remote,args,record,plan,pin)
                local=getattr(self.host,method)(operation,revision,p,children=children,recheck=recheck)
            return {'status':'complete' if local['revision']==4 else 'pending',
                'request_id':v['request_id'],'operation_id':operation,'revision':local['revision']}
