"""Concrete digest/pin consumers; no native qualification, model or server.

Private fallback uses real temporary files and full reads. Eligible-root tests
reuse the reader's synthetic Linux/root observations with real FDs and bytes.
"""
import hashlib
import os
from types import SimpleNamespace

import pytest

from blackoutburst_agents import dev_test_backend as binaries
from blackoutburst_agents import linux_test_namespace as namespace
from blackoutburst_agents import linux_test_backend as backend
from blackoutburst_agents import linux_test_supervisor as supervisor
from blackoutburst_agents import protected_public_pins as protected
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.dev_manifest import canonical
from blackoutburst_agents.dev_journal import PrivateDirectory
from blackoutburst_agents.dev_test_supervisor import _fresh_control_window
from blackoutburst_agents.linux_test_backend import _QualificationContext
from blackoutburst_agents import linux_pool_preparation as preparation
from test_linux_public_runtime import empty_public
from test_protected_public_pins import public_files
from test_development_ticket_frame_client import framed


@pytest.fixture
def public_consumers(public_files,monkeypatch):
    """Match the reader's synthetic public kernel view at external consumers.

    Only these two modules receive the proxy; Board/Git/PrivateDirectory OS
    observations stay real. No production reader callback returns success.
    """
    s=public_files
    class ObservedOS:
        def __getattr__(self,name):return getattr(s.actual_os,name)
        def observed(self,info):
            if (info.st_dev,info.st_ino) not in s.observed:return info
            value=s.module._metadata(info)
            fields={name:getattr(info,name) for name in dir(info) if name.startswith('st_')}
            for name,item in zip(('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink',
                                 'st_size','st_mtime_ns','st_ctime_ns'),value):fields[name]=item
            return SimpleNamespace(**fields)
        def fstat(self,fd):return self.observed(s.actual_os.fstat(fd))
        def stat(self,*args,**kwargs):return self.observed(s.actual_os.stat(*args,**kwargs))
    monkeypatch.setattr(binaries,'os',ObservedOS())
    monkeypatch.setattr(namespace,'os',ObservedOS())
    monkeypatch.setattr(binaries.platform,'system',lambda:'Linux')
    return s


def count_reads(monkeypatch,path):
    identity=os.stat(path);original=os.read;count={'bytes':0,'calls':0}
    def read(descriptor,maximum):
        data=original(descriptor,maximum)
        info=os.fstat(descriptor)
        if (info.st_dev,info.st_ino)==(identity.st_dev,identity.st_ino):
            count['bytes']+=len(data);count['calls']+=1
        return data
    monkeypatch.setattr(os,'read',read)
    return count


def test_private_binary_restored_mtime_same_size_is_fully_rehashed(public_consumers,tmp_path,monkeypatch):
    s=public_consumers;public=s.create('public-executable',b'fixed public',executable=True)
    binaries._binary(public)  # The private fallback is tested inside an active public epoch.
    path=tmp_path/'mutable-executable';path.write_bytes(b'A'*65536);path.chmod(0o700)
    original=os.stat(path);observed=count_reads(monkeypatch,path)
    first=binaries._binary(path)
    assert first==hashlib.sha256(b'A'*65536).hexdigest()
    first_bytes=observed['bytes'];assert first_bytes==65536
    path.write_bytes(b'B'*65536);os.utime(path,ns=(original.st_atime_ns,original.st_mtime_ns))
    assert os.stat(path).st_size==original.st_size and os.stat(path).st_mtime_ns==original.st_mtime_ns
    assert binaries._binary(path)==hashlib.sha256(b'B'*65536).hexdigest()!=first
    assert observed['bytes']==2*first_bytes
    assert str(path) not in s.cache.files


def test_private_pin_only_validator_keeps_full_content_check(empty_public,monkeypatch):
    path=empty_public;path.write_bytes(b'AAAA')
    observed=count_reads(monkeypatch,path)
    expected=namespace.check_public_pin(path)
    assert observed['bytes']==4
    namespace.check_public_pin(path,expected=expected)
    assert observed['bytes']==8
    original=os.stat(path);path.write_bytes(b'BBBB')
    os.utime(path,ns=(original.st_atime_ns,original.st_mtime_ns))
    with pytest.raises(namespace.ProbeError,match='linux_runtime_changed'):
        namespace.check_public_pin(path,expected=expected)
    assert observed['bytes']==12


