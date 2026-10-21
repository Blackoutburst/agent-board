"""The portable fdinfo model must not erase a descriptor reused by a thread."""
import fcntl
import os
import threading

from linux_lock_fixture import LockObservationFixture


def test_close_and_reused_fd_have_atomic_bookkeeping(tmp_path):
    # Standalone bookkeeping instance: no monkeypatch of the kernel or a worker.
    model=object.__new__(LockObservationFixture)
    model.handles,model.locks={},{}
    model.mutex=threading.RLock()
    model.open,model.close,model.dup,model.flock=os.open,os.close,os.dup,fcntl.flock
    first=model._open(tmp_path/'first',os.O_RDWR|os.O_CREAT,0o600)
    original_token=model.handles[first]
    model._flock(first,fcntl.LOCK_EX|fcntl.LOCK_NB)
    closed,release,opened=threading.Event(),threading.Event(),threading.Event()
    errors=[];new={};real_close=os.close
    def paused_close(fd):
        real_close(fd);closed.set()
        if not release.wait(2):raise AssertionError('close bookkeeping release timed out')
    model.close=paused_close
    def close_first():
        try:model._close(first)
        except BaseException as error:errors.append(error)
    def open_second():
        try:
            new['fd']=model._open(tmp_path/'second',os.O_RDWR|os.O_CREAT,0o600)
            new['token']=model.handles[new['fd']];opened.set()
        except BaseException as error:errors.append(error)
    closer=threading.Thread(target=close_first);opener=threading.Thread(target=open_second)
    try:
        closer.start();assert closed.wait(2)
        opener.start()
        # Old accounting lets this open reuse first before close pops its token.
        # Fixed accounting delays only this local descriptor operation.
        opened.wait(.2);release.set()
        closer.join(2);opener.join(2)
        assert not closer.is_alive() and not opener.is_alive() and not errors
        assert new['fd']==first, 'fixture did not exercise descriptor reuse'
        assert model.handles.get(new['fd']) is new['token']
        assert original_token not in model.locks
        model._flock(new['fd'],fcntl.LOCK_EX|fcntl.LOCK_NB)
        info=os.fstat(new['fd'])
        assert model.observe(new['fd'],pid=os.getpid(),expected=(info.st_dev,info.st_ino))
    finally:
        release.set()
        if closer.ident:closer.join(2)
        if opener.ident:opener.join(2)
        if 'fd' in new:real_close(new['fd'])


def test_blocking_flock_does_not_hold_accounting_mutex(tmp_path):
    model=object.__new__(LockObservationFixture)
    model.handles,model.locks={},{}
    model.mutex=threading.RLock()
    model.open,model.close,model.dup,model.flock=os.open,os.close,os.dup,fcntl.flock
    first=model._open(tmp_path/'lock',os.O_RDWR|os.O_CREAT,0o600)
    second=model._open(tmp_path/'lock',os.O_RDWR,0o600)
    model._flock(first,fcntl.LOCK_EX|fcntl.LOCK_NB)
    waiting,acquired=threading.Event(),threading.Event();errors=[];real_flock=fcntl.flock
    def tracked(fd,operation):
        if fd==second and operation==fcntl.LOCK_EX:waiting.set()
        return real_flock(fd,operation)
    model.flock=tracked
    def acquire():
        try:model._flock(second,fcntl.LOCK_EX);acquired.set()
        except BaseException as error:errors.append(error)
    def unlock():
        try:model._flock(first,fcntl.LOCK_UN)
        except BaseException as error:errors.append(error)
    waiter=threading.Thread(target=acquire);unlocker=threading.Thread(target=unlock)
    try:
        waiter.start();assert waiting.wait(2)
        unlocker.start()
        assert acquired.wait(2), 'blocking flock prevented the independent unlock'
        waiter.join(2);unlocker.join(2)
        assert not waiter.is_alive() and not unlocker.is_alive() and not errors
    finally:
        real_flock(first,fcntl.LOCK_UN)
        if waiter.ident:waiter.join(2)
        if unlocker.ident:unlocker.join(2)
        os.close(first);os.close(second)
