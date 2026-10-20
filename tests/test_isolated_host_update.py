"""Updater boundaries with synthetic public files and fake systemd/renameat2.

No real account, service, native qualification, provider or model is called.
"""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import shutil
import stat
from types import SimpleNamespace

import pytest

from deploy.isolated_host import manage,package as pkg,update
import test_isolated_host_package as package_fixture

ROOT=package_fixture.ROOT
REAL_RUNTIME=update.Runtime
REAL_RENAME=update._rename
REAL_ACCOUNTS=update._accounts


def fake_rename(source_fd,source,destination_fd,destination,flags):
    # Pure fixture for control flow. It does NOT qualify Linux atomic rename.
    if flags==1:
        try:os.stat(destination,dir_fd=destination_fd,follow_symlinks=False)
        except FileNotFoundError:pass
        else:raise FileExistsError(destination)
        os.rename(source,destination,src_dir_fd=source_fd,dst_dir_fd=destination_fd)
    elif flags==2:
        temporary=source+'.fixture-swap'
        os.rename(destination,temporary,src_dir_fd=destination_fd,dst_dir_fd=source_fd)
        os.rename(source,destination,src_dir_fd=source_fd,dst_dir_fd=destination_fd)
        os.rename(temporary,source,src_dir_fd=source_fd,dst_dir_fd=source_fd)
    else:raise AssertionError('unknown rename flag')


@pytest.fixture
def case(monkeypatch):
    fixture=package_fixture.PackageTests('test_bundle_contains_only_pinned_public_code_and_explicit_tools');fixture.setUp()
    actual_uid=os.geteuid();actual_gid=os.getegid()
    try:
        fixture.build();old=pkg.load_bundle(fixture.output);old_id=pkg.manifest_id(old)
        target=fixture.source/'deploy/isolated_host/managed-unit.service.in'
        target.write_bytes((ROOT/'deploy/isolated_host/managed-unit.service.in').read_bytes())
        (fixture.source/'connect.py').write_text('# changed public connector fixture\n')
        fixture.git('add','.');fixture.git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','public update')
        new_path=fixture.base/'new-bundle'
        manage.prepare(fixture.source,new_path,unit_mode='managed',**fixture.options)
        new=pkg.load_bundle(new_path);new_id=pkg.manifest_id(new)
        base=fixture.base/'state';releases=fixture.base/'releases';units=fixture.base/'units'
        for directory in (base,base/'installations',base/'owners',releases,units):directory.mkdir(mode=0o700)
        (base/'.install.lock').touch(mode=0o600)
        release=releases/old_id;shutil.copytree(fixture.output,release)
        for path in release.rglob('*'):
            if path.is_file():path.chmod(0o755 if 'tools' in path.relative_to(release).parts else 0o444)
        for path in sorted((p for p in release.rglob('*') if p.is_dir()),reverse=True):path.chmod(0o555)
        release.chmod(0o555)
        receipt={'schema_version':1,'bundle_id':old_id,'status':'installed_disabled','created_accounts':[
            {'name':name,'uid':2100+i,'gid':2100+i} for i,name in enumerate(pkg.OWNER_NAMES)],
            'written_units':[pkg.unit_name(n) for n in pkg.OWNER_NAMES],'activated':False}
        receipt_path=base/'installations'/(old_id+'.json');receipt_path.write_bytes(pkg.encoded(receipt));receipt_path.chmod(0o600)
        old_bytes={}
        for name in pkg.OWNER_NAMES:
            unit=pkg.unit_name(name)
            with monkeypatch.context() as context:
                context.setattr(pkg,'BASE',base);context.setattr(pkg,'RELEASES',releases)
                data=pkg.render_unit((release/'unit.service.in').read_text(),old,release,name).encode()
            (units/unit).write_bytes(data);(units/unit).chmod(0o644);old_bytes[unit]=data
            (base/'owners'/name).mkdir(mode=0o700)
        calls=[];state={'active':'inactive','sub':'dead','enabled':'disabled','dropins':'','calls':calls}
        def command(argv):
            calls.append(argv)
            if argv==['/usr/bin/systemctl','daemon-reload']:return b''
            assert argv[:3]==['/usr/bin/systemctl','show','--no-pager']
            name=argv[-1]
            return ('LoadState=loaded\nActiveState='+state['active']+'\nSubState='+state['sub']+'\nUnitFileState='+state['enabled']+
                '\nMainPID=0\nControlPID=0\nDropInPaths='+state['dropins']+'\nFragmentPath='+str(units/name)+'\nNames='+name+'\n').encode()
        original_trusted=update.trusted
        def fixture_trusted(info):
            # Only metadata trust is simulated; fd/path/inode checks are real.
            if info.st_uid not in (0,actual_uid):raise pkg.PackageError('untrusted_installation')
            if info.st_mode&0o022 and not info.st_mode&stat.S_ISVTX:raise pkg.PackageError('untrusted_installation')
        class Runtime:
            def __init__(self,*_):pass
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def verify(self):pass
        monkeypatch.setattr(update,'ROOT_UID',actual_uid);monkeypatch.setattr(update,'trusted',fixture_trusted)
        monkeypatch.setattr(update.os,'geteuid',lambda:0)
        monkeypatch.setattr(update.platform,'system',lambda:'Linux');monkeypatch.setattr(update.platform,'machine',lambda:'x86_64')
        for module in (update,pkg):
            monkeypatch.setattr(module,'BASE',base);monkeypatch.setattr(module,'RELEASES',releases)
        monkeypatch.setattr(update,'UNIT_DIR',units);monkeypatch.setattr(update,'SYSTEMD_ROOTS',(units,))
        monkeypatch.setattr(update,'Runtime',Runtime);monkeypatch.setattr(update,'_accounts',lambda accounts:None)
        monkeypatch.setattr(update,'_rename',fake_rename);monkeypatch.setattr(manage,'command',command)
        yield SimpleNamespace(root=fixture.base,bundle=new_path,base=base,releases=releases,units=units,
            old_id=old_id,new_id=new_id,old=old,new=new,receipt=receipt,receipt_path=receipt_path,
            old_bytes=old_bytes,state=state,actual_uid=actual_uid,actual_gid=actual_gid)
    finally:
        for path in fixture.base.rglob('*'):
            if path.is_dir() and not path.is_symlink():path.chmod(0o700)
        fixture.tearDown()


