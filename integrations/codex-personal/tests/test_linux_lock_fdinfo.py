"""Strict observation/reader unit models only; no procfs or host qualification."""
import os
import stat
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from blackoutburst_agents.dev_journal import _parse_lock_fdinfo, _linux_lock_observation
from blackoutburst_agents.dev_manifest import DevelopmentError


VALID = b'pos:\t0\nflags:\t0100002\nmnt_id:\t17\nino:\t42\nlock:\t1: FLOCK ADVISORY WRITE 123 00:01:42 0 EOF\n'


class LockFdinfoCodecTests(TestCase):
    def parse(self, data, **changes):
        return _parse_lock_fdinfo(data, **(dict(pid=123, device=os.makedev(0,1), inode=42) | changes))

    def test_exact_and_large_supported_numbers(self):
        self.assertEqual(self.parse(VALID), (17, int('0100002',8)))
        value = VALID.replace(b'42', b'18446744073709551615')
        self.assertEqual(self.parse(value, inode=(1<<64)-1)[0],17)

    def test_hostile_ambiguous_or_nonexclusive_observations(self):
        for value in [None, '', bytearray(VALID), b'', VALID[:-1], VALID+b'pos: 0\n',
                VALID+b'lock: 2: FLOCK ADVISORY WRITE 123 00:01:42 0 EOF\n',
                VALID+b'unknown: 1\n', VALID.replace(b'pos:\t0', b'pos: -1'),
                VALID.replace(b'pos:\t0', b'pos: '+b'9'*5000), VALID.replace(b'0\nflags',b'0\r\nflags'),
                VALID.replace(b'WRITE',b'READ'), VALID.replace(b'FLOCK',b'POSIX'),
                VALID.replace(b'FLOCK',b'OFDLCK'), VALID.replace(b'ADVISORY',b'MANDATORY'),
                VALID.replace(b'123',b'0'), VALID.replace(b'123',b'124'),
                VALID.replace(b'1: FLOCK',b'0: FLOCK'), VALID.replace(b'1: FLOCK',b'-> 1: FLOCK'),
                VALID.replace(b'00:01:42',b'00:02:42'), VALID.replace(b'00:01:42',b'00:01:43'),
                VALID.replace(b'0 EOF',b'1 EOF'), VALID.replace(b'0 EOF',b'0 42'),
                VALID.replace(b'0100002',b'0100000'), VALID.replace(b'0100002',b'0190002'),
                VALID.replace(b'mnt_id:\t17',b'mnt_id: 0'), VALID.replace(b'ino:\t42',b'ino: 41'),
                VALID.replace(b'pos',b'\xffpos'), VALID.replace(b'pos',b'\x00pos')]:
            with self.subTest(value=value):
                with self.assertRaises(DevelopmentError):self.parse(value)
        for changes in [dict(pid=True), dict(device=True), dict(inode=True), dict(pid=-1)]:
            with self.assertRaises(DevelopmentError):self.parse(VALID, **changes)

    def test_reader_only_uses_exact_current_pid_and_fd_with_bounded_reads(self):
        info = SimpleNamespace(st_dev=os.makedev(0,1),st_ino=42,st_mode=stat.S_IFREG | 0o600)
        with patch('blackoutburst_agents.dev_journal._linux_lock_supported',return_value=True), \
             patch('os.getpid',return_value=123), patch('os.readlink',return_value='123'), patch('os.fstat',return_value=info), \
             patch('blackoutburst_agents.dev_journal.absolute_directory',return_value=70) as directory, \
             patch('os.open',return_value=71) as opened, patch('os.read',side_effect=[VALID,b'']) as read, \
             patch('os.close') as closed, patch('fcntl.flock',side_effect=AssertionError('never acquire')):
            self.assertEqual(_linux_lock_observation(9,pid=123,expected=(info.st_dev,42)),(17,int('0100002',8)))
            directory.assert_called_once_with('/proc/123/fdinfo')
            self.assertEqual(opened.call_args.args[0],'9')
            self.assertTrue(opened.call_args.args[1] & os.O_NOFOLLOW)
            self.assertTrue(all(0 < call.args[1] <= 4097 for call in read.call_args_list))
            self.assertEqual([call.args[0] for call in closed.call_args_list],[71,70])

    def test_missing_changed_fd_fork_and_overflow_refuse_without_flock(self):
        info = SimpleNamespace(st_dev=os.makedev(0,1),st_ino=42,st_mode=stat.S_IFREG | 0o600)
        for change in ['unsupported','other_pid','foreign_proc_namespace','missing','denied','changed','nonregular','overflow']:
            with self.subTest(change=change), \
                 patch('blackoutburst_agents.dev_journal._linux_lock_supported',return_value=change!='unsupported'), \
                 patch('os.getpid',return_value=124 if change=='other_pid' else 123), \
                 patch('os.readlink',return_value='999' if change=='foreign_proc_namespace' else '123'), \
                 patch('os.fstat',return_value=info) as fstat, \
                 patch('blackoutburst_agents.dev_journal.absolute_directory',return_value=70), \
                 patch('os.open',return_value=71) as opened, patch('os.read',side_effect=[VALID,b'']) as read, \
                 patch('os.close'), patch('fcntl.flock',side_effect=AssertionError('never acquire')):
                if change=='missing':opened.side_effect=FileNotFoundError()
                if change=='denied':read.side_effect=PermissionError()
                if change=='changed':
                    fstat.side_effect=[info,info,SimpleNamespace(st_dev=info.st_dev,st_ino=43)]
                if change=='nonregular':fstat.side_effect=[info,SimpleNamespace(st_mode=stat.S_IFDIR)]
                if change=='overflow':read.side_effect=[b'x'*4097]
                with self.assertRaises(DevelopmentError):
                    _linux_lock_observation(9,pid=123,expected=(info.st_dev,42))
                if change in ['unsupported','other_pid','foreign_proc_namespace']:opened.assert_not_called()
