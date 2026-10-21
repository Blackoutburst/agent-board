"""Execute only Linux canary Python with temporary files and a fake network."""
import builtins
import errno
import json
import sys

import pytest

from blackoutburst_agents import linux_planner_policy as policy


def invoke(tmp_path, monkeypatch, capsys, *, changes=None, inside=b'synthetic planner canary'):
    payload={key:str(tmp_path/key) for key in ('inside','inside_write','outside','outside_write')}
    payload['port']=31415
    for name in ('inside','outside'):
        (tmp_path/name).write_bytes(inside if name=='inside' else b'synthetic planner canary')
    errors={'inside_write_denied':errno.EROFS,'outside_read_denied':errno.ENOENT,
            'outside_write_denied':errno.ENOENT,'network_loopback_denied':errno.EPERM}
    errors.update(changes or {})
    names={payload[key]:check for key,check in (
        ('inside','inside_read'),('inside_write','inside_write_denied'),
        ('outside','outside_read_denied'),('outside_write','outside_write_denied'))}
    real_open=builtins.open
    def access(path,*args,**kwargs):
        error=errors.get(names[str(path)])
        if error is not None:raise OSError(error,'synthetic denial')
        return real_open(path,*args,**kwargs)
    class Network:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def settimeout(self,value):pass
        def connect(self,address):
            error=errors['network_loopback_denied']
            if error is not None:raise OSError(error,'synthetic denial')
    with monkeypatch.context() as patch:
        patch.setattr(builtins,'open',access)
        patch.setattr(policy.socket,'socket',lambda *args:Network())
        patch.setattr(sys,'argv',['synthetic-canary',json.dumps(payload)])
        with pytest.raises(SystemExit) as stopped:
            exec(compile(policy.LINUX_CANARY,'<synthetic-linux-canary>','exec'),{})
    return stopped.value.code,json.loads(capsys.readouterr().out)['checks']


@pytest.mark.parametrize('changes',[
    {},
    {name:errno.EACCES for name in ('inside_write_denied','outside_read_denied','outside_write_denied','network_loopback_denied')},
    {name:errno.EPERM for name in ('inside_write_denied','outside_read_denied','outside_write_denied','network_loopback_denied')},
])
def test_only_closed_linux_denial_combinations_pass(tmp_path,monkeypatch,capsys,changes):
    code,checks=invoke(tmp_path,monkeypatch,capsys,changes=changes)
    assert code==0 and checks=={name:True for name in policy.CHECKS}
    assert (tmp_path/'inside').read_bytes()==b'synthetic planner canary'
    assert (tmp_path/'outside').read_bytes()==b'synthetic planner canary'
    assert not (tmp_path/'inside_write').exists() and not (tmp_path/'outside_write').exists()


@pytest.mark.parametrize('check,error',[
    ('inside_read',errno.ENOENT),('inside_read',errno.EACCES),('inside_read',errno.EROFS),
    ('inside_write_denied',errno.ENOENT),('inside_write_denied',errno.EEXIST),('inside_write_denied',errno.EIO),
    ('outside_read_denied',errno.EROFS),('outside_read_denied',errno.EIO),
    ('outside_write_denied',errno.EROFS),('outside_write_denied',errno.EEXIST),('outside_write_denied',errno.EIO),
    ('network_loopback_denied',errno.ENOENT),('network_loopback_denied',errno.EROFS),
    ('network_loopback_denied',errno.ECONNREFUSED),('network_loopback_denied',errno.ETIMEDOUT),
])
def test_wrong_errno_never_counts_as_denial(tmp_path,monkeypatch,capsys,check,error):
    code,checks=invoke(tmp_path,monkeypatch,capsys,changes={check:error})
    assert code==78 and checks[check] is False


@pytest.mark.parametrize('check,path',[
    ('inside_write_denied','inside_write'),('outside_read_denied','outside'),
    ('outside_write_denied','outside_write'),('network_loopback_denied',None),
])
def test_successful_access_is_refused_including_real_file_writes(tmp_path,monkeypatch,capsys,check,path):
    code,checks=invoke(tmp_path,monkeypatch,capsys,changes={check:None})
    assert code==78 and checks[check] is False
    if path and path.endswith('write'):
        assert (tmp_path/path).read_bytes()==b'synthetic planner canary'


def test_wrong_inside_bytes_do_not_qualify(tmp_path,monkeypatch,capsys):
    code,checks=invoke(tmp_path,monkeypatch,capsys,inside=b'foreign synthetic bytes')
    assert code==78 and checks['inside_read'] is False