@pytest.mark.parametrize('consumer',['binary','pin'])
def test_active_reader_failure_never_falls_back_to_full_hash(tmp_path,monkeypatch,consumer):
    path=tmp_path/'unrelated';path.write_bytes(b'A');path.chmod(0o700)
    def changed(*args,**kwargs):raise protected.ProtectedPublicPinsError('protected_public_pin_changed')
    monkeypatch.setattr(protected,'protected_public_pin',changed)
    monkeypatch.setattr(binaries.platform,'system',lambda:'Linux')
    observed=count_reads(monkeypatch,path)
    with pytest.raises(DevelopmentError):
        (binaries._binary(path) if consumer=='binary' else namespace.check_public_pin(path))
    assert observed['bytes']==0


def test_non_linux_binary_never_imports_new_proof_module(tmp_path,monkeypatch):
    path=tmp_path/'old-native-executable';path.write_bytes(b'old full digest');path.chmod(0o700)
    monkeypatch.setattr(binaries.platform,'system',lambda:'Darwin')
    import builtins
    original=builtins.__import__
    def imports(name,*args,**kwargs):
        assert name!='protected_public_pins','old closed Darwin bootstrap cannot load this module'
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',imports)
    assert binaries._binary(path)==hashlib.sha256(b'old full digest').hexdigest()


def test_supervisor_disables_before_loading_and_keeps_plain_full_reads(empty_public,monkeypatch):
    path=empty_public;path.write_bytes(b'plain bytes');path.chmod(0o700)
    observed=count_reads(monkeypatch,path)
    cache=protected._PublicPins();monkeypatch.setattr(protected,'_CACHE',cache)
    monkeypatch.setattr(binaries.platform,'system',lambda:'Linux')
    monkeypatch.setattr(protected,'_controller_facts',lambda:pytest.fail('disabled child attempted public cache observation'))
    class Loaded(Exception):pass
    def load(directory):
        assert directory=='NO_PRIVATE_DIRECTORY_OPENED' and cache.disabled is True
        assert binaries._binary(path)==hashlib.sha256(b'plain bytes').hexdigest()
        assert binaries._binary(path)==hashlib.sha256(b'plain bytes').hexdigest()
        assert observed['bytes']==2*len(b'plain bytes')
        pin=namespace.check_public_pin(path)
        namespace.check_public_pin(path,expected=pin)
        assert observed['bytes']==4*len(b'plain bytes')
        assert not cache.files and not cache.directories
        raise Loaded()
    monkeypatch.setattr(backend,'load_supervisor_job',load)
    try:
        with pytest.raises(Loaded):supervisor.supervise('NO_PRIVATE_DIRECTORY_OPENED')
    finally:cache.close()


def test_one_full_large_public_digest_then_fast_pin_only_checks(public_consumers):
    s=public_consumers;raw=b'x'*(4*1024*1024+17);path=s.create('codex',raw,executable=True)
    assert binaries._binary(path)==hashlib.sha256(raw).hexdigest()
    assert sum(size for _,size in s.reads)==len(raw)
    first=namespace.check_public_pin(path,root_owned=True)
    count=len(s.reads)
    for _ in range(8):
        assert binaries._binary(path)==first['sha256']
        assert namespace.check_public_pin(path,root_owned=True,expected=first)==first
    assert len(s.reads)==count
    # A byte consumer still opens/reads all exact bytes through the old API.
    assert namespace.read_public(path,root_owned=True,expected=first)==(raw,first)


