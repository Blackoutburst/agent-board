"""Exercise argparse string paths through stopped upgrades, without real root.

Historical public releases, units and receipts use real temporary files.
Only account/protection/systemd metadata are simulated by the root fixture;
reads of owner-private contents are forbidden and no service is started.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from deploy.worker_pool import manage, package as p
from test_worker_pool_addon import bundle as original_bundle, root_fixture
from test_worker_pool_netlink_upgrade import OLD_LINE, NEW_LINE, OLD_SHA, pin


def installed_transition(tmp_path, monkeypatch, old_version):
    """Install the genuine historical template through the real installer.

    Only root identity/protection/chown and systemd observations are simulated
    by the existing root fixture. All release/receipt/unit writes are real.
    """
    read_bytes = Path.read_bytes
    source = Path('deploy/worker_pool/unit.service.in')
    old_raw = read_bytes(source).replace(NEW_LINE, OLD_LINE)
    assert hashlib.sha256(old_raw).hexdigest() == OLD_SHA
    with monkeypatch.context() as temporary:
        temporary.setattr(Path, 'read_bytes', lambda path: old_raw if path == source else read_bytes(path))
        directory, previous, files = original_bundle.__wrapped__(tmp_path, monkeypatch)
    previous['adapter_version'] = old_version
    name = 'source/blackoutburst_agents/protected_public_pins.py'
    body = b'# synthetic public pin source, never executed\n'
    (directory / name).write_bytes(body)
    previous['files'][name] = pin(body)
    (directory / 'manifest.json').write_bytes(p.encoded(previous))
    directory, previous, _, calls = root_fixture((directory, previous, files), monkeypatch, tmp_path)
    account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(),
        pw_dir='/var/lib/blackoutburst-agent-host/owners/bb-agent-thescreamer')
    monkeypatch.setattr(manage, '_root_context', lambda _: account)
    def verify(path):
        value = p.load_bundle(path)
        assert Path(path) == p.RELEASES / p.manifest_id(value)
        for name in ('manifest.json', *value['files']):
            assert (Path(path)/name).stat().st_mode & 0o777 == (0o755 if name.startswith('tools/') else 0o444)
        return value
    monkeypatch.setattr(p, 'verify_release', verify)
    old_id = p.manifest_id(previous)
    first = manage.install_addon(directory, apply=True, expected_bundle_id=old_id)
    new_directory = tmp_path / 'new-transition-bundle'
    shutil.copytree(directory, new_directory)
    selected = p.decode((new_directory/'manifest.json').read_bytes())
    selected.update(adapter_version='0.37.0', revision='d'*40)
    new_raw = old_raw.replace(OLD_LINE, NEW_LINE)
    (new_directory/'unit.service.in').write_bytes(new_raw)
    selected['files']['unit.service.in'] = pin(new_raw)
    (new_directory/'manifest.json').write_bytes(p.encoded(selected))
    root = p.owner_root(previous['unix_owner'])
    sentinel = root/'batches'/'historical-failure'
    sentinel.write_bytes(b'old evidence retained')
    real_read = p.read_regular
    def public_only(path, *args, **kwargs):
        assert not Path(path).is_relative_to(root), 'upgrade read owner-private state'
        return real_read(path, *args, **kwargs)
    monkeypatch.setattr(p, 'read_regular', public_only)
    calls.clear()
    return previous, selected, new_directory, first, sentinel, calls, old_raw


def selected_new_version(state, version):
    old,new,directory,first,sentinel,calls,_=state
    new['adapter_version']=version
    (directory/'manifest.json').write_bytes(p.encoded(new))
    return ['upgrade',str(directory),'--previous-bundle-id',p.manifest_id(old),
        '--expected-bundle-id',p.manifest_id(new),'--apply']


@pytest.mark.parametrize('old_version', ['0.35.0','0.36.0'])
@pytest.mark.parametrize('new_version', ['0.37.0','0.37.1'])
def test_real_cli_string_path_crosses_closed_transition_and_stays_stopped(tmp_path,monkeypatch,capsys,old_version,new_version):
    state=installed_transition(tmp_path,monkeypatch,old_version)
    old,new,directory,first,sentinel,calls,old_raw=state
    args=selected_new_version(state,new_version)
    assert type(args[1]) is str
    assert manage.main(args)==0
    captured=capsys.readouterr()
    assert not captured.err
    result=json.loads(captured.out)
    assert result['status']=='upgraded_disabled'
    assert result['activated'] is False and result['execution_qualified'] is False
    receipt=p.decode(Path(result['receipt']).read_bytes())
    assert receipt['previous_bundle_id']==p.manifest_id(old)
    assert Path(first['receipt']).exists()
    assert (p.RELEASES/p.manifest_id(old)/'unit.service.in').read_bytes()==old_raw
    assert (p.RELEASES/p.manifest_id(new)/'unit.service.in').read_bytes()==(directory/'unit.service.in').read_bytes()
    assert sentinel.read_bytes()==b'old evidence retained'
    assert all(row[1] in ('show','daemon-reload') for row in calls)


@pytest.mark.parametrize('new_version', ['0.37.2','0.38.0'])
def test_real_cli_neighbour_version_does_not_gain_template_exception(tmp_path,monkeypatch,capsys,new_version):
    state=installed_transition(tmp_path,monkeypatch,'0.35.0')
    old,new,directory,first,sentinel,calls,_=state
    unit=p.UNIT_DIR/first['service'];expected=unit.read_bytes()
    assert manage.main(selected_new_version(state,new_version))==2
    captured=capsys.readouterr()
    assert not captured.err and json.loads(captured.out)=={'status':'refused'}
    assert unit.read_bytes()==expected and not calls
    assert not (p.RELEASES/p.manifest_id(new)).exists()
    assert sentinel.read_bytes()==b'old evidence retained'


def test_real_cli_extra_unit_edit_refuses_before_intent_or_publication(tmp_path,monkeypatch,capsys):
    state=installed_transition(tmp_path,monkeypatch,'0.36.0')
    old,new,directory,first,sentinel,calls,_=state
    unit=p.UNIT_DIR/first['service'];expected=unit.read_bytes()
    raw=(directory/'unit.service.in').read_bytes().replace(b'NoNewPrivileges=true',b'NoNewPrivileges=false')
    (directory/'unit.service.in').write_bytes(raw)
    new['files']['unit.service.in']=pin(raw)
    assert manage.main(selected_new_version(state,'0.37.1'))==2
    captured=capsys.readouterr()
    assert not captured.err and json.loads(captured.out)=={'status':'refused'}
    assert unit.read_bytes()==expected and not calls
    assert not (p.BASE/'installations'/(p.manifest_id(new)+'.json')).exists()
    assert not (p.RELEASES/p.manifest_id(new)).exists()
    assert sentinel.read_bytes()==b'old evidence retained'
