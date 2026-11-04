"""One fresh locked RunStore turn, failed before its transport was invoked."""
from contextvars import ContextVar
from copy import deepcopy
import os
import threading
from .runtime_guard import RuntimeGuard,RuntimeControlError,RuntimeAdmissionRefused

_CURRENT=ContextVar('runtime_before_transport',default=None)
_KEYS=('id','work_id','request_id','prompt_sha256','created_at')
_WORK=('id','owner_id','agent_id','project_id','ticket_id','workspace','executable','model')


def _fail():raise RuntimeControlError('runtime_uninvoked_proof_invalid')


class _Entry:
    def __init__(self,store,guard,turn,issuer):
        from .run_store import RunStore,_RUNTIME_ENTRY_ISSUER
        if issuer is not _RUNTIME_ENTRY_ISSUER or type(store) is not RunStore:_fail()
        row=store._turn(turn);store._require_lock(row['work_id'])
        self.store,self.lock=store,store._locks[row['work_id']]
        self.guard,self.work,self.turn=guard,None,turn
        self.row=tuple(row[k] for k in _KEYS)
        work=store.get_work(row['work_id']);self.selected=tuple(work[k] for k in _WORK)
        self.client,self.journal=guard.client,guard.journal
        self.owner=(os.getpid(),threading.get_ident())
        self.operation=None;self.failed=self.used=self.entered=False
    def _no_copy(self,*_):raise TypeError('Ephemeral runtime admission')
    __reduce_ex__=__copy__=__deepcopy__=_no_copy


def _lock(p):
    if (p.owner!=(os.getpid(),threading.get_ident())
            or p.store._locks.get(p.row[1]) is not p.lock):_fail()
    row=p.store._turn(p.turn)
    if (tuple(row[k] for k in _KEYS)!=p.row or row['status']!='running'
            or row['pid'] is not None or row['result_json'] is not None):_fail()


def bind_new(g,op):
    p=_CURRENT.get()
    if p is None or p.guard is not g:return
    if (p.operation is not None or op['phase']!='reserving' or op['revision']!=1
            or op['receipt'] is not None or op['start_confirmed']
            or op['configuration']!=g.configuration(p.work,p.turn)):_fail()
    p.operation=deepcopy(op)


def before(g,work,turn,p=None):
    if not isinstance(g,RuntimeGuard):return g.before(work,turn)
    if (type(p) is not _Entry or p.guard is not g or p.entered or p.turn!=turn
            or tuple(work[k] for k in _WORK)!=p.selected):_fail()
    _lock(p);p.entered=True;p.work=deepcopy(work);token=_CURRENT.set(p)
    try:return g.before(work,turn)
    except RuntimeAdmissionRefused:raise
    except Exception:
        p.failed=True
        if p.operation is not None:
            try:g.finish_uninvoked(work,turn,p)
            except Exception:pass  # Preserve the original admission error.
        raise
    finally:_CURRENT.reset(token)


def finish(g,work,turn,p):
    if (type(p) is not _Entry or _CURRENT.get() is not p or p.guard is not g
            or p.used or not p.failed or p.work!=work or p.turn!=turn
            or p.client is not g.client or p.journal is not g.journal or p.operation is None):_fail()
    _lock(p);p.used=True
    born=p.operation;row=g.journal.get(born['id'])
    if (g.operation is None or g.operation['id']!=born['id']
            or any(row[k]!=born[k] for k in ('id','key','configuration','created_at'))
            or row['configuration']!=g.configuration(work,turn)
            or row['phase'] not in ('reserving','reserved','starting','active','needs_review','finished')):_fail()
    g.operation=row
    try:
        found=g.client.call('lookup_execution',{'idempotency_key':row['key']})
        if type(found) is not dict or found.get('found') is not True:raise ValueError
        receipt=g.metadata(found.get('execution'))
        if row['phase']=='finished' and receipt!=row['receipt']:raise ValueError
        if receipt['state']!='finished':
            g.save('needs_review',receipt)
            receipt=g.metadata(g.client.call('finish_execution',{'execution_id':receipt['id'],
                'expected_revision':receipt['revision'],'status':'blocked','cleanup_confirmed':True,'reason':'execution_failed'}))
        if (receipt['state']!='finished' or receipt['cleanup_confirmed'] is not True
                or receipt['outcome']!='blocked' or receipt['reason']!='execution_failed'):raise ValueError
        if g.operation['phase']!='finished':g.save('finished',receipt)
    except Exception:
        g.review()
        raise RuntimeControlError('runtime_uninvoked_cleanup_unconfirmed') from None
    return True
