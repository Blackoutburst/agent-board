"""Synthetic root/systemd only; no owner files or actual service operations."""
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from deploy.worker_pool import manage,package as p
from test_worker_pool_addon import bundle,root_fixture

@pytest.fixture
def installed45(bundle,monkeypatch,tmp_path):
    directory,value,_=bundle;value['adapter_version']='0.45.0'
    for name in p.required_sdk_modules('0.45.0'):
        key='source/blackoutburst_agents/'+name+'.py'
        if key not in value['files']:
            raw=b'pass\n';(directory/key).write_bytes(raw)
            value['files'][key]={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    for name in ('continuation.service.in','continuation.timer.in'):
        raw=Path('deploy/worker_pool',name).read_bytes();(directory/name).write_bytes(raw)
        value['files'][name]={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    (directory/'manifest.json').write_bytes(p.encoded(value))
    _,_,_,calls=root_fixture(bundle,monkeypatch,tmp_path)
    account=SimpleNamespace(pw_uid=os.getuid(),pw_gid=os.getgid(),pw_dir='/synthetic/owner')
    monkeypatch.setattr(manage,'_root_context',lambda _:account)
    def verify(path):
        result=p.load_bundle(path)
        assert Path(path)==p.RELEASES/p.manifest_id(result)
        return result
    monkeypatch.setattr(p,'verify_release',verify)
    def system(argv):
        calls.append(argv);assert argv[0]=='/usr/bin/systemctl'
        if argv[1]=='daemon-reload':return b''
        assert argv[1]=='show';unit=argv[-1];present=(p.UNIT_DIR/unit).exists()
        fields={'LoadState':'loaded' if present else 'not-found','ActiveState':'inactive','SubState':'dead',
            'UnitFileState':'disabled' if present else '', 'MainPID':'0','ControlPID':'0',
            'DropInPaths':'','FragmentPath':str(p.UNIT_DIR/unit) if present else ''}
        if unit.endswith('.timer'):
            fields.pop('MainPID');fields.pop('ControlPID')
            assert 'MainPID' not in argv[-2] and 'ControlPID' not in argv[-2]
        return ''.join(k+'='+v+'\n' for k,v in fields.items()).encode()
    monkeypatch.setattr(manage,'command',system)
    first=manage.install_addon(directory,apply=True,expected_bundle_id=p.manifest_id(value))
    root=p.owner_root(value['unix_owner']);sentinel=root/'batches/old-evidence';sentinel.write_bytes(b'old state retained')
    read=p.read_regular
    def public_only(path,*a,**k):
        assert not Path(path).is_relative_to(root),'installer read private owner contents'
        return read(path,*a,**k)
    monkeypatch.setattr(p,'read_regular',public_only);calls.clear()
    return SimpleNamespace(value=value,release=p.RELEASES/p.manifest_id(value),first=first,root=root,
        sentinel=sentinel,calls=calls,system=system)

def apply(s):return manage.install_continuation(s.release,apply=True,expected_bundle_id=p.manifest_id(s.value))

def test_plan_reads_only_public_release(installed45,monkeypatch):
    s=installed45
    monkeypatch.setattr(manage,'command',lambda *_:pytest.fail('plan invoked process'))
    monkeypatch.setattr(manage,'_root_context',lambda *_:pytest.fail('plan inspected account'))
    result=manage.install_continuation(s.release)
    assert result['status']=='continuation_install_plan' and result['activated'] is False
    assert not (s.root/'continuations').exists()
    assert not list((p.BASE/'installations').glob('*.continuation*'))

def test_adds_separate_inert_units_without_touching_private_state_or_finite_unit(installed45):
    s=installed45;old=p.UNIT_DIR/s.first['service'];old_bytes=old.read_bytes()
    before={str(x.relative_to(s.root)):x.read_bytes() for x in s.root.rglob('*') if x.is_file()}
    result=apply(s)
    assert result['status']=='continuation_installed_disabled' and result['activated'] is False
    assert result['execution_qualified'] is False
    assert all(argv[1] in ('show','daemon-reload') for argv in s.calls)
    assert sum(argv[1]=='daemon-reload' for argv in s.calls)==1
    assert old.read_bytes()==old_bytes
    assert before=={str(x.relative_to(s.root)):x.read_bytes() for x in s.root.rglob('*') if x.is_file()}
    assert not (s.root/'continuations').exists()
    for name in (result['service'],result['timer']):assert (p.UNIT_DIR/name).stat().st_mode&0o777==0o644
    receipt=p.decode(Path(result['receipt']).read_bytes())
    assert receipt['status']=='continuation_installed_disabled' and receipt['finite_unit_sha256']==hashlib.sha256(old_bytes).hexdigest()
    assert 'Persistent=false' in (p.UNIT_DIR/result['timer']).read_text()
    with pytest.raises(p.PackageError):apply(s)

@pytest.mark.parametrize('fault',['live','enabled','dropin','changed_unit','receipt','root_mode','existing_service','existing_timer','intent','complete'])
def test_unknown_or_unfinished_layout_refuses_without_new_effect(installed45,monkeypatch,fault):
    s=installed45;owner=s.value['unix_owner'];old=p.UNIT_DIR/s.first['service'];original=old.read_bytes()
    if fault in ('live','enabled','dropin'):
        def system(argv):
            raw=s.system(argv)
            if argv[1]=='show' and argv[-1]==s.first['service']:
                if fault=='live':raw=raw.replace(b'ActiveState=inactive',b'ActiveState=active').replace(b'MainPID=0',b'MainPID=8123')
                elif fault=='enabled':raw=raw.replace(b'UnitFileState=disabled',b'UnitFileState=enabled')
                else:raw=raw.replace(b'DropInPaths=',b'DropInPaths=/synthetic/override.conf')
            return raw
        monkeypatch.setattr(manage,'command',system)
    elif fault=='changed_unit':old.write_bytes(b'unknown unit')
    elif fault=='receipt':
        path=Path(s.first['receipt']);data=p.decode(path.read_bytes());data['uid']+=1;path.write_bytes(p.encoded(data))
    elif fault=='root_mode':s.root.chmod(0o755)
    elif fault.startswith('existing_'):
        name=p.continuation_unit_name(owner,timer=fault=='existing_timer');(p.UNIT_DIR/name).write_bytes(b'prior unknown')
    else:
        suffix='.continuation.json' if fault=='intent' else '.continuation.complete.json'
        (p.BASE/'installations'/(p.manifest_id(s.value)+suffix)).write_bytes(b'uncertain prior install')
    before={str(x):x.read_bytes() for x in p.UNIT_DIR.iterdir() if x.is_file()}
    with pytest.raises(p.PackageError):apply(s)
    assert before=={str(x):x.read_bytes() for x in p.UNIT_DIR.iterdir() if x.is_file()}
    assert s.sentinel.read_bytes()==b'old state retained'
    assert all(argv[1]=='show' for argv in s.calls)

@pytest.mark.parametrize('boundary',['first_unit','reload','completion_ack'])
def test_partial_install_is_preserved_uncertain_and_cannot_be_replayed(installed45,monkeypatch,boundary):
    s=installed45;write=manage._new_file;system=manage.command
    def new_file(path,raw,mode):
        if boundary=='first_unit' and path==p.UNIT_DIR/p.continuation_unit_name(s.value['unix_owner']):raise OSError('synthetic')
        write(path,raw,mode)
        if boundary=='completion_ack' and str(path).endswith('.continuation.complete.json'):raise OSError('synthetic lost ack')
    def command(argv):
        if boundary=='reload' and argv[1]=='daemon-reload':raise p.PackageError('synthetic_reload')
        return system(argv)
    monkeypatch.setattr(manage,'_new_file',new_file);monkeypatch.setattr(manage,'command',command)
    with pytest.raises((p.PackageError,OSError)):apply(s)
    assert (p.BASE/'installations'/(p.manifest_id(s.value)+'.continuation.json')).exists()
    monkeypatch.setattr(manage,'_new_file',write);monkeypatch.setattr(manage,'command',system)
    with pytest.raises((p.PackageError,OSError)):apply(s)
    assert s.sentinel.read_bytes()==b'old state retained'
    assert all(argv[1] in ('show','daemon-reload') for argv in s.calls)

def test_exact_bundle_approval_required_before_root_or_units(installed45,monkeypatch):
    s=installed45;monkeypatch.setattr(manage,'_root_context',lambda *_:pytest.fail('unapproved root access'))
    with pytest.raises(p.PackageError,match='pool_approved_bundle_required'):manage.install_continuation(s.release,apply=True,expected_bundle_id='f'*64)
    assert s.calls==[]
