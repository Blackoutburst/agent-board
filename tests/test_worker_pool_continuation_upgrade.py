"""Real temporary Root receipts/unit files; account and systemd are synthetic."""
import hashlib
import fcntl
import json
from pathlib import Path
import shutil

import pytest

from deploy.worker_pool import manage,package as p
from test_worker_pool_addon import bundle,root_fixture
from test_worker_pool_continuation_install import installed45


def next_release(s,tmp_path,version,*,changed_template=False):
    directory=tmp_path/('bundle-'+version);shutil.copytree(s.release,directory)
    value=p.decode((directory/'manifest.json').read_bytes());value.update(revision=hashlib.sha256(version.encode()).hexdigest()[:40],adapter_version=version)
    name='source/blackoutburst_agents/__init__.py';raw=('__version__='+repr(version)+'\n').encode()
    (directory/name).chmod(0o600)
    (directory/name).write_bytes(raw);value['files'][name]={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    if changed_template:
        name='continuation.timer.in';raw=(directory/name).read_bytes()+b'# unapproved template change\n'
        (directory/name).chmod(0o600)
        (directory/name).write_bytes(raw);value['files'][name]={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    (directory/'manifest.json').chmod(0o600)
    (directory/'manifest.json').write_bytes(p.encoded(value))
    manage.upgrade_addon(directory,previous_bundle_id=p.manifest_id(s.value),
        expected_bundle_id=p.manifest_id(value),apply=True)
    return p.RELEASES/p.manifest_id(value),value


@pytest.fixture
def upgrade45(installed45,monkeypatch,tmp_path,request):
    s=installed45;s.states={};system=s.system
    def command(argv):
        if argv[1]=='reset-failed':
            s.calls.append(argv);s.states.setdefault(argv[-1],{}).update(ActiveState='inactive',SubState='dead');return b''
        raw=system(argv)
        if argv[1]=='show':
            fields=dict(line.split('=',1) for line in raw.decode().splitlines());fields.update(s.states.get(argv[-1],{}))
            return ''.join(k+'='+v+'\n' for k,v in fields.items()).encode()
        return raw
    monkeypatch.setattr(manage,'command',command);s.command=command
    s.old_result=manage.install_continuation(s.release,expected_bundle_id=p.manifest_id(s.value),apply=True)
    s.old_release=s.release;s.old_value=s.value
    s.old_units={name:(p.UNIT_DIR/name).read_bytes() for name in (s.old_result['service'],s.old_result['timer'])}
    s.release,s.value=next_release(s,tmp_path,'0.45.1',changed_template=getattr(request,'param',None)=='template')
    s.finite=p.UNIT_DIR/p.unit_name(s.value['unix_owner']);s.finite_raw=s.finite.read_bytes();s.calls.clear()
    return s


def apply(s):
    return manage.upgrade_continuation(s.release,previous_bundle_id=p.manifest_id(s.old_value),
        expected_bundle_id=p.manifest_id(s.value),apply=True)


def test_plan_and_wrong_approval_never_touch_accounts_or_units(upgrade45,monkeypatch):
    s=upgrade45
    monkeypatch.setattr(manage,'command',lambda *_:pytest.fail('plan invoked systemd'))
    monkeypatch.setattr(manage,'_root_context',lambda *_:pytest.fail('unapproved account lookup'))
    result=manage.upgrade_continuation(str(s.release),previous_bundle_id=p.manifest_id(s.old_value))
    assert result['status']=='continuation_upgrade_plan' and result['activated'] is False
    with pytest.raises(p.PackageError,match='pool_approved_bundle_required'):
        manage.upgrade_continuation(s.release,previous_bundle_id=p.manifest_id(s.old_value),expected_bundle_id='f'*64,apply=True)


@pytest.mark.parametrize('failed',[False,True])
def test_real_cli_two_units_replaced_backed_up_and_canonically_receipted(upgrade45,monkeypatch,capsys,failed):
    s=upgrade45;owner=s.value['unix_owner'];service=p.continuation_unit_name(owner);timer=p.continuation_timer_name(owner)
    if failed:s.states[service]={'ActiveState':'failed','SubState':'failed'}
    replaced=[];replace=manage._replace_unit
    def capture(path,expected,replacement):
        replaced.append(path.name);return replace(path,expected,replacement)
    monkeypatch.setattr(manage,'_replace_unit',capture)
    assert manage.main(['upgrade-continuation',str(s.release),'--previous-bundle-id',p.manifest_id(s.old_value),
        '--expected-bundle-id',p.manifest_id(s.value),'--apply'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='continuation_upgraded_disabled' and result['activated'] is False and result['execution_qualified'] is False
    assert replaced==[service,timer] and s.finite.read_bytes()==s.finite_raw
    assert s.sentinel.read_bytes()==b'old state retained'
    installed=p.decode(Path(result['receipt']).read_bytes());plan=manage.continuation_install_plan(s.release)
    rendered=manage._continuation_rendered(plan,s.value,manage._root_context(s.value))
    assert installed==manage._continuation_completion(plan,s.value,manage._root_context(s.value),rendered)
    assert installed['schema']=='worker-pool-continuation-install-v1' and installed['status']=='continuation_installed_disabled'
    # The canonical completion and distinct upgrade intent have separate roles.
    intent=p.BASE/'installations'/(p.manifest_id(s.value)+'.continuation.json')
    record=p.decode(intent.read_bytes());assert record['schema']=='worker-pool-continuation-upgrade-v1'
    assert record['previous_bundle_id']==p.manifest_id(s.old_value)
    for name,suffix in ((service,'.previous-service'),(timer,'.previous-timer')):
        assert intent.with_suffix(suffix).read_bytes()==s.old_units[name]
        assert intent.with_suffix(suffix).stat().st_mode&0o777==0o600
        assert (p.UNIT_DIR/name).read_bytes()==rendered[name]
    assert sum(argv[1]=='daemon-reload' for argv in s.calls)==1
    assert [argv for argv in s.calls if argv[1]=='reset-failed']==[['/usr/bin/systemctl','reset-failed',service]]
    assert all(argv[1] in ('show','daemon-reload','reset-failed') for argv in s.calls)


def test_completed_upgrade_is_an_exact_input_for_the_next_upgrade(upgrade45,tmp_path):
    s=upgrade45;first=apply(s);old_value=s.value
    s.release,s.value=next_release(s,tmp_path,'0.45.2');s.old_value=old_value;s.calls.clear()
    second=apply(s)
    assert second['status']=='continuation_upgraded_disabled' and Path(first['receipt']).exists()
    assert s.sentinel.read_bytes()==b'old state retained'
    assert all(argv[1] in ('show','daemon-reload','reset-failed') for argv in s.calls)


@pytest.mark.parametrize('fault',['live_service','live_timer','enabled_service','enabled_timer','dropin','fragment',
    'service_bytes','timer_bytes','old_receipt','extra_old_receipt','new_finite_receipt','finite_bytes','live_finite','private_root_mode'])
def test_unknown_receipt_unit_or_state_is_refused_without_replacement(upgrade45,fault):
    s=upgrade45;service=s.old_result['service'];timer=s.old_result['timer']
    if fault=='live_service':s.states[service]={'ActiveState':'active','SubState':'running','MainPID':'123'}
    elif fault=='live_timer':s.states[timer]={'ActiveState':'active','SubState':'waiting'}
    elif fault=='enabled_service':s.states[service]={'UnitFileState':'enabled'}
    elif fault=='enabled_timer':s.states[timer]={'UnitFileState':'enabled'}
    elif fault=='dropin':s.states[timer]={'DropInPaths':'/synthetic/override.conf'}
    elif fault=='fragment':s.states[service]={'FragmentPath':'/run/systemd/system/unapproved.service'}
    elif fault in ('service_bytes','timer_bytes'):(p.UNIT_DIR/(service if fault=='service_bytes' else timer)).write_bytes(b'unknown external unit')
    elif fault in ('old_receipt','extra_old_receipt','new_finite_receipt'):
        path=Path(s.old_result['receipt']) if fault!='new_finite_receipt' else p.BASE/'installations'/(p.manifest_id(s.value)+'.complete.json')
        value=p.decode(path.read_bytes());value['uid']=-1 if fault!='extra_old_receipt' else value['uid']
        if fault=='extra_old_receipt':value['unapproved_extra']=True
        path.write_bytes(p.encoded(value))
    elif fault=='finite_bytes':s.finite.write_bytes(b'external finite unit')
    elif fault=='live_finite':s.states[s.finite.name]={'ActiveState':'active','SubState':'running','MainPID':'123'}
    else:s.root.chmod(0o755)
    before={path.name:path.read_bytes() for path in p.UNIT_DIR.iterdir() if path.is_file()}
    with pytest.raises(p.PackageError):apply(s)
    assert before=={path.name:path.read_bytes() for path in p.UNIT_DIR.iterdir() if path.is_file()}
    assert not (p.BASE/'installations'/(p.manifest_id(s.value)+'.continuation.json')).exists()
    assert all(argv[1]=='show' for argv in s.calls)


@pytest.mark.parametrize('suffix',['.json','.complete.json','.previous-service','.previous-timer'])
def test_existing_intent_completion_or_backup_is_never_adopted(upgrade45,suffix):
    s=upgrade45;intent=p.BASE/'installations'/(p.manifest_id(s.value)+'.continuation.json')
    intent.with_suffix(suffix).write_bytes(b'uncertain prior attempt')
    with pytest.raises(p.PackageError):apply(s)
    assert all((p.UNIT_DIR/name).read_bytes()==raw for name,raw in s.old_units.items())
    assert all(argv[1]=='show' for argv in s.calls)


@pytest.mark.parametrize('upgrade45',['template'],indirect=True)
def test_changed_continuation_template_is_not_implicitly_authorized(upgrade45):
    s=upgrade45
    with pytest.raises(p.PackageError,match='pool_upgrade_scope_changed'):apply(s)
    assert not s.calls


@pytest.mark.parametrize('boundary',['after_first','after_second','reload','reset_failed'])
def test_precompletion_failures_restore_both_exact_old_units(upgrade45,monkeypatch,boundary):
    s=upgrade45;replace=manage._replace_unit;command=s.command;count=[0];failed=[False]
    def replaced(path,expected,new):
        value=replace(path,expected,new);count[0]+=1
        if not failed[0] and boundary==('after_first' if count[0]==1 else 'after_second'):
            failed[0]=True;raise OSError('synthetic write acknowledgement failure')
        return value
    def commands(argv):
        if not failed[0] and boundary in ('reload','reset_failed') and argv[1]==('daemon-reload' if boundary=='reload' else 'reset-failed'):
            failed[0]=True;raise p.PackageError('synthetic systemd failure')
        return command(argv)
    monkeypatch.setattr(manage,'_replace_unit',replaced);monkeypatch.setattr(manage,'command',commands)
    with pytest.raises((p.PackageError,OSError)):apply(s)
    assert all((p.UNIT_DIR/name).read_bytes()==raw for name,raw in s.old_units.items())
    assert s.finite.read_bytes()==s.finite_raw and s.sentinel.read_bytes()==b'old state retained'
    assert not (p.BASE/'installations'/(p.manifest_id(s.value)+'.continuation.complete.json')).exists()
    assert (p.BASE/'installations'/(p.manifest_id(s.value)+'.continuation.json')).exists()
    with pytest.raises(p.PackageError):apply(s)


@pytest.mark.parametrize('after_write',[False,True])
def test_completion_ack_uncertainty_keeps_new_units_stopped_and_refuses_replay(upgrade45,monkeypatch,after_write):
    s=upgrade45;write=manage._new_file
    def uncertain(path,raw,mode):
        if path.name==p.manifest_id(s.value)+'.continuation.complete.json':
            if after_write:write(path,raw,mode)
            raise OSError('synthetic uncertain completion')
        return write(path,raw,mode)
    monkeypatch.setattr(manage,'_new_file',uncertain)
    with pytest.raises(p.PackageError,match='pool_continuation_upgrade_completion_unconfirmed'):apply(s)
    before={name:(p.UNIT_DIR/name).read_bytes() for name in s.old_units}
    assert before[s.old_result['service']]!=s.old_units[s.old_result['service']]
    with pytest.raises(p.PackageError):apply(s)
    assert before=={name:(p.UNIT_DIR/name).read_bytes() for name in s.old_units}
    assert all(argv[1] in ('show','daemon-reload','reset-failed') for argv in s.calls)


def test_external_change_is_not_overwritten_during_pair_rollback(upgrade45,monkeypatch):
    s=upgrade45;timer=p.UNIT_DIR/s.old_result['timer']
    def command(argv):
        if argv[1]=='daemon-reload':timer.write_bytes(b'external replacement');raise p.PackageError('synthetic failure')
        return s.command(argv)
    monkeypatch.setattr(manage,'command',command)
    with pytest.raises(p.PackageError,match='pool_continuation_upgrade_units_unconfirmed'):apply(s)
    assert timer.read_bytes()==b'external replacement'
    assert not (p.BASE/'installations'/(p.manifest_id(s.value)+'.continuation.complete.json')).exists()


def test_root_lock_prevents_a_second_installer_before_any_unit_read(upgrade45):
    s=upgrade45
    with (p.BASE/'install.lock').open('r+b') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(p.PackageError,match='pool_install_busy'):apply(s)
    assert not s.calls and all((p.UNIT_DIR/name).read_bytes()==raw for name,raw in s.old_units.items())


def test_state_change_between_two_replacements_remains_uncertain_not_replayed(upgrade45,monkeypatch):
    s=upgrade45;replace=manage._replace_unit;replaced=[]
    def change(path,old,new):
        result=replace(path,old,new);replaced.append(path.name)
        if len(replaced)==1:s.states[s.old_result['timer']]={'UnitFileState':'enabled'}
        return result
    monkeypatch.setattr(manage,'_replace_unit',change)
    with pytest.raises(p.PackageError,match='pool_unit_not_stopped_disabled'):apply(s)
    assert replaced==[s.old_result['service']]
    assert not (p.BASE/'installations'/(p.manifest_id(s.value)+'.continuation.complete.json')).exists()
    assert all(argv[1]=='show' for argv in s.calls)


def test_cas_refuses_a_unit_changed_immediately_before_replacement(upgrade45,monkeypatch):
    s=upgrade45;replace=manage._replace_unit;changed=[False]
    def race(path,old,new):
        if not changed[0]:changed[0]=True;path.write_bytes(b'external compare-and-replace race')
        return replace(path,old,new)
    monkeypatch.setattr(manage,'_replace_unit',race)
    with pytest.raises(p.PackageError,match='pool_continuation_upgrade_units_unconfirmed'):apply(s)
    assert (p.UNIT_DIR/s.old_result['service']).read_bytes()==b'external compare-and-replace race'
    assert all(argv[1]=='show' for argv in s.calls)
