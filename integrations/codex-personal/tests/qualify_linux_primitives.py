"""Explicit Linux feasibility canaries; no backend, model, login or downloader.

Run only on an authorized non-root Linux host with the reviewed public CLI and
helper. Emits metadata only. Namespace PIDs are never used for host signals.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import select
import signal
import socket
import stat
import subprocess
import tempfile
import time
import uuid

CLI_SHA256 = '399ff69c431c066e40de318fba45c2b07f45a76b19939d247ee4828fc70c4215'
BWRAP_SHA256 = '77360cb751ccedc5971391444ac86a8a33c15b04d6b4a6fe45f5d25496e62c4c'
VERSION = 'codex-cli 0.163.0'
CHECKS = frozenset({'inside_read', 'inside_write', 'outside_read', 'outside_write',
    'network_loopback', 'network_unix_path', 'network_unix_abstract', 'fork', 'spawn',
    'session', 'process_group', 'nproc_raise', 'outside_process_hidden'})
CANARY = r'''
import errno,json,os,resource,socket,sys
p=json.loads(sys.argv[1]);checks={};errors={}
os.setsid()
resource.setrlimit(resource.RLIMIT_NPROC,(1,1))
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
resource.setrlimit(resource.RLIMIT_NOFILE,(64,64))
resource.setrlimit(resource.RLIMIT_CPU,(3,3))
def denied(name,fn,allowed):
    try:fn();checks[name]=False;errors[name]=0
    except OSError as e:checks[name]=e.errno in allowed;errors[name]=e.errno
def read(path):
    with open(path,'rb') as f:return f.read(64)
def write(path):
    with open(path,'xb') as f:f.write(b'synthetic')
def connect(family,address):
    with socket.socket(family,socket.SOCK_STREAM) as s:s.settimeout(.3);s.connect(address)
def fork():
    pid=os.fork()
    if pid==0:os._exit(0)
    os.waitpid(pid,0)
def spawn():
    pid=os.posix_spawn('/usr/bin/true',['/usr/bin/true'],{});os.waitpid(pid,0)
try:checks['inside_read']=read(p['inside'])==b'synthetic'
except OSError:checks['inside_read']=False
try:write(p['inside_write']);checks['inside_write']=True
except OSError:checks['inside_write']=False
fs={errno.EPERM,errno.EACCES,errno.ENOENT,errno.EROFS}
net={errno.EPERM,errno.EACCES,errno.ENETUNREACH,errno.ECONNREFUSED,errno.ENOENT}
denied('outside_read',lambda:read(p['outside']),fs)
denied('outside_write',lambda:write(p['outside_write']),fs)
denied('network_loopback',lambda:connect(socket.AF_INET,('127.0.0.1',p['port'])),net)
denied('network_unix_path',lambda:connect(socket.AF_UNIX,p['unix']),net)
denied('network_unix_abstract',lambda:connect(socket.AF_UNIX,p['abstract']),net)
denied('fork',fork,{errno.EAGAIN,errno.EPERM,errno.EACCES})
denied('spawn',spawn,{errno.EAGAIN,errno.EPERM,errno.EACCES})
denied('session',os.setsid,{errno.EPERM})
denied('process_group',lambda:os.setpgid(0,0),{errno.EPERM})
denied('outside_process_hidden',lambda:os.kill(p['outside_pid'],0),{errno.ESRCH,errno.EPERM})
try:resource.setrlimit(resource.RLIMIT_NPROC,(2,2));checks['nproc_raise']=False
except (ValueError,OSError):checks['nproc_raise']=True
print(json.dumps({'checks':checks,'errors':errors,'identity':{'pid':os.getpid(),'pgid':os.getpgrp(),'sid':os.getsid(0),'uid':os.getuid()}},sort_keys=True),flush=True)
raise SystemExit(0 if all(checks.values()) else 78)
'''
SLEEP_CANARY = r'''
import os,signal,time
os.setsid()
signal.signal(signal.SIGTERM,signal.SIG_IGN)
print('READY',flush=True)
time.sleep(5)
'''


def binary_hash(path):
    """Regular explicit executable only; reject all symlink path components."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('invalid executable path')
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ValueError('symlink executable path')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111
            or info.st_mode & 0o022 or info.st_uid not in (0, os.getuid())
            or info.st_size > 512 * 1024 * 1024):
        raise ValueError('invalid executable')
    hashed = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hashed.update(chunk)
    return hashed.hexdigest()


def policy(root, tools):
    # Root deny already excludes unrelated /tmp content. Explicit :slash_tmp
    # denial masks the bundled helper's re-exec path on this Linux build.
    filesystem = {':root': 'deny', ':minimal': 'read', str(root): 'write', str(tools): 'read'}
    entries = ','.join(json.dumps(k) + '=' + json.dumps(v) for k, v in filesystem.items())
    return '{filesystem={' + entries + '},network={enabled=false}}'


def command(codex, root, source, payload=None):
    argv = [str(codex), 'sandbox', '-P', 'board-linux-feasibility', '--include-managed-config',
        '-C', str(root), '-c', 'permissions.board-linux-feasibility=' + policy(root, codex.parent),
        '--', '/usr/bin/python3', '-I', '-S', '-c', source]
    return argv if payload is None else argv + [json.dumps(payload)]


def process_identity(pid):
    try:
        text = Path('/proc', str(pid), 'stat').read_text()
        tail = text[text.rindex(')') + 2:].split()
        return tail[19], tail[0]  # starttime and state; no command or environment retained
    except (OSError, ValueError, IndexError):
        return None


