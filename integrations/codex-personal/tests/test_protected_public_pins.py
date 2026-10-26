"""Real local files/descriptors with explicit synthetic root kernel observations.

No production owner, credentials, server, service, native sandbox or model.
"""
from copy import copy, deepcopy
import hashlib
import os
from pathlib import Path
import pickle
import stat
from types import SimpleNamespace
import threading

import pytest

from blackoutburst_agents import protected_public_pins as public


@pytest.fixture
def public_files(tmp_path, monkeypatch):
    """Use for integration: only registered public inodes gain root observations.

    Private files and other modules' os/stat functions remain entirely real.
    The initial /tmp ancestors represent a separate root-protected mount in
    this synthetic kernel view, never a production exception for writable /tmp.
    """
    root=tmp_path.resolve()/'public';root.mkdir(mode=0o755)
    cache=public._PublicPins();monkeypatch.setattr(public,'_CACHE',cache)
    epoch=[('controller',os.getpid(),994,994,(1,2),'nnp1-capsets0-initial-userns')]
    monkeypatch.setattr(public,'_controller_facts',lambda:epoch[0])
    monkeypatch.setattr(public,'_fs_magic',lambda _:0xef53)
    observed={};registered=set();reads=[]
    for path in (root,*root.parents):
        info=path.stat();observed[(info.st_dev,info.st_ino)]=('outside' if path!=root else 'inside')
    original_metadata=public._metadata
    def metadata(info):
        value=list(original_metadata(info));kind=observed.get((info.st_dev,info.st_ino))
        if kind is not None:
            value[3]=0
            if kind=='outside':value[2]&=~0o022
        return tuple(value)
    monkeypatch.setattr(public,'_metadata',metadata)
    actual_os=public.os
    class ObservedOS:
        def __getattr__(self,name):return getattr(actual_os,name)
        def read(self,fd,count):
            data=actual_os.read(fd,count);info=actual_os.fstat(fd)
            if (info.st_dev,info.st_ino) in registered:reads.append((info.st_ino,len(data)))
            return data
    monkeypatch.setattr(public,'os',ObservedOS())
    def create(name,data=b'public data',*,executable=False):
        path=root/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data);path.chmod(0o755 if executable else 0o444)
        for node in (path,*[p for p in path.parents if p.is_relative_to(root)]):
            info=node.stat();key=(info.st_dev,info.st_ino);observed[key]='inside'
            if node==path:registered.add(key)
        return path
    result=SimpleNamespace(module=public,cache=cache,root=root,create=create,reads=reads,
        epoch=epoch,observed=observed,registered=registered,actual_os=actual_os)
    try:yield result
    finally:cache.close()


def observe(s,path,**kwargs):
    return s.module.protected_public_pin(path,**kwargs)


def test_real_cold_hash_then_zero_content_reads_and_copied_facts(public_files):
    s=public_files;path=s.create('codex',b'x'*(3*1024*1024+19),executable=True)
    first=observe(s,path,executable=True);count=len(s.reads)
    assert first['sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    assert sum(size for _,size in s.reads)==path.stat().st_size
    assert first['uid']==0 and first['nlink']==1
    first['identity'][0]=-1;first['signature'][0]=-1
    again=observe(s,path,executable=True)
    assert again['identity'][0]>0 and again['signature'][0]>0 and len(s.reads)==count
    assert all(not os.get_inheritable(fd) for fd in s.cache.owned)


@pytest.mark.parametrize('mutation',['content','same_size_restored_mtime','mode','replacement','symlink','ancestor','hardlink'])
def test_observed_mutation_poison_is_permanent(public_files,mutation):
    s=public_files;path=s.create('data',b'original');observe(s,path)
    old=path.stat()
    if mutation=='content':path.chmod(0o644);path.write_bytes(b'new bytes')
    elif mutation=='same_size_restored_mtime':
        path.chmod(0o644);path.write_bytes(b'changed!');path.chmod(0o444)
        os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns))
    elif mutation=='mode':path.chmod(0o644)
    elif mutation=='replacement':
        path.rename(path.with_name('old'));s.create('data',b'original')
    elif mutation=='symlink':
        path.rename(path.with_name('old'));path.symlink_to(path.with_name('old'))
    elif mutation=='ancestor':s.root.chmod(0o777)
    else:os.link(path,path.with_name('alias'))
    with pytest.raises(public.ProtectedPublicPinsError):observe(s,path)
    with pytest.raises(public.ProtectedPublicPinsError):observe(s,s.create('new'))
    assert not s.cache.owned


