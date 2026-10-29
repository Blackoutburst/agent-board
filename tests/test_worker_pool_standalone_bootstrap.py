"""Unprivileged bootstrap canaries; no root, service, auth or private state."""
import importlib.util
import os
from pathlib import Path
import py_compile
import struct
import subprocess
import sys
import pytest

@pytest.fixture
def addon(tmp_path):
    root=tmp_path/'public-bundle';root.mkdir(mode=0o700)
    for source,dest in [('manage.py','pool_manage.py'),('package.py','pool_package.py')]:
        path=root/dest;path.write_bytes(Path('deploy/worker_pool',source).read_bytes());path.chmod(0o600)
    marker=root/'untrusted-code-ran'
    canary=('from pathlib import Path\nPath('+repr(str(marker))+').write_text("bad")\nraise SystemExit(93)\n')
    def run(*flags,isolated=True):
        return subprocess.run([sys.executable,*(['-I'] if isolated else []),'-B',str(root/'pool_manage.py'),*flags],
            cwd=root,capture_output=True,timeout=10)
    return root,marker,canary,run

def test_bundle_stdlib_shadow_cannot_execute_before_help(addon):
    root,marker,canary,run=addon
    (root/'json.py').write_text(canary)
    result=run('--help')
    assert result.returncode==0 and b'usage:' in result.stdout and not result.stderr
    assert not marker.exists()

def test_timestamp_valid_poisoned_package_cache_is_never_loaded(addon):
    root,marker,canary,run=addon;source=root/'pool_package.py';info=source.stat()
    evil=root/'evil.py';evil.write_text(canary)
    cache=Path(importlib.util.cache_from_source(str(source)));cache.parent.mkdir(mode=0o700)
    py_compile.compile(str(evil),cfile=str(cache),doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    raw=cache.read_bytes();cache.write_bytes(raw[:8]+struct.pack('<II',int(info.st_mtime)&0xffffffff,info.st_size&0xffffffff)+raw[16:])
    result=run('--help')
    assert result.returncode==0 and b'usage:' in result.stdout and not result.stderr
    assert not marker.exists()

def test_nonisolated_command_refuses_before_shadowed_argparse(addon):
    root,marker,canary,run=addon;(root/'argparse.py').write_text(canary)
    result=run('--help',isolated=False)
    assert result.returncode==2 and result.stdout==b'{"status":"refused"}\n' and not result.stderr
    assert not marker.exists()

@pytest.mark.parametrize('fault',['symlink','hardlink','group_writable'])
def test_package_alias_or_writable_source_refuses_before_execution(addon,fault):
    root,marker,canary,run=addon;source=root/'pool_package.py'
    if fault=='symlink':
        held=root/'held.py';source.rename(held);source.symlink_to(held)
    elif fault=='hardlink':os.link(source,root/'alias.py')
    else:source.chmod(0o620)
    result=run('--help')
    assert result.returncode==2 and result.stdout==b'{"status":"refused"}\n' and not result.stderr
    assert not marker.exists()