def apply(case):
    return update.update_existing(case.bundle,expected_old_release=case.old_id,expected_new_release=case.new_id)


def test_update_writes_only_public_release_known_units_and_disabled_receipt(case):
    private=case.base/'owners'/pkg.OWNER_NAMES[0]/'auth-sentinel';private.write_text('PRIVATE OWNER AUTH UNREAD')
    result=apply(case)
    assert result['status']=='updated_disabled' and not result['activated'] and not result['execution_qualified']
    assert private.read_text()=='PRIVATE OWNER AUTH UNREAD'
    assert (case.releases/case.old_id).is_dir() and (case.releases/case.new_id).is_dir()
    for name in pkg.OWNER_NAMES:
        data=(case.units/pkg.unit_name(name)).read_text()
        assert ' managed-run\n' in data and 'Restart=no\n' in data and str(case.releases/case.new_id) in data
    receipt=pkg.decode((case.base/'installations'/(case.new_id+'.json')).read_bytes())
    assert receipt['created_accounts']==case.receipt['created_accounts']
    assert [argv for argv in case.state['calls'] if argv[1]!='show']==[['/usr/bin/systemctl','daemon-reload']]
    assert (case.base/'updates'/result['backup_id']/'result.json').is_file()


@pytest.mark.parametrize('state,value',[('active','active'),('active','failed'),('sub','running'),('enabled','enabled'),('enabled','static'),('dropins','/public/generic-service.d/fixture.conf')])
def test_service_or_dropin_drift_refuses_before_release_or_unit_change(case,state,value):
    case.state[state]=value
    with pytest.raises(pkg.PackageError):apply(case)
    assert not (case.releases/case.new_id).exists() and not (case.base/'updates').exists()
    assert all((case.units/n).read_bytes()==data for n,data in case.old_bytes.items())