def test_private_leaf_and_writable_ancestry_never_admitted(public_files):
    s=public_files
    private=s.root/'private';private.write_bytes(b'private')
    assert observe(s,private) is None
    writable=s.root/'writable';writable.mkdir(mode=0o777)
    path=s.create('writable/child');writable.chmod(0o777)
    assert observe(s,path) is None
    assert not s.cache.files and not s.reads


@pytest.mark.parametrize('initial',[None,('other controller',)])
def test_initial_ineligible_and_active_epoch_drift(public_files,initial):
    s=public_files;path=s.create('data')
    if initial is None:
        s.epoch[0]=None;assert observe(s,path) is None;assert not s.cache.owned
    else:
        observe(s,path);s.epoch[0]=initial
        with pytest.raises(public.ProtectedPublicPinsError):observe(s,path)
        assert not s.cache.owned


def test_threads_share_public_facts_but_reobserve_context(public_files):
    s=public_files;path=s.create('data');first=observe(s,path);read_count=len(s.reads)
    results=[]
    thread=threading.Thread(target=lambda:results.append(observe(s,path)));thread.start();thread.join(3)
    assert not thread.is_alive() and results==[first] and len(s.reads)==read_count


def test_cold_hash_does_not_hold_global_lock_against_hot_worker(public_files,monkeypatch):
    s=public_files;hot=s.create('hot');cold=s.create('cold',b'x'*1024*1024)
    observe(s,hot);cold_inode=cold.stat().st_ino
    entered=threading.Event();release=threading.Event();results=[];failures=[]
    original_read=s.module.os.read
    def blocked(fd,count):
        if s.actual_os.fstat(fd).st_ino==cold_inode:
            entered.set();assert release.wait(3)
        return original_read(fd,count)
    monkeypatch.setattr(s.module.os,'read',blocked)
    def cold_worker():
        try:results.append(observe(s,cold))
        except BaseException as error:failures.append(error)
    worker=threading.Thread(target=cold_worker);worker.start();assert entered.wait(3)
    done=threading.Event()
    hot_worker=threading.Thread(target=lambda:(results.append(observe(s,hot)),done.set()))
    hot_worker.start()
    try:assert done.wait(1),'A hot check waited behind an unrelated cold hash'
    finally:release.set();worker.join(3);hot_worker.join(3)
    assert not worker.is_alive() and not hot_worker.is_alive() and not failures and len(results)==2


def test_disable_closes_descriptors_and_never_readmits(public_files):
    s=public_files;path=s.create('data');observe(s,path);fds=set(s.cache.owned)
    public.disable_protected_public_pins_for_process()
    assert not s.cache.owned and s.cache.disabled
    for fd in fds:
        with pytest.raises(OSError):os.fstat(fd)
    assert observe(s,path) is None and not s.cache.owned


def test_close_is_not_disable_or_rebaseline(public_files):
    s=public_files;path=s.create('data');observe(s,path)
    public.close_protected_public_pins()
    with pytest.raises(public.ProtectedPublicPinsError):observe(s,path)
    with pytest.raises(public.ProtectedPublicPinsError):public.disable_protected_public_pins_for_process()


def test_cache_cannot_be_serialized_or_copied(public_files):
    for operation in (pickle.dumps,copy,deepcopy):
        with pytest.raises(TypeError):operation(public_files.cache)


def test_fork_closes_child_epoch_without_harming_parent(public_files):
    if not hasattr(os,'fork'):pytest.skip('fork requires POSIX')
    s=public_files;path=s.create('data');first=observe(s,path);fds=set(s.cache.owned)
    pid=os.fork()
    if pid==0:
        ok=True
        try:
            try:observe(s,path);ok=False
            except public.ProtectedPublicPinsError:pass
            for fd in fds:
                try:os.fstat(fd);ok=False
                except OSError:pass
        finally:os._exit(0 if ok else 1)
    assert os.waitpid(pid,0)[1]==0 and observe(s,path)==first


def controller_rows(**changes):
    values=dict(Pid='123',Tgid='100',Uid='994 994 994 994',Gid='994 994 994 994',Groups='994',
        NoNewPrivs='1',CapInh='0000000000000000',CapPrm='0000000000000000',
        CapEff='0000000000000000',CapBnd='000001ffffffffff',CapAmb='0000000000000000')
    values.update(changes)
    return ''.join(k+':\t'+v+'\n' for k,v in values.items()).encode()


