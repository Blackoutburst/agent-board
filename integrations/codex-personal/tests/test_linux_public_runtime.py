"""Real held empty-file reads with synthetic trusted ancestor permissions."""
import os
import stat
from types import SimpleNamespace

import pytest

from blackoutburst_agents.linux_test_namespace import ProbeError, read_public


@pytest.fixture
def empty_public(tmp_path, monkeypatch):
    path=tmp_path/'__init__.py';path.write_bytes(b'');path.chmod(0o600)
    fstat,stat_path=os.fstat,os.stat
    def trusted_ancestor(info):
        if not stat.S_ISDIR(info.st_mode):return info
        values={name:getattr(info,name) for name in dir(info) if name.startswith('st_')}
        values['st_mode'] &= ~0o022
        return SimpleNamespace(**values)
    monkeypatch.setattr(os,'fstat',lambda fd:trusted_ancestor(fstat(fd)))
    monkeypatch.setattr(os,'stat',lambda *args,**kwargs:trusted_ancestor(stat_path(*args,**kwargs)))
    return path


def test_empty_runtime_file_has_exact_held_hash_and_zero_size(empty_public):
    raw,pin=read_public(empty_public,allow_empty=True)
    assert raw==b'' and pin['size']==0
    assert pin['sha256']=='e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    assert read_public(empty_public,allow_empty=True,expected=pin)==(raw,pin)


def test_empty_executable_still_refuses_without_runtime_opt_in(empty_public):
    with pytest.raises(ProbeError):read_public(empty_public)


def test_runtime_empty_opt_in_does_not_skip_content_revalidation(empty_public):
    _,pin=read_public(empty_public,allow_empty=True)
    empty_public.write_bytes(b'changed')
    with pytest.raises(ProbeError):read_public(empty_public,allow_empty=True,expected=pin)


@pytest.mark.parametrize('kind',['symlink','hardlink','public_write','non_boolean'])
def test_empty_runtime_files_keep_physical_and_type_guards(empty_public,kind):
    path=empty_public
    if kind=='symlink':
        other=path.with_suffix('.actual');path.rename(other);path.symlink_to(other)
    elif kind=='hardlink':os.link(path,path.with_suffix('.alias'))
    elif kind=='public_write':path.chmod(0o622)
    with pytest.raises((ProbeError,OSError)):read_public(path,allow_empty=1 if kind=='non_boolean' else True)
