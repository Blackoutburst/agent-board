"""Opt-in entry/package boundaries; no root/service/provider execution."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from deploy.worker_pool import entry,package as p,manage
from test_continuation_pool_configuration import case
from test_worker_pool_addon import bundle,elf,LIMITS


def entry_fixture(monkeypatch,root,version='0.45.0'):
    release=root.parent/'release'
    manifest={'adapter_version':version,'unix_owner':'bb-agent-thescreamer','board_owner':'owner',
        'board_endpoint':'https://board.invalid/mcp','unit_limits':dict(LIMITS),
        'python':{'path':'/usr/bin/python3.10'},'codex_version':'0.163.0'}
    monkeypatch.setattr(entry.p,'verify_release',lambda _:manifest)
    monkeypatch.setattr(entry,'owner_context',lambda *_:SimpleNamespace(pw_dir='/synthetic/home'))
    monkeypatch.setattr(entry.p,'owner_root',lambda _:root)
    monkeypatch.setattr(entry.sys,'path',list(entry.sys.path))
    def argv(action,*flags):return ['--release',str(release),'--owner','bb-agent-thescreamer',action,*flags]
    return release,manifest,argv


def test_real_plan_reads_only_operator_documents_and_never_creates_state(case,monkeypatch,capsys):
    root,path,*_=case;_,_,argv=entry_fixture(monkeypatch,root)
    before={str(x.relative_to(root)):x.read_bytes() for x in root.rglob('*') if x.is_file()}
    import socket,subprocess
    def deny(*_a,**_k):raise AssertionError('plan must not start/connect')
    monkeypatch.setattr(socket,'socket',deny);monkeypatch.setattr(subprocess,'Popen',deny)
    monkeypatch.setattr(entry,'in_continuation_unit',deny)
    assert entry.main(argv('continuation-plan','--continuation',str(path)))==0
    value=json.loads(capsys.readouterr().out)
    assert value['status']=='plan' and value['activated'] is value['execution_qualified'] is value['launchable'] is False
    assert 'PRIVATE_' not in json.dumps(value)
    assert before=={str(x.relative_to(root)):x.read_bytes() for x in root.rglob('*') if x.is_file()}
    assert set(x.name for x in root.iterdir())=={'config'}


@pytest.mark.parametrize('version',['0.32.0','0.34.0','0.44.0','0.44.1'])
@pytest.mark.parametrize('action',['continuation-plan','run-continuation-once'])
def test_older_versions_refuse_before_configuration_or_new_unit_read(tmp_path,monkeypatch,capsys,version,action):
    root=tmp_path/'absent';_,_,argv=entry_fixture(monkeypatch,root,version)
    monkeypatch.setattr(entry,'in_continuation_unit',lambda *_:pytest.fail('old version observed new unit'))
    assert entry.main(argv(action,'--continuation',str(root/'config/missing.json')))==3
    assert json.loads(capsys.readouterr().out)['reason']=='pool_continuation_version_required'
    assert not root.exists()


@pytest.mark.parametrize('action,flags',[
    ('continuation-plan',[]),('run-continuation-once',[]),
    ('continuation-plan',['--continuation','/a/config.json','--batch','/a/batch.json']),
    ('run-continuation-once',['--continuation','/a/config.json','--workflow','/a/workflow.json']),
    ('batch-plan',['--continuation','/a/config.json']),('run-workflow',['--continuation','/a/config.json']),
    ('init-development-anchor',['--continuation','/a/config.json'])])
def test_mixed_or_missing_flags_refuse_before_any_release_access(monkeypatch,capsys,action,flags):
    monkeypatch.setattr(p,'verify_release',lambda *_:pytest.fail('mixed flags accessed release'))
    assert entry.main(['--release','/release','--owner','owner',action,*flags])==3
    assert json.loads(capsys.readouterr().out)['status']=='needs_review'


@pytest.mark.parametrize('fault',['text','cgroup','old_unit'])
def test_run_requires_new_exact_unit_before_service_import(tmp_path,monkeypatch,capsys,fault):
    root=tmp_path/'owner';release,manifest,argv=entry_fixture(monkeypatch,root)
    unit=p.continuation_unit_name(manifest['unix_owner']);expected=b'exact rendered public unit\n'
    monkeypatch.setattr(p,'protected',lambda _:None)
    monkeypatch.setattr(p,'render_continuation_unit',lambda *_:expected.decode())
    monkeypatch.setattr(p,'read_regular',lambda path: b'public template' if str(path).endswith('.in') else (b'wrong' if fault=='text' else expected))
    cg=unit if fault=='text' else (p.unit_name(manifest['unix_owner']) if fault=='old_unit' else 'unrelated.service')
    monkeypatch.setattr(entry,'current_cgroup',lambda:('0::/system.slice/'+cg+'\n').encode())
    assert entry.main(argv('run-continuation-once','--continuation',str(root/'config/continuation.json')))==3
    value=json.loads(capsys.readouterr().out)
    assert value['reason'] in ('pool_continuation_unit_changed','pool_continuation_unit_required')
    assert not root.exists()


def upgraded_manifest(value):
    value=deepcopy(value);value['adapter_version']='0.45.0'
    for name in p.required_sdk_modules('0.45.0'):
        value['files'].setdefault('source/blackoutburst_agents/'+name+'.py',{'size':5,'sha256':hashlib.sha256(b'pass\n').hexdigest()})
    for name in ('continuation.service.in','continuation.timer.in'):
        raw=Path('deploy/worker_pool',name).read_bytes()
        value['files'][name]={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    return value


@pytest.mark.parametrize('missing',[*p.CONTINUATION_SDK_MODULES,'continuation.service.in','continuation.timer.in'])
def test_version45_manifest_cannot_omit_new_controller_modules_or_templates(bundle,missing):
    _,old,_=bundle;value=upgraded_manifest(old)
    assert p.validate_manifest(value)==value
    name=missing if missing.endswith('.in') else 'source/blackoutburst_agents/'+missing+'.py'
    value['files'].pop(name)
    with pytest.raises(p.PackageError,match='pool_inventory_invalid'):p.validate_manifest(value)


@pytest.mark.parametrize('version',['0.32.0','0.34.99','0.35.0','0.44.1'])
def test_old_manifests_remain_closed_without_continuation(bundle,version):
    _,old,_=bundle;value=deepcopy(old);value['adapter_version']=version
    if version not in ('0.32.0','0.34.99'):
        value['files']['source/blackoutburst_agents/protected_public_pins.py']={'size':5,'sha256':hashlib.sha256(b'pass\n').hexdigest()}
    assert p.validate_manifest(value)==value
    value['files']['continuation.timer.in']={'size':1,'sha256':'a'*64}
    with pytest.raises(p.PackageError):p.validate_manifest(value)


def test_new_unit_limits_and_hardening_match_legacy_and_timer_is_inert():
    old=Path('deploy/worker_pool/unit.service.in').read_text().splitlines()
    new=Path('deploy/worker_pool/continuation.service.in').read_text().splitlines()
    assert [line for line in old if not line.startswith(('Description=','ExecStart='))]==[line for line in new if not line.startswith(('Description=','ExecStart='))]
    assert 'run-continuation-once --continuation @ROOT@/config/continuation.json' in '\n'.join(new)
    timer=Path('deploy/worker_pool/continuation.timer.in').read_text()
    assert all(value in timer for value in ('OnBootSec=30s','OnUnitInactiveSec=30s','Persistent=false','Unit=@CONTINUATION_SERVICE@'))
    assert p.continuation_unit_name('bb-agent-thescreamer')!=p.unit_name('bb-agent-thescreamer')
    assert p.continuation_unit_name('bb-agent-thescreamer',timer=True).endswith('.timer')
    assert p.continuation_timer_name('bb-agent-thescreamer')==p.continuation_unit_name('bb-agent-thescreamer',timer=True)


@pytest.mark.parametrize('version',['0.44.1','0.45.0'])
def test_prepare_addon_packages_new_templates_only_for_explicit_new_version(tmp_path,version):
    source=tmp_path/'source';source.mkdir(mode=0o700)
    names=('package.py','manage.py','entry.py','unit.service.in','continuation.service.in','continuation.timer.in')
    for name in names:
        dest=source/'deploy/worker_pool'/name;dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_bytes(Path('deploy/worker_pool',name).read_bytes())
    contents={'connect.py':b'pass\n','integrations/codex-personal/pyproject.toml':('version = "'+version+'"\n').encode(),
        **{'integrations/codex-personal/blackoutburst_agents/'+name+'.py':b'pass\n' for name in p.required_sdk_modules(version)}}
    for name,raw in contents.items():
        dest=source/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(raw)
    def git(*args):return manage.command(['/usr/bin/git','-C',str(source),*args])
    git('init','--quiet','--initial-branch=main');git('config','user.name','Synthetic continuation packaging');git('config','user.email','synthetic@example.invalid')
    git('add','.');git('commit','--quiet','-m','Synthetic package')
    tools={}
    for name in ('codex','bwrap','gh'):
        path=tmp_path/name;path.write_bytes(elf());tools[name]=str(path)
    output=tmp_path/'bundle'
    result=manage.prepare_addon(source,output,**tools,codex_version='0.163.0',gh_version='2.94.0',python_pin={'path':'/usr/bin/python3.10','sha256':'b'*64},
        unix_owner='bb-agent-thescreamer',board_owner='owner',board_endpoint='https://board.invalid/mcp',unit_limits=dict(LIMITS))
    assert result['activated'] is result['execution_qualified'] is False
    value=p.load_bundle(output)
    assert all((name in value['files'])==(version=='0.45.0') for name in ('continuation.service.in','continuation.timer.in'))


def test_new_template_rendering_is_manifest_bound_and_refuses_old_release(bundle):
    _,old,_=bundle;value=upgraded_manifest(old);release=p.RELEASES/p.manifest_id(value)
    home='/synthetic/normal-home'
    service=p.render_continuation_unit(Path('deploy/worker_pool/continuation.service.in').read_text(),value,release,home)
    timer=p.render_continuation_unit(Path('deploy/worker_pool/continuation.timer.in').read_text(),value,release,home)
    assert '@' not in service+timer
    assert 'MemoryMax='+str(LIMITS['memory_bytes']) in service and 'RuntimeMaxSec=3600' in service
    assert '--continuation '+str(p.owner_root(value['unix_owner'])/'config/continuation.json') in service
    assert 'Unit='+p.continuation_unit_name(value['unix_owner']) in timer
    with pytest.raises(p.PackageError,match='pool_continuation_version_required'):
        p.render_continuation_unit('no placeholders',old,p.RELEASES/p.manifest_id(old),home)
    with pytest.raises(p.PackageError,match='unexpected_pool_release'):
        p.render_continuation_unit('no placeholders',value,p.RELEASES/('f'*64),home)
