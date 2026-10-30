"""Real protected reads: exact bytes, bounded input and concurrent substitution."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.git_workspace import _file


class ProtectedFileReadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='protected-file-read-', dir='/private/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.parent = self.root/'parent'
        self.parent.mkdir(mode=0o700)
        self.path = self.parent/'input'

    def write(self, raw):
        self.path.write_bytes(raw)
        self.path.chmod(0o600)

    def test_exact_boundary_and_binary_bytes(self):
        for raw, maximum in ((b'', 0), (b'\x00\xff', 2),
                             (bytes(range(256))*256, 65536),
                             (b'z'*131079, 131079), (b'short', 100)):
            with self.subTest(length=len(raw), maximum=maximum):
                self.write(raw)
                self.assertEqual(_file(self.path, maximum), raw)

    def test_oversized_file_is_refused_before_read(self):
        self.write(b'a'*65537)
        with patch('blackoutburst_agents.git_workspace.os.read') as read:
            with self.assertRaisesRegex(DevelopmentError, 'git_workspace_file_refused'):
                _file(self.path, 65536)
        read.assert_not_called()

    def test_short_reads_keep_each_byte_and_never_grow_a_prefix(self):
        # A read chunk is a bytes object. Refuse concatenating a growing
        # prefix onto it; a single final join accepts the same exact bytes.
        class Chunk(bytes):
            def __radd__(self, other):
                raise AssertionError('Repeated immutable-prefix copying')

        raw = bytes(range(256))*1025
        self.write(raw)
        original = os.read
        requested = []
        def read(fd, maximum):
            requested.append(maximum)
            return Chunk(original(fd, min(maximum, 127)))
        with patch('blackoutburst_agents.git_workspace.os.read', read):
            self.assertEqual(_file(self.path, len(raw)), raw)
        self.assertGreater(len(requested), 2000)
        self.assertTrue(all(1 <= value <= 65536 for value in requested))

    def test_mutation_during_read_refuses_and_closes_descriptor(self):
        for mutation in ('content', 'truncate', 'grow', 'replace', 'parent'):
            with self.subTest(mutation=mutation):
                # Use a distinct existing directory for each substitution.
                self.parent = self.root/('case-'+mutation)
                self.parent.mkdir(mode=0o700)
                self.path = self.parent/'input'
                self.write(b'a'*131072)
                original = os.read
                descriptors = []
                def read(fd, maximum):
                    part = original(fd, min(maximum, 65536))
                    if not descriptors:
                        descriptors.append(fd)
                        if mutation == 'content':
                            self.path.write_bytes(b'b'*131072)
                        elif mutation == 'truncate':
                            self.path.write_bytes(b'x')
                        elif mutation == 'grow':
                            with self.path.open('ab') as stream:
                                stream.write(b'overflow')
                        elif mutation == 'replace':
                            self.path.rename(self.parent/'old-input')
                            self.write(b'a'*131072)
                        else:
                            self.parent.rename(self.root/'previous-parent')
                            self.parent.mkdir(mode=0o700)
                            self.write(b'a'*131072)
                    return part
                with patch('blackoutburst_agents.git_workspace.os.read', read):
                    with self.assertRaisesRegex(DevelopmentError, 'git_workspace_file_changed'):
                        _file(self.path, 131072)
                self.assertEqual(len(descriptors), 1)
                with self.assertRaises(OSError):
                    os.fstat(descriptors[0])

    def test_symlink_writable_and_non_regular_inputs_remain_refused(self):
        self.write(b'input')
        target = self.parent/'target'
        self.path.rename(target)
        self.path.symlink_to(target.name)
        with self.assertRaisesRegex(DevelopmentError, 'git_workspace_file_refused'):
            _file(self.path, 100)
        self.path.unlink()
        self.write(b'input')
        self.path.chmod(0o622)
        with self.assertRaisesRegex(DevelopmentError, 'git_workspace_file_refused'):
            _file(self.path, 100)
        self.path.unlink()
        self.path.mkdir(mode=0o700)
        with self.assertRaisesRegex(DevelopmentError, 'git_workspace_file_refused'):
            _file(self.path, 100)

    def test_executable_hardlink_exception_is_unchanged(self):
        self.write(b'synthetic executable bytes')
        os.link(self.path, self.parent/'same-file')
        with self.assertRaisesRegex(DevelopmentError, 'git_workspace_file_refused'):
            _file(self.path, 100)
        self.assertEqual(_file(self.path, 100, executable=True),
                         b'synthetic executable bytes')


if __name__ == '__main__':
    unittest.main()
