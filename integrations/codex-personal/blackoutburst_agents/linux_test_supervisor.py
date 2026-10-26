"""Owned Linux named-test supervisor; only explicit, pinned calls start a job.

The helper has no Board/model client. Source is released only after a fresh
parent boundary; a native observation is never a broker snapshot admission.
"""
from __future__ import annotations

import array
import fcntl
import os
from pathlib import Path
import select
import signal
import socket
import stat
import subprocess
import time
import uuid

from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, canonical, digest
from .dev_test_supervisor import _ControlPipe, _control_clock
from .linux_test_namespace import (MAX_INPUT_PACKET, MAX_PACKET, ProbeError,
    inner, outer, read_sealed, require, sealed_memfd)

MAX_JOB_BYTES = 1024 * 1024
CLEANUP_SECONDS = 2
SOURCE_WINDOW_MARKER = 'source-window.ready'
BOOTSTRAP = r'''
import hashlib,importlib.abc,importlib.util,json,os,stat,sys
root,pins,phase,target=sys.argv[1:]
pins=json.loads(pins);sources={};total=0;nodes=[]
if phase not in ('supervise','outer','inner'):raise SystemExit(78)
inside=phase=='inner'
if inside and (os.getpid()!=1 or os.geteuid()!=0 or os.getegid()!=0):raise SystemExit(78)
if not isinstance(pins,dict) or set(pins)!={'package','files'}:raise SystemExit(78)
package=pins['package'];entries=pins['files']
if not isinstance(package,dict) or set(package)!={'path','identity','chain'} or package['path']!=root:raise SystemExit(78)
if not isinstance(root,str) or not root.startswith('/') or os.path.normpath(root)!=root or len(root)>4096:raise SystemExit(78)
parts=root.split('/')[1:]
if not 1<=len(parts)<=255 or any(x in ('','.','..') for x in parts):raise SystemExit(78)
chain=package['chain']
if not isinstance(chain,list) or len(chain)!=len(parts)+1:raise SystemExit(78)
if not isinstance(entries,dict) or not 1<=len(entries)<=128:raise SystemExit(78)
def verify():
 for i,(fd,name,before) in enumerate(nodes):
  current=os.fstat(fd);linked=os.stat('/' if i==0 else name,follow_symlinks=False,**({} if i==0 else {'dir_fd':nodes[i-1][0]}))
  keys=('st_dev','st_ino','st_uid','st_gid','st_mode')
  if any(getattr(before,k)!=getattr(current,k) or getattr(before,k)!=getattr(linked,k) for k in keys):raise SystemExit(78)
try:
 current=''
 for i,name in enumerate([None]+parts):
  fd=os.open('/' if i==0 else name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,**({} if i==0 else {'dir_fd':nodes[-1][0]}))
  info=os.fstat(fd);nodes.append((fd,name,info));current='/' if i==0 else current.rstrip('/')+'/'+name
  pin=chain[i]
  if not isinstance(pin,dict) or set(pin)!={'path','identity','uid','mode'} or pin['path']!=current or pin['identity']!=[info.st_dev,info.st_ino] or pin['mode']!=info.st_mode or (not inside and pin['uid']!=info.st_uid):raise SystemExit(78)
 if package['identity']!=[nodes[-1][2].st_dev,nodes[-1][2].st_ino]:raise SystemExit(78)
 verify()
 for name,expected in entries.items():
  if not isinstance(name,str) or '/' in name or not name.endswith('.py') or name in ('.py','..py'):raise SystemExit(78)
  if not isinstance(expected,dict) or set(expected)!={'sha256','bytes'} or type(expected['bytes']) is not int or not 0<expected['bytes']<=262144:raise SystemExit(78)
  path=os.path.join(root,name)
  verify();fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=nodes[-1][0])
  try:
   before=os.fstat(fd)
   if not stat.S_ISREG(before.st_mode) or (not inside and before.st_uid not in (0,os.geteuid())) or before.st_nlink!=1 or before.st_mode&0o022 or before.st_size!=expected['bytes']:raise SystemExit(78)
   data=bytearray()
   while len(data)<=262144:
    piece=os.read(fd,min(65536,262145-len(data)))
    if not piece:break
    data.extend(piece)
   after=os.fstat(fd);linked=os.stat(name,dir_fd=nodes[-1][0],follow_symlinks=False)
   keys=('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
   verify()
   if len(data)!=expected['bytes'] or any(getattr(before,k)!=getattr(after,k) or getattr(before,k)!=getattr(linked,k) for k in keys) or hashlib.sha256(data).hexdigest()!=expected['sha256']:raise SystemExit(78)
   total+=len(data)
   if total>4194304:raise SystemExit(78)
   sources[name]=(path,bytes(data))
  finally:os.close(fd)
 verify()
finally:
 for fd,_,_ in reversed(nodes):os.close(fd)
class Held(importlib.abc.MetaPathFinder,importlib.abc.Loader):
 def find_spec(self,fullname,path=None,target=None):
  if fullname=='blackoutburst_agents':name='__init__.py'
  elif fullname.startswith('blackoutburst_agents.') and fullname.count('.')==1:name=fullname.split('.')[1]+'.py'
  else:return None
  if name not in sources:raise ImportError('unpinned_module')
  return importlib.util.spec_from_loader(fullname,self,is_package=name=='__init__.py')
 def create_module(self,spec):return None
 def exec_module(self,module):
  name='__init__.py' if module.__name__=='blackoutburst_agents' else module.__name__.split('.')[1]+'.py'
  path,data=sources[name];module.__file__=path
  if name=='__init__.py':module.__path__=[]
  exec(compile(data,path,'exec',dont_inherit=True),module.__dict__)
sys.meta_path.insert(0,Held())
from blackoutburst_agents.linux_test_supervisor import helper_entry
raise SystemExit(helper_entry(phase,target))
'''


