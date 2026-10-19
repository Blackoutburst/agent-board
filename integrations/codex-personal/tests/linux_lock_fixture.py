"""Explicit OFD/fdinfo model for portable unit fixtures, never host evidence.

Real fixture file operations/locks still run. Only fdinfo is simulated from
successful tracked operations; production code receives no injection argument.
No process, fork, host qualification, model or RPC is used.
"""
import fcntl
import os
from unittest.mock import patch

from blackoutburst_agents.dev_journal import _parse_lock_fdinfo


class LockObservationFixture:
    def __init__(self, case):
        self.handles, self.locks = {}, {}
        self.open, self.close, self.dup, self.flock = os.open, os.close, os.dup, fcntl.flock
        for target, replacement in [('os.open', self._open), ('os.close', self._close),
                ('os.dup', self._dup), ('fcntl.flock', self._flock),
                ('blackoutburst_agents.dev_journal._linux_lock_supported', lambda: True),
                ('blackoutburst_agents.dev_journal._linux_lock_observation', self.observe)]:
            context = patch(target, side_effect=replacement)
            context.start();case.addCleanup(context.stop)

    def _open(self, *args, **kwargs):
        descriptor = self.open(*args, **kwargs)
        self.handles[descriptor] = object()
        return descriptor

    def _dup(self, descriptor):
        duplicate = self.dup(descriptor)
        self.handles[duplicate] = self.handles[descriptor]
        return duplicate

    def _close(self, descriptor):
        self.close(descriptor)
        token = self.handles.pop(descriptor, None)
        if token is not None and token not in self.handles.values():self.locks.pop(token, None)

    def _flock(self, descriptor, operation):
        result = self.flock(descriptor, operation)
        token = self.handles[descriptor]
        if operation == fcntl.LOCK_UN:self.locks.pop(token, None)
        else:self.locks[token] = (operation & ~fcntl.LOCK_NB, os.getpid())
        return result

    def observe(self, descriptor, *, pid, expected):
        from blackoutburst_agents.dev_manifest import identity, DevelopmentError
        info = os.fstat(descriptor)
        if identity(info) != expected:raise DevelopmentError('fixture_changed_fd')
        held = self.locks.get(self.handles.get(descriptor))
        data = f'pos:\t0\nflags:\t0100002\nmnt_id:\t1\nino:\t{info.st_ino}\n'
        if held:
            mode = 'WRITE' if held[0] == fcntl.LOCK_EX else 'READ'
            data += (f'lock:\t1: FLOCK ADVISORY {mode} {held[1]} '
                     f'{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino} 0 EOF\n')
        return _parse_lock_fdinfo(data.encode('ascii'), pid=pid, device=info.st_dev, inode=info.st_ino)