def test_existing_target_release_is_not_adopted_or_deleted(case):
    target=case.releases/case.new_id;target.mkdir();(target/'unknown').write_text('keep')
    with pytest.raises(pkg.PackageError,match='already_exists'):apply(case)
    assert (target/'unknown').read_text()=='keep'


@pytest.mark.parametrize('kind',['file','directory','link'])
def test_unknown_public_bundle_entry_refuses(case,kind):
    target=case.bundle/'unknown'
    if kind=='file':target.write_text('unlisted')
    elif kind=='directory':target.mkdir()
    else:target.symlink_to(case.bundle/'source')
    with pytest.raises(pkg.PackageError):apply(case)
    assert not (case.releases/case.new_id).exists()


def test_alias_under_a_different_filename_refuses(case):
    alias=case.units/'unrelated-alias.service';alias.symlink_to(pkg.unit_name(pkg.OWNER_NAMES[0]))
    with pytest.raises(pkg.PackageError,match='unknown_unit_link'):apply(case)
    assert alias.is_symlink() and not (case.releases/case.new_id).exists()


def test_partial_failure_restores_only_known_units_and_retains_releases(case,monkeypatch):
    original=manage.command;failed=[]
    def command(argv):
        if argv==['/usr/bin/systemctl','daemon-reload'] and not failed:
            failed.append(True);raise pkg.PackageError('synthetic_reload_failure')
        return original(argv)
    monkeypatch.setattr(manage,'command',command)
    with pytest.raises(pkg.PackageError,match='restored_units_only'):apply(case)
    assert all((case.units/n).read_bytes()==data for n,data in case.old_bytes.items())
    assert (case.releases/case.new_id).is_dir() and (case.releases/case.old_id).is_dir()
    assert not (case.base/'installations'/(case.new_id+'.json')).exists()
    assert len(list((case.base/'updates').iterdir()))==1


def test_unrecognized_unit_after_write_is_preserved_on_rollback(case,monkeypatch):
    original=manage.command;changed=[];unit=case.units/pkg.unit_name(pkg.OWNER_NAMES[0])
    def command(argv):
        if argv==['/usr/bin/systemctl','daemon-reload'] and not changed:
            changed.append(True);replacement=case.units/'foreign.fixture';replacement.write_text('UNKNOWN CONCURRENT UNIT')
            replacement.chmod(0o644);os.replace(replacement,unit)
            raise pkg.PackageError('synthetic_reload_failure')
        return original(argv)
    monkeypatch.setattr(manage,'command',command)
    with pytest.raises(pkg.PackageError,match='requires_review'):apply(case)
    assert unit.read_text()=='UNKNOWN CONCURRENT UNIT'
    assert (case.releases/case.new_id).is_dir()


def test_leaf_substitution_during_atomic_exchange_preserves_unknown_inode(case,monkeypatch):
    first=pkg.unit_name(pkg.OWNER_NAMES[0]);injected=[]
    def rename(source_fd,source,destination_fd,destination,flags):
        if flags==2 and destination==first and not injected:
            injected.append(True)
            os.rename(destination,'saved-original.fixture',src_dir_fd=destination_fd,dst_dir_fd=destination_fd)
            fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o644,dir_fd=destination_fd)
            os.write(fd,b'UNKNOWN DURING CAS');os.close(fd)
        return fake_rename(source_fd,source,destination_fd,destination,flags)
    monkeypatch.setattr(update,'_rename',rename)
    with pytest.raises(pkg.PackageError,match='requires_review'):apply(case)
    assert (case.units/first).read_bytes()==b'UNKNOWN DURING CAS'
    assert (case.units/'saved-original.fixture').read_bytes()==case.old_bytes[first]