def helper_command(plan, phase, target):
    return [plan['interpreter'], '-I', '-B', '-S', '-c', BOOTSTRAP,
            plan['sources']['package']['path'], canonical(plan['sources']).decode(), phase, str(target)]


def _read_proc(path, maximum=16384):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        raw = os.read(descriptor, maximum + 1)
        require(len(raw) <= maximum, 'linux_namespace_identity_invalid')
        return raw.decode('ascii', 'strict')
    finally:
        os.close(descriptor)


def receive_identity(channel, nonce, launcher):
    data, ancillary, flags, _ = channel.recvmsg(MAX_PACKET, socket.CMSG_SPACE(array.array('i').itemsize))
    descriptors = []
    try:
        for level, kind, raw in ancillary:
            require(level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS)
            values = array.array('i'); values.frombytes(raw); descriptors.extend(values)
        message = decode(data)
        require(not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) and len(descriptors) == 1
                and type(message) is dict and set(message) == {'stage', 'nonce', 'namespace_identity'}
                and message['stage'] == 'identity' and message['nonce'] == nonce)
        descriptor = descriptors[0]
        os.set_inheritable(descriptor, False)
        require(not select.select([descriptor], [], [], 0)[0])
        own_pid = os.getpid()
        fields = dict(line.split(':', 1) for line in _read_proc('/proc/' + str(own_pid) + '/fdinfo/' + str(descriptor)).splitlines() if ':' in line)
        pid = int(fields['Pid']); numbers = [int(item) for item in fields['NSpid'].split()]
        require(pid > 1 and numbers == [pid, 1])
        status = dict(line.split(':', 1) for line in _read_proc('/proc/' + str(pid) + '/status').splitlines() if ':' in line)
        namespace = os.stat('/proc/' + str(pid) + '/ns/pid')
        own_namespace = os.stat('/proc/' + str(own_pid) + '/ns/pid')
        require(int(status['PPid']) == launcher.pid
                and message['namespace_identity'] == [namespace.st_dev, namespace.st_ino]
                and (namespace.st_dev, namespace.st_ino) != (own_namespace.st_dev, own_namespace.st_ino)
                and not select.select([descriptor], [], [], 0)[0], 'linux_namespace_identity_invalid')
        descriptors.pop()
        return descriptor, {'namespace_identity': message['namespace_identity'],
                            'acquired_by': 'SCM_RIGHTS_validated', 'numeric_pid_signalled': False}
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def cleanup_owned(launcher, launcher_fd, init_fd):
    """Only possessed kernel handles/unreaped Popen child, never journal PIDs."""
    deadline = _control_clock() + CLEANUP_SECONDS
    okay = init_fd is not None and launcher_fd is not None
    for descriptor in (init_fd, launcher_fd):
        if descriptor is not None and not select.select([descriptor], [], [], 0)[0]:
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                okay = False
    if launcher.poll() is None and launcher_fd is None:
        try:
            launcher.kill()  # Only our exact unreaped host child, no PID adoption.
        except OSError:
            okay = False
    terminal = init_fd is not None and bool(select.select([init_fd], [], [], max(0, deadline-_control_clock()))[0])
    try:
        launcher.wait(timeout=max(0.001, deadline-_control_clock()))
        reaped = True
    except (subprocess.TimeoutExpired, OSError):
        reaped = False
    return {'init_identity_received': init_fd is not None, 'init_terminal': terminal,
            'launcher_reaped': reaped, 'cleanup_confirmed': bool(okay and terminal and reaped),
            'numeric_pid_signalled': False}


