"""Public-only upgrade boundaries; all service/root operations are synthetic."""
import hashlib
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from deploy.worker_pool import manage,package as p
from test_worker_pool_addon import bundle,root_fixture


@pytest.fixture
def installed(bundle,monkeypatch,tmp_path,request):
    directory,old,_=bundle;version=getattr(request,'param','0.34.0');old['adapter_version']=version
    if version=='0.38.0':
        name='source/blackoutburst_agents/protected_public_pins.py';raw=b'# synthetic public pin module\n'
        (directory/name).write_bytes(raw)
        old['files'][name]={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    (directory/'manifest.json').write_bytes(p.encoded(old))
    directory,old,_,calls=root_fixture(bundle,monkeypatch,tmp_path)
    account=SimpleNamespace(pw_uid=os.getuid(),pw_gid=os.getgid(),
        pw_dir='/var/lib/blackoutburst-agent-host/owners/bb-agent-thescreamer')
    monkeypatch.setattr(manage,'_root_context',lambda _:account)
    def verify(path):
        manifest=p.load_bundle(path)
        assert Path(path)==p.RELEASES/p.manifest_id(manifest)
        for name in ('manifest.json',*manifest['files']):
            assert (Path(path)/name).stat().st_mode&0o777==(0o755 if name.startswith('tools/') else 0o444)
        return manifest
    monkeypatch.setattr(p,'verify_release',verify)
    first=manage.install_addon(directory,apply=True,expected_bundle_id=p.manifest_id(old))
    new_dir=tmp_path/'new-bundle';shutil.copytree(directory,new_dir)
    new=p.decode((new_dir/'manifest.json').read_bytes());new['revision']='c'*40;new['adapter_version']=version
    path='source/blackoutburst_agents/__init__.py';raw=('__version__='+repr(version)+'\n').encode()
    (new_dir/path).write_bytes(raw);new['files'][path]={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    (new_dir/'manifest.json').write_bytes(p.encoded(new))
    unit=p.UNIT_DIR/first['service'];old_unit=unit.read_bytes()
    root=p.owner_root(old['unix_owner']);sentinel=root/'batches'/'failed-pilot-evidence';sentinel.write_bytes(b'keep this old trace')
    original_command=manage.command;state={'active':'failed','pid':'0','enabled':'disabled','dropins':''}
    def command(argv):
        assert argv[0]=='/usr/bin/systemctl'
        if argv[1]=='reset-failed':
            calls.append(argv);state['active']='inactive';return b''
        raw=original_command(argv)
        if argv[1]=='show':
            text=raw.decode().replace('ActiveState=inactive','ActiveState='+state['active'])
            text=text.replace('SubState=dead','SubState='+('failed' if state['active']=='failed' else 'dead'))
            text=text.replace('MainPID=0','MainPID='+state['pid']).replace('UnitFileState=disabled','UnitFileState='+state['enabled'])
            text=text.replace('DropInPaths=','DropInPaths='+state['dropins'])
            return text.encode()
        return raw
    monkeypatch.setattr(manage,'command',command)
    read=p.read_regular
    def public_only(path,*args,**kwargs):
        assert not Path(path).is_relative_to(root),'upgrade opened private owner contents'
        return read(path,*args,**kwargs)
    monkeypatch.setattr(p,'read_regular',public_only)
    calls.clear()
    return SimpleNamespace(directory=new_dir,old=old,new=new,first=first,unit=unit,old_unit=old_unit,
        root=root,sentinel=sentinel,state=state,calls=calls,command=command)


def apply(s):
    return manage.upgrade_addon(s.directory,previous_bundle_id=p.manifest_id(s.old),apply=True,
        expected_bundle_id=p.manifest_id(s.new))


def test_upgrade_plan_never_reads_installations_or_runs_command(bundle,monkeypatch):
    directory,old,_=bundle
    monkeypatch.setattr(manage,'command',lambda *_:pytest.fail('plan executed command'))
    plan=manage.upgrade_addon(directory,previous_bundle_id='e'*64)
    assert plan['status']=='upgrade_plan' and plan['activated'] is False
    assert not p.BASE.exists() and not p.RELEASES.exists()


@pytest.mark.parametrize('starting',['failed','inactive'])
def test_upgrade_preserves_private_history_and_stays_disabled(installed,starting):
    s=installed;s.state['active']=starting
    result=apply(s)
    assert result['status']=='upgraded_disabled' and result['activated'] is False
    assert result['preserve_private_state'] is True
    assert s.unit.read_bytes()!=s.old_unit and str(p.RELEASES/p.manifest_id(s.new)).encode() in s.unit.read_bytes()
    assert s.sentinel.read_bytes()==b'keep this old trace'
    assert Path(s.first['receipt']).exists() and (p.RELEASES/p.manifest_id(s.old)).exists()
    receipt=p.decode(Path(result['receipt']).read_bytes())
    assert receipt['status']=='installed_disabled' and receipt['previous_bundle_id']==p.manifest_id(s.old)
    assert s.state['active']=='inactive'
    assert all(row[1] in ('show','daemon-reload','reset-failed') for row in s.calls)
    assert sum(row[1]=='reset-failed' for row in s.calls)==(starting=='failed')


def test_current_pin_release_upgrades_a_receipted_pre_pin_release(installed):
    s=installed
    assert s.old['adapter_version']=='0.34.0'
    assert 'source/blackoutburst_agents/protected_public_pins.py' not in s.old['files']
    s.new['adapter_version']='0.35.0'
    name='source/blackoutburst_agents/protected_public_pins.py';raw=b'# synthetic pin module\n'
    (s.directory/name).write_bytes(raw)
    s.new['files'][name]={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    (s.directory/'manifest.json').write_bytes(p.encoded(s.new))
    result=apply(s)
    assert result['status']=='upgraded_disabled' and result['activated'] is False
    assert s.sentinel.read_bytes()==b'keep this old trace'
    assert (p.RELEASES/p.manifest_id(s.old)).exists()
    assert (p.RELEASES/p.manifest_id(s.new)/name).read_bytes()==raw


@pytest.mark.parametrize('change',['live','enabled','dropins','unit','receipt','root_mode','tool'])
def test_unknown_or_live_prior_state_is_never_adopted(installed,change):
    s=installed
    if change=='live':s.state.update(active='active',pid='12345')
    elif change=='enabled':s.state['enabled']='enabled'
    elif change=='dropins':s.state['dropins']='/unknown/override.conf'
    elif change=='unit':s.unit.write_bytes(b'unknown replacement')
    elif change=='receipt':
        path=Path(s.first['receipt']);v=p.decode(path.read_bytes());v['uid']+=1;path.write_bytes(p.encoded(v))
    elif change=='root_mode':s.root.chmod(0o755)
    else:
        path=s.directory/'tools/gh';raw=path.read_bytes()+b'changed';path.write_bytes(raw)
        s.new['files']['tools/gh']={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
        (s.directory/'manifest.json').write_bytes(p.encoded(s.new))
    expected=s.unit.read_bytes()
    with pytest.raises(p.PackageError):apply(s)
    assert s.unit.read_bytes()==expected and s.sentinel.read_bytes()==b'keep this old trace'
    assert not (p.RELEASES/p.manifest_id(s.new)).exists()
    assert all(row[1]=='show' for row in s.calls)


def test_reload_failure_restores_only_our_exact_unit(installed,monkeypatch):
    s=installed;failed=False
    def command(argv):
        nonlocal failed
        if argv[1]=='daemon-reload' and not failed:
            failed=True;raise p.PackageError('synthetic_reload_failure')
        return s.command(argv)
    monkeypatch.setattr(manage,'command',command)
    with pytest.raises(p.PackageError,match='synthetic_reload_failure'):apply(s)
    assert s.unit.read_bytes()==s.old_unit and s.sentinel.read_bytes()==b'keep this old trace'
    assert not (p.BASE/'installations'/(p.manifest_id(s.new)+'.complete.json')).exists()


def test_external_unit_change_is_not_overwritten_by_rollback(installed,monkeypatch):
    s=installed
    def command(argv):
        if argv[1]=='daemon-reload':
            s.unit.write_bytes(b'new unknown operator unit')
            raise p.PackageError('synthetic_reload_failure')
        return s.command(argv)
    monkeypatch.setattr(manage,'command',command)
    with pytest.raises(p.PackageError,match='pool_upgrade_unit_result_unconfirmed'):apply(s)
    assert s.unit.read_bytes()==b'new unknown operator unit'


def test_lost_complete_ack_retains_stopped_new_unit_and_forbids_replay(installed,monkeypatch):
    s=installed;write=manage._new_file
    def lose(path,raw,mode):
        write(path,raw,mode)
        if path.name==p.manifest_id(s.new)+'.complete.json':raise OSError('synthetic lost durable ack')
    monkeypatch.setattr(manage,'_new_file',lose)
    with pytest.raises(p.PackageError,match='pool_upgrade_completion_unconfirmed'):apply(s)
    expected=s.unit.read_bytes()
    assert expected!=s.old_unit and s.state['active']=='inactive'
    assert s.sentinel.read_bytes()==b'keep this old trace'
    with pytest.raises(p.PackageError):apply(s)
    assert s.unit.read_bytes()==expected


@pytest.mark.parametrize('installed',['0.38.0'],indirect=True)
def test_cli_upgrades_038_to_039_with_identical_unit_template_and_no_start(installed,capsys):
    s=installed;s.new['adapter_version']='0.39.0'
    name='source/blackoutburst_agents/__init__.py';raw=b'__version__="0.39.0"\n'
    (s.directory/name).write_bytes(raw)
    s.new['files'][name]={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    (s.directory/'manifest.json').write_bytes(p.encoded(s.new))
    assert s.new['files']['unit.service.in']==s.old['files']['unit.service.in']
    assert manage.main(['upgrade',str(s.directory),'--previous-bundle-id',p.manifest_id(s.old),
        '--expected-bundle-id',p.manifest_id(s.new),'--apply'])==0
    result=__import__('json').loads(capsys.readouterr().out)
    assert result['status']=='upgraded_disabled' and result['activated'] is False
    assert s.sentinel.read_bytes()==b'keep this old trace' and s.state['active']=='inactive'
    assert s.unit.read_bytes()==p.render_unit((s.directory/'unit.service.in').read_text(),s.new,
        p.RELEASES/p.manifest_id(s.new),'/var/lib/blackoutburst-agent-host/owners/bb-agent-thescreamer').encode()
    assert all(row[1] in ('show','daemon-reload','reset-failed') for row in s.calls)