@pytest.mark.parametrize('operation',['read','new','mkdir'])
def test_ancestor_replacement_cannot_redirect_retained_directory(tmp_path,operation):
    root=tmp_path/'tree';root.mkdir();ancestor=root/'ancestor';ancestor.mkdir();sub=ancestor/'sub';sub.mkdir()
    (sub/'public').write_text('expected')
    with update.Directory(sub,protect=False) as directory:
        ancestor.rename(root/'held');ancestor.mkdir();(ancestor/'sub').mkdir();(ancestor/'sub'/'public').write_text('unknown')
        with pytest.raises(pkg.PackageError,match='ancestor_changed'):
            if operation=='read':directory.read('public')
            elif operation=='new':directory.new('new',b'no')
            else:directory.mkdir('new')
    assert (ancestor/'sub'/'public').read_text()=='unknown' and not (ancestor/'sub'/'new').exists()


def test_plan_and_bad_ids_have_no_system_effect(case,monkeypatch):
    monkeypatch.setattr(manage,'command',lambda _:pytest.fail('plan cannot call systemctl'))
    result=update.plan_update(case.bundle,expected_old_release=case.old_id,expected_new_release=case.new_id)
    assert result['status']=='update_plan' and not (case.base/'updates').exists()
    for old,new in [('a'*12,case.new_id),(case.old_id,True),(case.old_id,case.old_id)]:
        with pytest.raises(pkg.PackageError):update.plan_update(case.bundle,expected_old_release=old,expected_new_release=new)


@pytest.mark.parametrize('change',[
    lambda v:v.update(status='incomplete_requires_review'),
    lambda v:v.update(activated=0),
    lambda v:v.update(bundle_id='f'*64),
    lambda v:v.update(extra='not permitted'),
    lambda v:v['created_accounts'][0].update(uid=True),
    lambda v:v['created_accounts'][1].update(uid=2100),
    lambda v:v['created_accounts'][1].update(gid=2100),
    lambda v:v['created_accounts'][1].update(name='unknown-owner'),
])
def test_old_installation_receipt_shape_and_identity_refuse(case,change):
    value=deepcopy(case.receipt);change(value);case.receipt_path.write_bytes(pkg.encoded(value))
    with pytest.raises(pkg.PackageError):apply(case)
    assert not (case.releases/case.new_id).exists()


@pytest.mark.parametrize('key,value',[
    ('architecture','aarch64'),('board_endpoint','https://different.example/mcp'),
    ('codex_version','0.999.0'),('python',{'path':'/usr/bin/python3.11','sha256':'a'*64}),
    ('owners',dict(zip(pkg.OWNER_NAMES,('other-a','other-b')))),
])
def test_runtime_and_owner_compatibility_is_exact(case,key,value):
    old=SimpleNamespace(manifest=deepcopy(case.old),data={'unit.service.in':(case.releases/case.old_id/'unit.service.in').read_bytes()})
    new=SimpleNamespace(manifest=deepcopy(case.new),data={'unit.service.in':(case.bundle/'unit.service.in').read_bytes()})
    new.manifest[key]=value
    with pytest.raises(pkg.PackageError,match='runtime_or_owner_changed'):update._compatible(old,new)


def test_interpreter_pin_revalidated_without_running_interpreter(case,monkeypatch):
    root=case.root/'runtime';root.mkdir();binary=root/'python3.10';binary.write_bytes(package_fixture.elf_fixture());binary.chmod(0o755)
    systemctl=root/'systemctl';systemctl.write_bytes(package_fixture.elf_fixture());systemctl.chmod(0o755)
    directory=update.Directory
    def mapped(path,*args,**kwargs):
        assert Path(path)==Path('/usr/bin')
        return directory(root,*args,**kwargs)
    monkeypatch.setattr(update,'Directory',mapped)
    with REAL_RUNTIME(case.old) as runtime:
        runtime.verify();binary.write_bytes(package_fixture.elf_fixture()+b'changed')
        with pytest.raises(pkg.PackageError,match='runtime_pin_changed'):runtime.verify()


