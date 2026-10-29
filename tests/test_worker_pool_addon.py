"""Public synthetic bundle/root boundary; no root command, service or CLI runs."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest

from deploy.worker_pool import package as p,manage,entry


LIMITS={'memory_bytes':6*1024**3,'memory_high_bytes':4*1024**3,'tasks':256,
    'cpu_percent':300,'lifetime_seconds':3600,'stop_seconds':45}


def elf():
    raw=bytearray(64);raw[:7]=b'\x7fELF\x02\x01\x01';struct.pack_into('<HH',raw,16,2,62)
    return bytes(raw)


@pytest.fixture
def bundle(tmp_path,monkeypatch):
    directory=tmp_path/'bundle';directory.mkdir(mode=0o700)
    template=Path('deploy/worker_pool/unit.service.in').read_bytes()
    files={name:b'pass\n' for name in ('pool_entry.py','pool_package.py','pool_manage.py','source/connect.py',
        *('source/blackoutburst_agents/'+name+'.py' for name in p.REQUIRED_SDK_MODULES))}
    files.update({'unit.service.in':template,'tools/codex':elf(),'tools/codex-resources/bwrap':elf(),'tools/gh':elf()})
    value={'schema':p.SCHEMA,'revision':'a'*40,'adapter_version':'0.32.0','architecture':'x86_64',
        'unix_owner':'bb-agent-thescreamer','board_owner':'owner','board_endpoint':'https://board.test/mcp',
        'python':{'path':'/usr/bin/python3.10','sha256':'b'*64},'codex_version':'0.163.0','gh_version':'2.82.1',
        'unit_limits':dict(LIMITS),'files':{name:{'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()} for name,raw in files.items()}}
    for name,raw in {**files,'manifest.json':p.encoded(value)}.items():
        path=directory/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    install=tmp_path/'install';install.mkdir()
    monkeypatch.setattr(p,'BASE',install/'state');monkeypatch.setattr(p,'RELEASES',install/'opt'/'releases')
    monkeypatch.setattr(p,'UNIT_DIR',install/'units');p.UNIT_DIR.mkdir()
    return directory,value,files


def test_closed_addon_inventory_and_readonly_plan_never_calls_systemctl(bundle,monkeypatch):
    directory,value,_=bundle
    monkeypatch.setattr(manage,'command',lambda *_:pytest.fail('plan executed command'))
    assert p.load_bundle(directory)==value
    plan=manage.install_addon(directory)
    assert plan['activated'] is False and plan['execution_qualified'] is False
    assert plan['service']=='blackoutburst-agent-pool-bb-agent-thescreamer.service'
    assert not p.BASE.exists() and not p.RELEASES.exists()


@pytest.mark.parametrize('module',['cloud_workflow','review_lifecycle','review_lifecycle_client','review_pool','development_pool_delivery','development_client','development_frame','development_authorization'])
def test_new_workflow_release_cannot_omit_essential_sdk_modules(bundle,module):
    directory,value,_=bundle
    del value['files']['source/blackoutburst_agents/'+module+'.py']
    (directory/'manifest.json').write_bytes(p.encoded(value))
    with pytest.raises(p.PackageError,match='pool_inventory_invalid'):manage.plan_addon(directory)
    assert not p.RELEASES.exists() and not p.BASE.exists()


@pytest.mark.parametrize('version',['0.32.0','0.34.0','0.34.99'])
def test_pre_pin_releases_still_verify_without_the_new_module(bundle,version):
    directory,value,_=bundle;value['adapter_version']=version
    (directory/'manifest.json').write_bytes(p.encoded(value))
    assert p.load_bundle(directory)==value
    assert 'protected_public_pins' not in p.required_sdk_modules(version)


@pytest.mark.parametrize('version',['0.35.0','0.35.1','0.36.0','0.44.1'])
def test_protected_pin_releases_require_the_new_module_before_install(bundle,version):
    directory,value,_=bundle;value['adapter_version']=version
    (directory/'manifest.json').write_bytes(p.encoded(value))
    with pytest.raises(p.PackageError,match='pool_inventory_invalid'):manage.plan_addon(directory)
    assert not p.RELEASES.exists() and not p.BASE.exists()
    name='source/blackoutburst_agents/protected_public_pins.py';raw=b'# synthetic pin module\n'
    (directory/name).write_bytes(raw)
    value['files'][name]={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    (directory/'manifest.json').write_bytes(p.encoded(value))
    assert p.load_bundle(directory)==value


def test_public_git_preparation_and_isolated_standalone_plan(tmp_path):
    source=tmp_path/'public-source';source.mkdir(mode=0o700)
    for name in ('package.py','manage.py','entry.py','unit.service.in'):
        target=source/'deploy/worker_pool'/name;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes((Path('deploy/worker_pool')/name).read_bytes())
    for name,raw in {'connect.py':b'pass\n','integrations/codex-personal/pyproject.toml':b'version = "0.32.0"\n',
            **{'integrations/codex-personal/blackoutburst_agents/'+name:b'pass\n' for name in
                (name+'.py' for name in p.REQUIRED_SDK_MODULES)}}.items():
        path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    def git(*args):return manage.command(['/usr/bin/git','-C',str(source),*args])
    git('init','--quiet','--initial-branch=main');git('config','user.name','Synthetic package test')
    git('config','user.email','synthetic@example.invalid');git('add','.');git('commit','--quiet','-m','Synthetic public package')
    tools={}
    for name in ('codex','bwrap','gh'):
        path=tmp_path/name;path.write_bytes(elf());tools[name]=str(path)
    output=tmp_path/'prepared'
    result=manage.prepare_addon(source,output,**tools,codex_version='0.163.0',gh_version='2.82.1',
        python_pin={'path':'/usr/bin/python3.10','sha256':'b'*64},unix_owner='bb-agent-thescreamer',
        board_owner='owner',board_endpoint='https://board.test/mcp',unit_limits=dict(LIMITS))
    manifest=p.load_bundle(output)
    assert result['bundle_id']==p.manifest_id(manifest) and result['activated'] is False
    assert (output/'pool_entry.py').read_bytes()==Path('deploy/worker_pool/entry.py').read_bytes()
    completed=subprocess.run([sys.executable,'-I','-B',str(output/'pool_manage.py'),'plan',str(output)],
        capture_output=True,check=True,timeout=10)
    plan=json.loads(completed.stdout)
    assert plan['bundle_id']==result['bundle_id'] and plan['activated'] is False and not completed.stderr
    assert plan['execution_qualified'] is False


@pytest.mark.parametrize('change',['unknown_tool','missing_gh','wrong_hash','symlink','hardlink','extra','bad_limits'])
def test_public_bundle_fraud_refuses_before_install(bundle,change,tmp_path):
    directory,value,_=bundle
    if change=='unknown_tool':value['files']['tools/shell']=value['files']['tools/gh']
    elif change=='missing_gh':del value['files']['tools/gh']
    elif change=='wrong_hash':value['files']['tools/gh']['sha256']='0'*64
    elif change=='symlink':
        path=directory/'tools/gh';raw=path.read_bytes();path.unlink();target=tmp_path/'public-target';target.write_bytes(raw);path.symlink_to(target)
    elif change=='hardlink':__import__('os').link(directory/'tools/gh',tmp_path/'hardlink')
    elif change=='extra':(directory/'unexpected').write_bytes(b'extra')
    else:value['unit_limits']['memory_high_bytes']=value['unit_limits']['memory_bytes']
    (directory/'manifest.json').write_bytes(p.encoded(value))
    with pytest.raises(p.PackageError):manage.install_addon(directory)
    assert not p.BASE.exists()


def root_fixture(bundle,monkeypatch,tmp_path):
    directory,value,_=bundle;calls=[]
    account=SimpleNamespace(pw_uid=12345,pw_gid=12345,pw_dir='/var/lib/blackoutburst-agent-host/owners/bb-agent-thescreamer')
    monkeypatch.setattr(manage,'_root_context',lambda _:account)
    monkeypatch.setattr(p,'protected',lambda _:None)
    monkeypatch.setattr(manage,'_root_lock',lambda _:None)
    monkeypatch.setattr(manage.os,'chown',lambda *args:None)
    def system(argv):
        calls.append(argv)
        assert argv[0]=='/usr/bin/systemctl' and argv[1] in ('daemon-reload','show')
        if argv[1]=='daemon-reload':return b''
        if not (p.UNIT_DIR/p.unit_name(value['unix_owner'])).exists():
            return b'LoadState=not-found\nActiveState=inactive\nSubState=dead\nUnitFileState=\nMainPID=0\nControlPID=0\nDropInPaths=\nFragmentPath=\n'
        return ('LoadState=loaded\nActiveState=inactive\nSubState=dead\nUnitFileState=disabled\nMainPID=0\nControlPID=0\nDropInPaths=\nFragmentPath='+str(p.UNIT_DIR/p.unit_name(value['unix_owner']))+'\n').encode()
    monkeypatch.setattr(manage,'command',system)
    return directory,value,account,calls


def test_apply_creates_only_new_disabled_unit_and_rollback_preserves_data(bundle,monkeypatch,tmp_path):
    directory,value,account,calls=root_fixture(bundle,monkeypatch,tmp_path)
    old=tmp_path/'old-host-sentinel';old.write_bytes(b'old R74 unchanged')
    result=manage.install_addon(directory,apply=True,expected_bundle_id=p.manifest_id(value))
    assert result['status']=='installed_disabled' and result['activated'] is False
    root=p.owner_root(value['unix_owner']);(root/'batches'/'preserved').write_bytes(b'never remove evidence')
    unit=p.UNIT_DIR/result['service'];raw=unit.read_text()
    for text in ('Restart=no','CPUQuota=300%','MemoryMax=6442450944','MemoryHigh=4294967296',
            'TasksMax=256','RuntimeMaxSec=3600','TimeoutStopSec=45','run-workflow --batch','--workflow'):
        assert text in raw
    assert 'owner_entry' not in raw and 'Environment=HOME' not in raw and 'CODEX_HOME=' not in raw
    assert old.read_bytes()==b'old R74 unchanged'
    assert manage.rollback_addon(result['receipt'])['status']=='rollback_plan'
    monkeypatch.setattr(manage.os,'geteuid',lambda:0)
    assert manage.rollback_addon(result['receipt'],apply=True)['status']=='new_unit_removed'
    assert not unit.exists() and (root/'batches'/'preserved').read_bytes()==b'never remove evidence'
    assert old.read_bytes()==b'old R74 unchanged'
    assert all(argv[1] in ('show','daemon-reload') for argv in calls)


def test_install_refuses_existing_unit_or_layout_without_overwrite(bundle,monkeypatch,tmp_path):
    directory,value,_,calls=root_fixture(bundle,monkeypatch,tmp_path)
    unit=p.UNIT_DIR/p.unit_name(value['unix_owner']);unit.write_bytes(b'pre-existing unknown unit')
    with pytest.raises(p.PackageError,match='pool_unit_already_exists'):manage.install_addon(directory,apply=True,expected_bundle_id=p.manifest_id(value))
    assert unit.read_bytes()==b'pre-existing unknown unit' and calls==[]


def test_rollback_refuses_live_or_changed_unit(bundle,monkeypatch,tmp_path):
    directory,value,_,_=root_fixture(bundle,monkeypatch,tmp_path)
    result=manage.install_addon(directory,apply=True,expected_bundle_id=p.manifest_id(value));monkeypatch.setattr(manage.os,'geteuid',lambda:0)
    monkeypatch.setattr(manage,'_inactive',lambda _:p.fail('pool_unit_not_inactive_disabled'))
    with pytest.raises(p.PackageError,match='pool_unit_not_inactive_disabled'):manage.rollback_addon(result['receipt'],apply=True)
    monkeypatch.setattr(manage,'_inactive',lambda _:None)
    unit=p.UNIT_DIR/p.unit_name(value['unix_owner']);unit.write_bytes(b'changed')
    with pytest.raises(p.PackageError,match='pool_unit_changed'):manage.rollback_addon(result['receipt'],apply=True)


def test_install_crash_does_not_adopt_partial_release(bundle,monkeypatch,tmp_path):
    directory,value,_,_=root_fixture(bundle,monkeypatch,tmp_path);original=manage.command
    def crash(argv):
        if argv[1]=='daemon-reload':raise RuntimeError('synthetic crash after unit write')
        return original(argv)
    monkeypatch.setattr(manage,'command',crash)
    with pytest.raises(RuntimeError):manage.install_addon(directory,apply=True,expected_bundle_id=p.manifest_id(value))
    assert (p.UNIT_DIR/p.unit_name(value['unix_owner'])).exists()
    with pytest.raises(p.PackageError):manage.install_addon(directory,apply=True,expected_bundle_id=p.manifest_id(value))
    assert not list((p.BASE/'installations').glob('*.complete.json'))


def test_root_context_does_not_create_accounts(bundle,monkeypatch):
    _,value,_=bundle
    monkeypatch.setattr(manage.os,'geteuid',lambda:0);monkeypatch.setattr(manage.platform,'system',lambda:'Linux')
    monkeypatch.setattr(manage.platform,'machine',lambda:'x86_64')
    monkeypatch.setattr(manage.pwd,'getpwnam',lambda _:(_ for _ in ()).throw(KeyError()))
    with pytest.raises(p.PackageError,match='pool_existing_account_required'):manage._root_context(value)


def test_unit_lifecycle_and_source_pins_checked_before_sdk_import(bundle,monkeypatch,capsys):
    directory,value,_=bundle
    monkeypatch.setattr(p,'verify_release',lambda _:(_ for _ in ()).throw(p.PackageError('wrong release')))
    assert entry.main(['--release',str(directory),'--owner',value['unix_owner'],'run-batch','--batch','/unused/batch.json'])==3
    assert 'needs_review' in capsys.readouterr().out


def test_coherent_bundle_replacement_during_copy_cannot_change_selected_release(bundle,monkeypatch,tmp_path):
    directory,value,_,_=root_fixture(bundle,monkeypatch,tmp_path)
    original=p.read_regular;changed=False
    def replace(path,*args,**kwargs):
        nonlocal changed
        if Path(path)==directory/'manifest.json' and (p.BASE/'installations').exists() and not changed:
            changed=True
            value['revision']='c'*40
            raw=b'print("replacement public entry")\n'
            value['files']['pool_entry.py']={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
            (directory/'pool_entry.py').write_bytes(raw);(directory/'manifest.json').write_bytes(p.encoded(value))
        return original(path,*args,**kwargs)
    monkeypatch.setattr(p,'read_regular',replace)
    with pytest.raises(p.PackageError,match='pool_bundle_selection_changed'):manage.install_addon(directory,apply=True,expected_bundle_id=p.manifest_id(value))
    assert changed and not list(p.RELEASES.glob('[0-9a-f]'*64))
    assert not (p.UNIT_DIR/p.unit_name(value['unix_owner'])).exists()


@pytest.mark.skipif(__import__('sys').platform!='linux',reason='real current-PID procfs requires Linux')
def test_current_cgroup_reads_real_size_zero_proc_file():
    import os
    assert Path('/proc/'+str(os.getpid())+'/cgroup').stat().st_size==0
    raw=entry.current_cgroup();assert raw and b':' in raw and len(raw)<=65536


def test_dangling_unit_alias_is_not_adopted(bundle,monkeypatch,tmp_path):
    directory,value,_,calls=root_fixture(bundle,monkeypatch,tmp_path)
    (p.UNIT_DIR/'unrelated-name.service').symlink_to(p.unit_name(value['unix_owner']))
    with pytest.raises(p.PackageError,match='pool_unit_already_exists'):manage.install_addon(directory,apply=True,expected_bundle_id=p.manifest_id(value))
    assert calls==[] and not p.BASE.exists()


def test_rollback_rechecks_exact_unit_after_public_state_read(bundle,monkeypatch,tmp_path):
    directory,value,_,_=root_fixture(bundle,monkeypatch,tmp_path)
    result=manage.install_addon(directory,apply=True,expected_bundle_id=p.manifest_id(value));monkeypatch.setattr(manage.os,'geteuid',lambda:0)
    unit=p.UNIT_DIR/p.unit_name(value['unix_owner'])
    monkeypatch.setattr(manage,'_inactive',lambda _:unit.write_bytes(b'new unknown unit'))
    with pytest.raises(p.PackageError,match='pool_unit_changed'):manage.rollback_addon(result['receipt'],apply=True)
    assert unit.read_bytes()==b'new unknown unit'


def test_canonical_anchor_helper_creates_only_missing_directories_never_reads_old_files(tmp_path,monkeypatch):
    home=tmp_path/'normal-home';home.mkdir(mode=0o700)
    account=SimpleNamespace(pw_dir=str(home))
    first=entry.ensure_development_anchor(account)
    anchor=home/'.local/state/blackoutburst-agents/development'
    assert first['anchor']==str(anchor) and len(first['created'])==4
    (anchor/'fence.lock').write_bytes(b'old lock sentinel');(anchor/'evidence').write_bytes(b'old evidence sentinel')
    monkeypatch.setattr(p,'read_regular',lambda *_a,**_k:pytest.fail('read existing anchor contents'))
    again=entry.ensure_development_anchor(account)
    assert again['created']==[] and again['changed_existing'] is False
    assert (anchor/'fence.lock').read_bytes()==b'old lock sentinel' and (anchor/'evidence').read_bytes()==b'old evidence sentinel'


@pytest.mark.parametrize('change',['symlink','mode','foreign_owner'])
def test_anchor_helper_refuses_existing_untrusted_directory_without_chmod(tmp_path,monkeypatch,change):
    home=tmp_path/'normal-home';home.mkdir(mode=0o700);local=home/'.local'
    if change=='symlink':
        target=tmp_path/'target';target.mkdir(mode=0o700);local.symlink_to(target)
    else:local.mkdir(mode=0o755 if change=='mode' else 0o700)
    if change=='foreign_owner':monkeypatch.setattr(entry.os,'geteuid',lambda:__import__('os').getuid()+1000)
    with pytest.raises((p.PackageError,OSError)):entry.ensure_development_anchor(SimpleNamespace(pw_dir=str(home)))
    assert not (local/'state').exists()
    if change=='mode':assert local.stat().st_mode&0o777==0o755