def owned_descendants(pid):
    """Observation only of this precisely owned launcher's synthetic subtree."""
    result = {}
    todo = [pid]
    while todo and len(result) < 32:
        parent = todo.pop()
        try:children = Path('/proc', str(parent), 'task', str(parent), 'children').read_text().split()
        except OSError:continue
        for value in children:
            child = int(value)
            current = process_identity(child)
            if current and child not in result:
                result[child] = current[0]
                todo.append(child)
    return result


def observe_launcher_cleanup(codex, root, env):
    """Kill only the owned Popen launcher, then observe its captured descendants.

    Namespace pid2 is data, never a host kill target. A five-second intrinsic
    sleep bounds the synthetic payload even if launcher cleanup is ineffective.
    This does not qualify durable supervisor recovery or arbitrary fork trees.
    """
    p = subprocess.Popen(command(codex, root, SLEEP_CANARY), cwd=root, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        close_fds=True, start_new_session=True)
    descendants = {}
    try:
        ready = select.select([p.stdout], [], [], 5)[0]
        line = p.stdout.readline(64) if ready else b''
        if line != b'READY\n':
            return {'ready': False, 'launcher_reaped': False, 'descendants_disappeared': False}
        launcher_before = process_identity(p.pid)
        descendants = owned_descendants(p.pid)
        p.kill()  # Popen owns this exact launcher; no namespace identity used.
        p.wait(timeout=2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            remaining = {pid: start for pid, start in descendants.items()
                if (current := process_identity(pid)) is not None and current[0] == start}
            if not remaining:break
            time.sleep(.02)
        return {'ready': True, 'launcher_reaped': p.poll() is not None,
            'captured_descendant_count': len(descendants),
            'launcher_observed_before_kill': launcher_before is not None,
            'descendants_disappeared': bool(descendants) and not remaining,
            'remaining_zombies_only': bool(remaining) and all(
                (current := process_identity(pid)) is None or current[1] == 'Z' for pid in remaining)}
    finally:
        if p.poll() is None:p.kill()
        p.wait(timeout=2)
        p.stdout.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', required=True)
    args = parser.parse_args(argv)
    if platform.system() != 'Linux' or os.getuid() == 0:
        raise SystemExit('This probe requires an authorized non-root Linux host')
    codex = Path(args.codex)
    helper = codex.parent / 'codex-resources' / 'bwrap'
    if binary_hash(codex) != CLI_SHA256 or binary_hash(helper) != BWRAP_SHA256:
        raise SystemExit('Pinned public executable hash mismatch')
    env = {k: os.environ[k] for k in ('HOME', 'CODEX_HOME', 'TMPDIR') if k in os.environ}
    env.update({'PATH': '/usr/bin:/bin', 'LANG': 'C'})
    report = {'schema': 1, 'check': 'linux_feasibility_canaries', 'backend_execution_qualified': False,
        'model_calls': 0, 'auth_rpc_calls_requested': 0, 'controller_auth_files_read': False,
        'production_enabled': False, 'system_configuration_changed': False,
        'cli_version': VERSION, 'cli_sha256': CLI_SHA256, 'bwrap_sha256': BWRAP_SHA256,
        'interpreter_sha256': binary_hash('/usr/bin/python3.10'),
        'host': {'system': platform.system(), 'release': platform.release(), 'machine': platform.machine()},
        'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest() if '__file__' in globals()
            and Path(__file__).is_file() else None}
    with tempfile.TemporaryDirectory(prefix='agent-board-linux-feasibility-') as folder:
        base = Path(folder);base.chmod(0o700)
        root = base / 'checkout';root.mkdir(mode=0o700)
        inside = root / 'inside';inside.write_bytes(b'synthetic')
        outside = base / 'outside';outside.write_bytes(b'synthetic')
        sentinel = subprocess.Popen(['/usr/bin/python3', '-I', '-S', '-c', 'import time;time.sleep(10)'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp, socket.socket(
                    socket.AF_UNIX, socket.SOCK_STREAM) as unix, socket.socket(
                    socket.AF_UNIX, socket.SOCK_STREAM) as abstract:
                tcp.bind(('127.0.0.1', 0));tcp.listen(1)
                unix.bind(str(base / 'socket'));unix.listen(1)
                name = '\0agent-board-' + uuid.uuid4().hex;abstract.bind(name);abstract.listen(1)
                version = subprocess.run([str(codex), '--version'], env=env, stdin=subprocess.DEVNULL,
                    capture_output=True, timeout=5)
                if version.returncode != 0 or version.stdout.decode().strip() != VERSION:
                    raise SystemExit('Pinned CLI version mismatch')
                payload = {'inside': str(inside), 'inside_write': str(root / 'written'),
                    'outside': str(outside), 'outside_write': str(base / 'written'),
                    'port': tcp.getsockname()[1], 'unix': str(base / 'socket'),
                    'abstract': name, 'outside_pid': sentinel.pid}
                p = subprocess.run(command(codex, root, CANARY, payload), cwd=root, env=env,
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                    close_fds=True, start_new_session=True)
                value = json.loads(p.stdout)
                checks = value['checks']
                if (set(checks) != CHECKS or any(type(v) is not bool for v in checks.values())
                        or p.returncode not in (0, 78)):
                    raise SystemExit('Invalid synthetic guard response')
                report['checks'] = checks
                report['denial_errnos'] = value['errors']
                report['namespace_identity'] = value['identity']
                report['outside_fixture_unchanged'] = outside.read_bytes() == b'synthetic' and not (base / 'written').exists()
                report['launcher_cleanup_observation'] = observe_launcher_cleanup(codex, root, env)
        finally:
            sentinel.kill();sentinel.wait(timeout=2)
    report['synthetic_fixture_removed'] = not base.exists()
    report['result'] = 'primitives_observed_backend_still_refused'
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if all(report['checks'].values()) and report['outside_fixture_unchanged'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