def _await(channel, control, deadline):
    while _control_clock() < deadline:
        require(control.authorized(), 'linux_authorization_expired')
        if select.select([channel], [], [], 0.02)[0]:
            return
    raise DevelopmentError('linux_startup_expired')


def _source_authorized(control,window):
    """A later heartbeat cannot extend the age of the captured SOURCE frame."""
    require(control.authorized(),'linux_authorization_expired')
    now=_control_clock()
    require(window['validated_at'] <= now < window['deadline'],'linux_authorization_expired')


def source_window_ready(private):
    """Only a missing marker at open is pending; present evidence is strict.

    The parent creates this empty marker after the complete window and directory
    have been fsynced. Its existence carries no authorization: the window and
    actual fresh control frame are still compared at the SOURCE boundary.
    """
    private.verify()
    descriptor = None
    try:
        try:
            descriptor = os.open(SOURCE_WINDOW_MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=private.fd)
        except FileNotFoundError:
            return False
        private.validate_file(descriptor)
        before = os.fstat(descriptor)
        require(before.st_size == 0 and os.read(descriptor, 1) == b'')
        after = os.fstat(descriptor)
        linked = os.stat(SOURCE_WINDOW_MARKER, dir_fd=private.fd, follow_symlinks=False)
        fields = ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        require(all(getattr(before, key) == getattr(after, key) == getattr(linked, key) for key in fields))
        private.verify()
        return True
    except OSError:
        raise DevelopmentError('linux_source_window_invalid') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def supervise(directory):
    from .protected_public_pins import disable_protected_public_pins_for_process
    disable_protected_public_pins_for_process()
    from .linux_test_backend import load_supervisor_job, validate_native_receipt
    private, job, plan, state = load_supervisor_job(directory)
    lock = None; launcher = launcher_fd = init_fd = None
    parent = child = None; descriptors = []
    source_released = False; release_intent = False; observed_exit = None; launcher_attempted = False
    output_bytes = 0
    import hashlib
    output = hashlib.sha256()
    checks, identity = {}, None
    reason = 'linux_native_unconfirmed'
    clean = {'init_identity_received': False, 'init_terminal': False, 'launcher_reaped': False,
             'cleanup_confirmed': False, 'numeric_pid_signalled': False}
    # A second helper must never publish non-launch/cleanup metadata over an
    # existing producer. Failure to acquire this exclusive producer identity
    # leaves every existing state/receipt untouched, even if its outcome is lost.
    try:
        lock = os.open('supervisor.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                       0o600, dir_fd=private.fd)
        private.validate_file(lock)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        if lock is not None:os.close(lock)
        private.close()
        return 78
    try:
        control = _ControlPipe(nonce=job['nonce'], initial=job['control'])
        require(control.wait_initial(), 'linux_authorization_expired')
        started = _control_clock()
        from .linux_test_namespace import limits
        limits(min(61, job['timeout_seconds'] + 1), file_bytes=MAX_INPUT_PACKET)
        script = private.read('script.py', 65536)
        inputs = private.read('input.packet', MAX_INPUT_PACKET)
        require(digest(script) == job['script_sha256'] and digest(inputs) == job['request']['input_packet_sha256'])
        script_fd = sealed_memfd('linux-approved-test', script)
        inputs_fd = sealed_memfd('linux-test-inputs', inputs)
        descriptors += [script_fd, inputs_fd]
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        packet = {'nonce': job['nonce'], 'supervisor_pid': os.getpid(),
            'control_fd': child.fileno(), 'root': str(private.path / 'root'),
            'runtime': plan['runtime'], 'script_fd': script_fd, 'inputs_fd': inputs_fd,
            'cpu_seconds': min(61, job['timeout_seconds'] + 1),
            'script_sha256': job['script_sha256'],
            'input_packet_sha256': job['request']['input_packet_sha256'],
            'input_snapshot_sha256': job['request']['input_snapshot_sha256']}
        raw = canonical({'packet': packet, 'plan': plan})
        packet_fd = sealed_memfd('linux-namespace-packet', raw); descriptors.append(packet_fd)
        token = canonical({'fd': packet_fd, 'sha256': digest(raw)}).decode()
        command = helper_command(plan, 'inner', token)
        command = [plan['unshare'], '--user', '--map-root-user', '--mount', '--pid', '--fork',
            '--kill-child=SIGKILL', '--net', '--ipc', '--uts', '--propagation', 'private', '--', *command]
        outer_raw = canonical({'packet': packet, 'command': command})
        outer_fd = sealed_memfd('linux-launcher-packet', outer_raw); descriptors.append(outer_fd)
        outer_token = canonical({'fd': outer_fd, 'sha256': digest(outer_raw)}).decode()
        state['phase'] = 'namespace_launch_intent'
        private.replace('state.json', canonical(state))  # Ambiguity precedes Popen.
        require(control.authorized(), 'linux_authorization_expired')
        launcher_attempted = True  # An exception from Popen is still ambiguous.
        launcher = subprocess.Popen(helper_command(plan, 'outer', outer_token),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, pass_fds=tuple(descriptors + [child.fileno()]),
            env={'LANG': 'C.UTF-8', 'PATH': '/usr/bin:/bin'}, start_new_session=True)
        child.close(); child = None
        launcher_fd = os.pidfd_open(launcher.pid)
        startup_deadline = started + 5
        _await(parent, control, startup_deadline)
        init_fd, identity = receive_identity(parent, job['nonce'], launcher)
        require(control.authorized(), 'linux_authorization_expired')
        parent.sendall(b'I')
        _await(parent, control, startup_deadline)
        ready = decode(parent.recv(MAX_PACKET))
        require(type(ready) is dict and set(ready) == {'stage', 'nonce', 'runtime_sha256',
            'script_sha256', 'input_packet_sha256', 'checks'}
            and ready['stage'] == 'ready' and ready['nonce'] == job['nonce']
            and ready['runtime_sha256'] == digest(canonical(plan['runtime']))
            and ready['script_sha256'] == job['script_sha256']
            and ready['input_packet_sha256'] == job['request']['input_packet_sha256'])
        from .linux_test_namespace import REQUIRED_CHECKS
        checks = ready['checks']
        require(type(checks) is dict and set(checks) == REQUIRED_CHECKS | {'inputs_write'}
                and all(value is True for value in checks.values()), 'linux_kernel_guard_failed')
        # Request a real parent capture; receipt of READY is never authorization.
        challenge = uuid.uuid4().hex
        state.update(phase='source_boundary_requested', boundary_sequence=control.last_window['sequence'],
                     source_challenge=challenge)
        private.replace('state.json', canonical(state))
        boundary_sequence = control.last_window['sequence']
        approved = False
        while _control_clock() < startup_deadline:
            require(control.authorized(), 'linux_authorization_expired')
            if not source_window_ready(private):
                time.sleep(0.01); continue
            authorization = decode(private.read('source-window.json', 2048))
            require(type(authorization) is dict and set(authorization) ==
                    {'nonce', 'challenge', 'request_sha256', 'window_sha256'}
                    and authorization['nonce'] == job['nonce'] and authorization['challenge'] == challenge
                    and authorization['request_sha256'] == digest(canonical(job['request'])))
            if (control.last_window['sequence'] > boundary_sequence
                    and authorization['window_sha256'] == digest(canonical(control.last_window))):
                approved = True; break
            time.sleep(0.01)
        require(approved, 'linux_source_boundary_missing')
        source_window=decode(canonical(control.last_window))
        release_intent = True
        private.create('source-admission.json', canonical({'nonce': job['nonce'],
            'request_sha256': digest(canonical(job['request'])), 'identity': identity,
            'control': source_window, 'phase': 'source_release_intent'}))
        state['phase'] = 'source_release_intent'; private.replace('state.json', canonical(state))
        _source_authorized(control,source_window)
        parent.sendall(canonical({'schema':'linux-source-release-v1','nonce':job['nonce'],
                                 'control':source_window})); source_released = True
        state['phase'] = 'source_ack_written'; private.replace('state.json', canonical(state))
        parent.close(); parent = None
        deadline = min(_control_clock() + job['timeout_seconds'], started + job['timeout_seconds'] + 5)
        streams = {launcher.stdout.fileno(), launcher.stderr.fileno()}
        for descriptor in streams:
            os.set_blocking(descriptor, False)
        while streams or launcher.poll() is None:
            require(control.authorized(), 'linux_authorization_expired')
            require(_control_clock() < deadline, 'linux_script_wall_expired')
            for descriptor in select.select(list(streams), [], [], 0.02)[0]:
                data = os.read(descriptor, min(65536, job['max_output_bytes'] + 1 - output_bytes))
                if not data:
                    streams.remove(descriptor); continue
                remaining = job['max_output_bytes'] - output_bytes
                output.update(data[:remaining]); output_bytes += min(len(data), remaining)
                require(len(data) <= remaining, 'linux_output_limit')
        observed_exit = launcher.returncode
        reason = 'completed' if observed_exit == 0 else 'script_failed'
    except BaseException as error:
        allowed = {'linux_authorization_expired', 'linux_startup_expired', 'linux_source_boundary_missing',
            'linux_kernel_guard_failed', 'linux_script_wall_expired', 'linux_output_limit'}
        value = getattr(error, 'code', None)
        reason = value if type(value) is str and value in allowed else 'linux_native_unconfirmed'
    finally:
        if launcher is not None:
            clean = cleanup_owned(launcher, launcher_fd, init_fd)
            launcher.stdout.close(); launcher.stderr.close()
        elif not launcher_attempted:
            clean = {'init_identity_received': False, 'init_terminal': False,
                     'launcher_reaped': False, 'cleanup_confirmed': True, 'numeric_pid_signalled': False}
        for channel in (parent, child):
            if channel is not None:
                channel.close()
        for descriptor in [*descriptors, launcher_fd, init_fd]:
            if descriptor is not None:
                os.close(descriptor)
        if reason == 'completed' and not clean['cleanup_confirmed']:
            reason = 'linux_native_unconfirmed'
        native = {'schema': 'blackoutburst-linux-native-observation-v1', 'nonce': job['nonce'],
            'request_sha256': digest(canonical(job['request'])), 'plan_sha256': digest(canonical(plan)),
            'source_released': source_released, 'source_release_intent': release_intent,
            'exit_code': observed_exit, 'reason': reason, 'checks': checks,
            'launcher_attempted': launcher_attempted,
            'output_bytes': min(output_bytes, job['max_output_bytes']), 'output_sha256': output.hexdigest(),
            'cleanup': clean, 'identity': identity, 'broker_snapshot_admitted': False}
        try:
            validate_native_receipt(native, job, plan)
            private.create('native-receipt.json', canonical(native))
            state.update(phase='complete', native_receipt_sha256=digest(canonical(native)))
            private.replace('state.json', canonical(state))
        finally:
            if lock is not None:
                os.close(lock)
            private.close()
    return 0


def helper_entry(phase, target):
    """Only the pinned bootstrap uses these fixed phases, never model arguments."""
    try:
        if phase == 'supervise':
            return supervise(Path(target))
        token = decode(target.encode())
        require(type(token) is dict and set(token) == {'fd', 'sha256'})
        data = decode(read_sealed(token['fd'], token['sha256'], MAX_INPUT_PACKET))
        os.close(token['fd'])
        if phase == 'outer':
            require(type(data) is dict and set(data) == {'packet', 'command'})
            return outer(data['packet'], data['command'])
        if phase == 'inner':
            require(type(data) is dict and set(data) == {'packet', 'plan'})
            return inner(data['packet'])
    except BaseException:
        pass
    return 78
