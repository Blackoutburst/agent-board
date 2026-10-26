"""The sandbox setup dependency permits one exact stopped unit transition."""
import hashlib
from pathlib import Path

import pytest

from deploy.worker_pool import manage


OLD_LINE = b'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n'
NEW_LINE = b'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK\n'
OLD_SHA = '8db5a568a5ec5df5f03434dfa1e1eab3d651546a59d3b5133b70a784d6f6d446'


def pin(raw):
    return {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def templates():
    new = Path('deploy/worker_pool/unit.service.in').read_bytes()
    assert new.count(NEW_LINE) == 1
    old = new.replace(NEW_LINE, OLD_LINE)
    assert hashlib.sha256(old).hexdigest() == OLD_SHA
    return old, new


def candidate(tmp_path, old_version='0.35.0', new_version='0.37.0', old=None, new=None):
    shipped_old, shipped_new = templates()
    old = shipped_old if old is None else old
    new = shipped_new if new is None else new
    previous = tmp_path / 'previous'
    destination = tmp_path / 'destination'
    previous.mkdir(); destination.mkdir()
    (previous / 'unit.service.in').write_bytes(old)
    (destination / 'unit.service.in').write_bytes(new)
    return ({'adapter_version': old_version, 'files': {'unit.service.in': pin(old)}},
            {'adapter_version': new_version, 'files': {'unit.service.in': pin(new)}},
            previous, destination)


@pytest.mark.parametrize('version', ['0.35.0', '0.36.0'])
def test_exact_setup_dependency_upgrade_allowed(tmp_path, version):
    assert manage._unit_template_upgrade_allowed(*candidate(tmp_path, old_version=version))


@pytest.mark.parametrize('old_version,new_version', [
    ('0.34.0', '0.37.0'), ('0.35.1', '0.37.0'), ('0.36.1', '0.37.0'),
    ('0.37.0', '0.37.0'), ('0.36.0', '0.37.1'), ('0.36.0', '0.38.0'),
])
def test_changed_template_cannot_use_other_version_transitions(tmp_path, old_version, new_version):
    assert not manage._unit_template_upgrade_allowed(*candidate(tmp_path, old_version, new_version))


@pytest.mark.parametrize('change', [
    lambda raw: raw + b'# extra\n',
    lambda raw: raw.replace(b'AF_NETLINK\n', b'AF_NETLINK AF_PACKET\n'),
    lambda raw: raw.replace(b'AF_NETLINK\n', b'AF_NETLINK \n'),
    lambda raw: raw.replace(b'AF_NETLINK\n', b'\n'),
    lambda raw: raw.replace(b'AF_UNIX ', b''),
    lambda raw: raw.replace(b'NoNewPrivileges=true', b'NoNewPrivileges=false'),
    lambda raw: raw.replace(b'CapabilityBoundingSet=\n', b'CapabilityBoundingSet=CAP_NET_ADMIN\n'),
    lambda raw: raw.replace(b'CPUQuota=@CPU@%', b'CPUQuota=800%'),
    lambda raw: raw.replace(b'ProtectSystem=strict', b'ProtectSystem=false'),
])
def test_netlink_exception_cannot_conceal_other_unit_changes(tmp_path, change):
    _, new = templates()
    assert not manage._unit_template_upgrade_allowed(*candidate(tmp_path, new=change(new)))


def test_modified_predecessor_template_refuses_even_if_delta_is_exact(tmp_path):
    old, new = templates()
    assert not manage._unit_template_upgrade_allowed(*candidate(tmp_path,
        old=old+b'# locally changed\n', new=new+b'# locally changed\n'))


@pytest.mark.parametrize('which,field', [('old','size'),('old','sha256'),('new','size'),('new','sha256')])
def test_reloaded_bytes_must_match_the_selected_manifest(tmp_path, which, field):
    args=candidate(tmp_path)
    selected=args[0 if which=='old' else 1]['files']['unit.service.in']
    selected[field]=selected[field]+1 if field=='size' else '0'*64
    assert not manage._unit_template_upgrade_allowed(*args)


def test_temporarily_valid_template_does_not_authorize_different_manifest(tmp_path):
    _, new=templates(); malicious=new.replace(b'ProtectSystem=strict',b'ProtectSystem=false')
    args=candidate(tmp_path,new=malicious)
    # The helper sees a valid replacement after an earlier bundle read selected
    # a different manifest. It must compare its own held bytes with that pin.
    (args[3]/'unit.service.in').write_bytes(new)
    assert not manage._unit_template_upgrade_allowed(*args)


def test_unchanged_template_keeps_existing_upgrade_path(tmp_path):
    old,_=templates()
    assert manage._unit_template_upgrade_allowed(*candidate(tmp_path,
        old_version='0.34.0',new_version='0.38.0',old=old,new=old))