def test_real_preclaim_qualification_reuses_primed_large_public_digest(public_consumers,framed,monkeypatch):
    s=public_consumers;f,t=framed
    raw=b'codex-public'*400000;path=s.create('codex',raw,executable=True)
    f.tools['codex']=str(path);job,ticket,args=f.selected();windows=[];t.cost=.401
    read=s.module.os.read
    def expensive_cold(fd,count):
        data=read(fd,count)
        if s.actual_os.fstat(fd).st_ino==path.stat().st_ino:
            t.clock+=len(data)/(5*1024*1024)  # Deterministic cost, not a benchmark.
        return data
    monkeypatch.setattr(s.module.os,'read',expensive_cold)
    def qualify(actual):
        assert sum(size for _,size in s.reads)==len(raw)  # Request pinning, outside the window.
        before=len(s.reads);t.clock=100.;t.frame_calls.clear()
        producer=SimpleNamespace(manifest=actual.manifest,authorize_scope=actual.authorize)
        producer._authorize=lambda:backend.SupervisedLinuxNamedPythonBackend._authorize(producer)
        context=_QualificationContext(producer,'normal')
        first=_fresh_control_window(context.authorize,nonce='a'*32,sequence=1,clock=lambda:t.clock)
        t.clock+=.4
        source=_fresh_control_window(context.before_source,nonce='a'*32,sequence=2,previous=first,clock=lambda:t.clock)
        assert len(s.reads)==before and len(t.frame_calls)==2
        windows.extend((first,source))
        with PrivateDirectory(actual.directory) as private:private.replace('qualification.json',canonical({'synthetic':True}))
    monkeypatch.setattr(preparation.SupervisedLinuxNamedPythonBackend,'qualify',qualify)
    assert f.prepare(job,args)['status']=='bound'
    assert windows[0]['deadline']==101.5 and windows[1]['validated_at']==pytest.approx(101.202)
    assert sum(size for _,size in s.reads)==len(raw)
    assert f.board.get_ticket(f.human,ticket['id'])['ticket']['claimant'] is None


@pytest.mark.parametrize('change',['grants','source','controller'])
def test_fast_local_digest_never_caches_authority_or_source(public_consumers,framed,monkeypatch,change):
    s=public_consumers;f,t=framed
    path=s.create('codex',b'fixed public executable',executable=True);f.tools['codex']=str(path)
    job,ticket,args=f.selected()
    def qualify(actual):
        producer=SimpleNamespace(manifest=actual.manifest,authorize_scope=actual.authorize)
        producer._authorize=lambda:backend.SupervisedLinuxNamedPythonBackend._authorize(producer)
        context=_QualificationContext(producer,'normal');t.clock=100.
        first=_fresh_control_window(context.authorize,nonce='a'*32,sequence=1,clock=lambda:t.clock)
        if change=='grants':f.board.set_policy(f.human,True,True,True,False,[],[])
        elif change=='source':f.update(ticket,description='Changed protected context')
        else:s.epoch[0]=('different controller epoch',)
        with pytest.raises(DevelopmentError):
            _fresh_control_window(context.before_source,nonce='a'*32,sequence=2,previous=first,clock=lambda:t.clock)
        raise DevelopmentError('synthetic_authorization_refused')
    monkeypatch.setattr(preparation.SupervisedLinuxNamedPythonBackend,'qualify',qualify)
    with pytest.raises(DevelopmentError,match='synthetic_authorization_refused'):f.prepare(job,args)
    assert f.board.get_ticket(f.human,ticket['id'])['ticket']['claimant'] is None


def test_plan_rechecks_public_pins_but_rehashes_private_scripts(public_consumers,tmp_path,monkeypatch):
    s=public_consumers
    interpreter=s.create('python',b'public python',executable=True)
    unshare=s.create('unshare',b'public unshare',executable=True)
    library=s.create('stdlib.py',b'public runtime bytes')
    root=tmp_path/'checkout';root.mkdir(mode=0o700)
    scripts=tmp_path/'scripts';scripts.mkdir(mode=0o700)
    script=scripts/'unit.py';script.write_bytes(b'assert 1\n');script.chmod(0o600)
    monkeypatch.setattr(backend,'_environment',lambda:{'host':{'system':'Linux','machine':'x86_64'},'synthetic':True})
    monkeypatch.setattr(backend,'sys',SimpleNamespace(executable=str(interpreter)))
    # Runtime inventory selection is synthetic; each returned pin and read is real.
    monkeypatch.setattr(backend,'public_runtime',lambda:{str(library):namespace.read_public(library,root_owned=True)[1]})
    plan=backend.pin_linux_execution_plan(root=str(root),scripts_root=str(scripts),
        scripts={'unit':{'path':'unit.py','timeout_seconds':1,'max_output_bytes':64}},
        interpreter=str(interpreter),unshare=str(unshare))
    before=len(s.reads)
    for _ in range(3):assert backend.validate_current_plan(plan)==plan
    assert len(s.reads)==before
    initial=script.stat();script.write_bytes(b'assert 0\n')
    os.utime(script,ns=(initial.st_atime_ns,initial.st_mtime_ns))
    assert script.stat().st_size==initial.st_size and script.stat().st_mtime_ns==initial.st_mtime_ns
    with pytest.raises(DevelopmentError,match='linux_execution_script_changed'):backend.validate_current_plan(plan)