def test_unavailable_atomic_exchange_never_falls_back(case,monkeypatch):
    import errno
    def refused(*_):return -1
    monkeypatch.setattr(update.ctypes,'CDLL',lambda *args,**kwargs:SimpleNamespace(renameat2=refused))
    monkeypatch.setattr(update.ctypes,'get_errno',lambda:errno.ENOSYS)
    with pytest.raises(pkg.PackageError,match='atomic_unit_exchange_unavailable'):REAL_RENAME(1,'a',2,'b',2)


def test_legacy_default_and_explicit_managed_template_are_separate(case):
    assert (case.releases/case.old_id/'unit.service.in').read_bytes()!=(case.bundle/'unit.service.in').read_bytes()
    assert hashlib.sha256((case.bundle/'unit.service.in').read_bytes()).hexdigest()==update.MANAGED_TEMPLATE
    assert ' managed-run\n' in (case.bundle/'unit.service.in').read_text()
    assert ' run\n' in (case.releases/case.old_id/'unit.service.in').read_text()


def test_exclusive_install_lock_refusal_has_no_writes(case):
    import fcntl
    with (case.base/'.install.lock').open('r+b') as held:
        fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(pkg.PackageError,match='installation_in_progress'):apply(case)
    assert not (case.releases/case.new_id).exists() and not (case.base/'updates').exists()


def test_public_systemd_template_names_are_not_mistaken_for_overrides(case):
    nested=case.units/'unrelated@.service.d';nested.mkdir();(nested/'public-fixture.conf').write_text('# metadata-only fixture')
    update._links()


def test_bundle_revalidation_rejects_late_unknown_empty_directory(case):
    with update.Bundle(case.bundle,case.new_id) as bundle:
        (case.bundle/'late-unknown').mkdir()
        with pytest.raises(pkg.PackageError,match='inventory_changed'):bundle.verify()


def test_account_observation_reads_metadata_and_pins_home_inode(case,monkeypatch):
    accounts={name:{'name':name,'uid':case.actual_uid+i,'gid':case.actual_gid+i} for i,name in enumerate(pkg.OWNER_NAMES)}
    original_stat=update.Directory.stat
    def stat_home(directory,name):
        info=original_stat(directory,name)
        if directory.path==case.base/'owners' and name in accounts:
            values={key:getattr(info,key) for key in ('st_dev','st_ino','st_mode','st_uid','st_gid')}
            values.update(st_uid=accounts[name]['uid'],st_gid=accounts[name]['gid'])
            return SimpleNamespace(**values)
        return info
    monkeypatch.setattr(update.Directory,'stat',stat_home)
    monkeypatch.setattr(update.pwd,'getpwnam',lambda name:SimpleNamespace(pw_name=name,pw_uid=accounts[name]['uid'],
        pw_gid=accounts[name]['gid'],pw_dir=str(case.base/'owners'/name),pw_shell='/bin/bash'))
    original_open=os.open
    def guarded_open(path,*args,**kwargs):
        assert str(path) not in pkg.OWNER_NAMES,'home directory contents must never be opened'
        assert not str(path).startswith(str(case.base/'owners')+'/'),'owner file must never be opened'
        return original_open(path,*args,**kwargs)
    monkeypatch.setattr(update.os,'open',guarded_open)
    before=REAL_ACCOUNTS(accounts)
    first=case.base/'owners'/pkg.OWNER_NAMES[0];first.rename(case.base/'owners'/'held-old-home');first.mkdir(mode=0o700)
    after=REAL_ACCOUNTS(accounts)
    assert before!=after and before[pkg.OWNER_NAMES[0]]['home_identity']!=after[pkg.OWNER_NAMES[0]]['home_identity']
    # The guard covers product observation, not synthetic fixture teardown.
    monkeypatch.setattr(update.os,'open',original_open)