def parse_controller(status=None,uid_map=b'0 0 4294967295\n',gid_map=b'0 0 4294967295\n'):
    return public._parse_controller(controller_rows() if status is None else status,uid_map,gid_map,(1,2),
        pid=100,tid=123,ids=(994,994,994,994),groups=[994])


def test_controller_accepts_safe_nonzero_bounding_set_only():
    assert parse_controller() is not None


@pytest.mark.parametrize('change',[{'NoNewPrivs':'0'},{'CapInh':'0000000000000001'},
    {'CapPrm':'0000000000000001'},{'CapEff':'0000000000000001'},{'CapAmb':'0000000000000001'},
    {'Uid':'994 0 994 994'},{'Pid':'124'},{'Tgid':'101'},{'Groups':'994 1000'}])
def test_controller_rejects_caps_ids_or_thread_mismatch(change):
    assert parse_controller(controller_rows(**change)) is None


@pytest.mark.parametrize('mapping',[b'0 1000 1\n',b'0 0 1\n',b'0 0 4294967295\n1 1 1\n',b'bad'])
def test_controller_rejects_rootless_or_partial_identity_namespace(mapping):
    assert parse_controller(uid_map=mapping) is None
    assert parse_controller(gid_map=mapping) is None


def test_held_descriptor_and_content_budgets_are_bounded(public_files,monkeypatch):
    s=public_files;monkeypatch.setattr(public,'MAX_FILES',1)
    first=s.create('one');second=s.create('two')
    assert observe(s,first) is not None and observe(s,second) is None
    assert len(s.cache.files)==1 and len(s.cache.owned)<public.MAX_HELD_FDS
    monkeypatch.setattr(public,'MAX_TOTAL_BYTES',1)
    assert observe(s,s.create('three')) is None


def test_active_controller_drift_never_falls_back(public_files):
    s=public_files;path=s.create('data');observe(s,path)
    s.epoch[0]=None
    with pytest.raises(public.ProtectedPublicPinsError):observe(s,path)


def test_disable_during_cold_hash_aborts_without_closing_an_active_read(public_files,monkeypatch):
    s=public_files;path=s.create('cold',b'x'*1024*1024);inode=path.stat().st_ino
    entered=threading.Event();release=threading.Event();errors=[]
    original=s.module.os.read
    def blocked(fd,count):
        if s.actual_os.fstat(fd).st_ino==inode:
            entered.set();assert release.wait(3)
        return original(fd,count)
    monkeypatch.setattr(s.module.os,'read',blocked)
    def worker():
        try:observe(s,path)
        except BaseException as error:errors.append(error)
    thread=threading.Thread(target=worker);thread.start();assert entered.wait(3)
    public.disable_protected_public_pins_for_process()
    assert observe(s,path) is None
    release.set();thread.join(3)
    assert not thread.is_alive() and len(errors)==1 and isinstance(errors[0],public.ProtectedPublicPinsError)
    assert not s.cache.owned and observe(s,path) is None


def test_lost_owned_fd_never_closes_a_recycled_foreign_descriptor(public_files):
    s=public_files;path=s.create('one');other=s.create('other')
    observe(s,path);fd=s.cache.files[str(path)]['fd']
    os.close(fd)
    foreign=os.open(other,os.O_RDONLY)
    assert foreign==fd
    try:
        with pytest.raises(public.ProtectedPublicPinsError):observe(s,path)
        assert os.read(foreign,64)==other.read_bytes()
    finally:os.close(foreign)


@pytest.mark.parametrize('magic',[0x65735546,0x6969,0x517b,0])
def test_userspace_network_or_unknown_filesystems_keep_full_reader(public_files,monkeypatch,magic):
    s=public_files;path=s.create('data');monkeypatch.setattr(public,'_fs_magic',lambda _:magic)
    assert observe(s,path) is None and not s.cache.files and not s.reads


def test_active_filesystem_change_is_not_a_fallback(public_files,monkeypatch):
    s=public_files;path=s.create('data');observe(s,path)
    monkeypatch.setattr(public,'_fs_magic',lambda _:0x65735546)
    with pytest.raises(public.ProtectedPublicPinsError):observe(s,path)
