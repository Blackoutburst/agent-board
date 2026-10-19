"""Offline package tests: no real account, system service, network or Codex call."""
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy.isolated_host import manage, package as pkg

ROOT = Path(__file__).resolve().parents[1]


def elf_fixture(machine=62):
    value = bytearray(64)
    value[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HH', value, 16, 3, machine)
    return bytes(value)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='isolated-host-')
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / 'repo'; self.source.mkdir()
        files = {
            'integrations/codex-personal/blackoutburst_agents/__init__.py': '__version__ = "0.23.0"\n',
            'integrations/codex-personal/blackoutburst_agents/cli.py': '# public fixture\n',
            'integrations/codex-personal/pyproject.toml': '[project]\nversion = "0.23.0"\n',
            'connect.py': '# public connector fixture\n',
            # These tracked private-looking extras must not enter the inventory.
            'connection.json': '{"token":"PRIVATE-BOARD-SENTINEL"}',
            '.codex/auth.json': '{"secret":"PRIVATE-AUTH-SENTINEL"}',
            'integrations/codex-personal/tests/receipt.json': 'PRIVATE-CAPTURE-SENTINEL',
        }
        for name in ('package.py', 'owner_entry.py', 'unit.service.in'):
            files['deploy/isolated_host/' + name] = (ROOT / 'deploy/isolated_host' / name).read_text()
        for name, content in files.items():
            destination = self.source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        self.codex = self.base / 'codex'; self.codex.write_bytes(elf_fixture())
        self.bwrap = self.base / 'bwrap'; self.bwrap.write_bytes(elf_fixture())
        self.output = self.base / 'bundle'
        self.options = dict(codex=self.codex, bwrap=self.bwrap, codex_version='0.163.0',
                            architecture='x86_64', python=Path('/usr/bin/python3.10'),
                            python_sha256=hashlib.sha256(elf_fixture()).hexdigest(),
                            board_endpoint='https://board.example/mcp',
                            owners=dict(zip(pkg.OWNER_NAMES, ('owner-a', 'owner-b'))))

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.source), *args], capture_output=True, check=True).stdout

    def build(self, **changes):
        return manage.prepare(self.source, self.output, **{**self.options, **changes})

    def assertCode(self, code, call):
        with self.assertRaises(pkg.PackageError) as error:
            call()
        self.assertEqual(error.exception.code, code)

    def test_bundle_contains_only_pinned_public_code_and_explicit_tools(self):
        result = self.build()
        manifest = pkg.load_bundle(self.output)
        self.assertFalse(result['activated'])
        self.assertFalse(result['execution_qualified'])
        self.assertEqual(result['bundle_id'], pkg.manifest_id(manifest))
        self.assertEqual(set(manifest['owners']), set(pkg.OWNER_NAMES))
        for path in self.output.rglob('*'):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertNotIn(b'PRIVATE-', path.read_bytes())
        self.assertFalse((self.output / 'connection.json').exists())

    def test_missing_tools_fail_without_creating_output_or_running_a_command(self):
        with patch.object(manage, 'command') as command:
            self.assertCode('linux_tools_required', lambda: self.build(codex=None))
        command.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_macos_wrong_architecture_and_nonelf_tools_refuse(self):
        for value in (b'\xcf\xfa\xed\xfe' + b'0' * 60, b'#!/bin/sh\n', elf_fixture(183)):
            self.codex.write_bytes(value)
            self.assertCode('linux_elf_required', self.build)
            self.assertFalse(self.output.exists())

    def test_dirty_checkout_is_not_a_release(self):
        (self.source / 'connect.py').write_text('changed\n')
        self.assertCode('clean_source_required', self.build)
        self.assertFalse(self.output.exists())

    def test_invalid_identity_path_and_endpoint_fail_before_bundle_publication(self):
        cases = [({'owners': dict.fromkeys(pkg.OWNER_NAMES, 'same')}, 'distinct_board_owners_required'),
                 ({'python': Path('/usr/bin/python3.10;touch')}, 'invalid_path'),
                 ({'python': Path('/home/ubuntu/python')}, 'explicit_system_python_required'),
                 ({'board_endpoint': 'https://user:secret@board.example/mcp'}, 'invalid_board_endpoint')]
        for changes, code in cases:
            with self.subTest(code=code):
                self.assertCode(code, lambda: self.build(**changes))
                self.assertFalse(self.output.exists())

    def test_extra_secret_or_tampered_source_rejects_bundle(self):
        self.build()
        extra = self.output / 'auth.json'; extra.write_text('secret')
        self.assertCode('inventory_mismatch', lambda: pkg.load_bundle(self.output))
        extra.unlink()
        (self.output / 'source/connect.py').write_text('# tamper')
        self.assertCode('pin_changed', lambda: pkg.load_bundle(self.output))

    def test_symlink_and_hardlinked_tool_inputs_refuse(self):
        other = self.base / 'linked-codex'; other.symlink_to(self.codex)
        self.assertCode('symlink_path', lambda: self.build(codex=other))
        other.unlink(); os.link(self.codex, other)
        self.assertCode('invalid_regular_file', lambda: self.build(codex=other))

    def test_inventory_cannot_be_extended_to_credentials_or_paths_outside_bundle(self):
        self.build(); manifest = pkg.load_bundle(self.output)
        for name in ('../escape', '/absolute', 'source/auth.json', 'source/blackoutburst_agents/.secret'):
            forged = deepcopy(manifest)
            forged['files'][name] = {'size': 1, 'sha256': '0' * 64}
            with self.assertRaises(pkg.PackageError):
                pkg.validate_manifest(forged)

    def test_official_linux_codex_size_fits_the_bounded_inventory(self):
        self.build(); manifest = pkg.load_bundle(self.output)
        # Actual rust-v0.163.0 uncompressed ELF; no large allocation is needed
        # to check the inventory admission that initially rejected this binary.
        manifest['files']['tools/codex']['size'] = 296865480
        self.assertEqual(pkg.validate_manifest(manifest), manifest)
        manifest['files']['tools/codex']['size'] = pkg.MAX_FILE + 1
        self.assertCode('invalid_inventory', lambda: pkg.validate_manifest(manifest))

    def test_rendered_units_are_disabled_bounded_and_never_rearm(self):
        self.build(); manifest = pkg.load_bundle(self.output)
        template = (self.output / 'unit.service.in').read_text()
        release = pkg.RELEASES / pkg.manifest_id(manifest)
        for name in pkg.OWNER_NAMES:
            unit = pkg.render_unit(template, manifest, release, name)
            self.assertIn('User=' + name + '\n', unit)
            self.assertIn('Restart=no\n', unit)
            self.assertIn('KillMode=control-group\n', unit)
            self.assertIn('UMask=0077\n', unit)
            self.assertIn('ProtectSystem=strict\n', unit)
            self.assertIn(' -I -B ', unit)
            self.assertNotIn('HOME=', unit)
            self.assertNotIn('CODEX_HOME=', unit)
            self.assertNotIn(' enable', unit)
            self.assertNotIn('ExecStartPre=', unit)
            self.assertNotIn('Restart=on-failure', unit)

    def test_install_command_defaults_to_plan_without_os_changes(self):
        self.build()
        output = io.StringIO()
        with patch.object(manage, 'install') as installer, redirect_stdout(output):
            self.assertEqual(manage.main(['install', '--bundle', str(self.output)]), 0)
        installer.assert_not_called()
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt['status'], 'plan')
        self.assertFalse(receipt['activated'])

    def test_apply_requires_administrator_and_never_prints_private_details(self):
        self.build(); output = io.StringIO()
        with patch.object(manage.os, 'geteuid', return_value=501), patch.object(manage, 'command') as command, redirect_stdout(output):
            self.assertEqual(manage.main(['install', '--bundle', str(self.output), '--apply']), 2)
        command.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['reason'], 'administrator_required')
        self.assertNotIn(str(self.base), output.getvalue())

    def test_installer_uses_only_fixed_argv_and_leaves_all_services_disabled(self):
        self.build()
        base, releases, units = self.base / 'state', self.base / 'releases', self.base / 'units'
        units.mkdir()
        users, calls = {}, []

        def call(argv):
            calls.append(argv)
            if argv[0] == '/usr/sbin/useradd':
                name = argv[-1]
                users[name] = SimpleNamespace(pw_uid=1200 + len(users), pw_gid=1200 + len(users),
                                              pw_dir=str(base / 'owners' / name))
            return b''

        with patch.object(manage, '_preflight'), patch.object(manage, 'protected'), \
                patch.object(manage, 'BASE', base), patch.object(pkg, 'BASE', base), \
                patch.object(manage, 'RELEASES', releases), patch.object(pkg, 'RELEASES', releases), \
                patch.object(manage, 'UNIT_DIR', units), patch.object(manage, 'command', side_effect=call), \
                patch.object(manage.pwd, 'getpwnam', side_effect=lambda name: users[name]), \
                patch.object(manage.os, 'chown') as chown:
            result = manage.install(self.output)
        self.assertEqual(result['status'], 'installed_disabled')
        self.assertFalse(result['activated'])
        self.assertEqual([x for x in calls if x[0] == '/usr/bin/systemctl'],
                         [['/usr/bin/systemctl', 'daemon-reload']])
        self.assertEqual({x[-1] for x in calls if x[0] == '/usr/sbin/useradd'}, set(pkg.OWNER_NAMES))
        self.assertNotIn('ubuntu', users)
        self.assertTrue(chown.called)
        for name in pkg.OWNER_NAMES:
            root = base / 'owners' / name
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertFalse((root / '.local/state/blackoutburst-agents/host').exists())
            self.assertFalse((root / '.codex').exists())
            self.assertFalse(any(root.rglob('*.json')))
            self.assertEqual((units / pkg.unit_name(name)).stat().st_mode & 0o777, 0o644)
        for path in releases.rglob('*'):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o022, 0)

    def test_root_protection_rejects_agent_writable_source(self):
        root = self.base / 'trusted'; root.mkdir(mode=0o700)
        source = root / 'file'; source.write_text('fixture'); source.chmod(0o644)
        pkg.protected(source, uid=os.geteuid(), stop_at=root)
        source.chmod(0o664)
        self.assertCode('untrusted_installation', lambda: pkg.protected(source, uid=os.geteuid(), stop_at=root))

    def test_preflight_refuses_wrong_host_before_system_command(self):
        self.build(); manifest = pkg.load_bundle(self.output)
        with patch.object(manage.os, 'geteuid', return_value=0), \
                patch.object(manage.platform, 'system', return_value='Darwin'), \
                patch.object(manage, 'command') as command:
            self.assertCode('linux_host_mismatch', lambda: manage._preflight(manifest))
        command.assert_not_called()

    def test_partial_installation_preserves_owner_data_and_records_review(self):
        self.build()
        base, releases, units = self.base / 'state', self.base / 'releases', self.base / 'units'
        units.mkdir(); users, calls = {}, []

        def call(argv):
            calls.append(argv)
            if argv[0] == '/usr/sbin/useradd':
                if users:
                    # Simulated administrator/system failure, never a real useradd.
                    raise pkg.PackageError('system_command_failed')
                name = argv[-1]
                users[name] = SimpleNamespace(pw_uid=1200, pw_gid=1200, pw_dir=str(base / 'owners' / name))
            return b''

        with patch.object(manage, '_preflight'), patch.object(manage, 'protected'), \
                patch.object(manage, 'BASE', base), patch.object(pkg, 'BASE', base), \
                patch.object(manage, 'RELEASES', releases), patch.object(pkg, 'RELEASES', releases), \
                patch.object(manage, 'UNIT_DIR', units), patch.object(manage, 'command', side_effect=call), \
                patch.object(manage.pwd, 'getpwnam', side_effect=lambda name: users[name]), \
                patch.object(manage.os, 'chown'):
            self.assertCode('system_command_failed', lambda: manage.install(self.output))
        receipt = json.loads(next((base / 'installations').glob('*.json')).read_text())
        self.assertEqual(receipt['status'], 'incomplete_requires_review')
        self.assertEqual(len(receipt['created_accounts']), 1)
        self.assertTrue((base / 'owners' / pkg.OWNER_NAMES[0]).exists())
        self.assertFalse(any(argv[0] == '/usr/bin/systemctl' for argv in calls))
        self.assertFalse(any('userdel' in str(argv) for argv in calls))

    def test_existing_account_refuses_without_useradd_or_modification(self):
        self.build(); manifest = pkg.load_bundle(self.output)
        calls = []
        def call(argv):
            calls.append(argv)
            return b'3.10\n' if argv[0].endswith('python3.10') else b'systemd 249 (249.11)\n'
        with patch.object(manage.os, 'geteuid', return_value=0), \
                patch.object(manage.platform, 'system', return_value='Linux'), \
                patch.object(manage.platform, 'machine', return_value='x86_64'), \
                patch.object(manage, 'protected'), patch.object(manage, 'read_regular', return_value=elf_fixture()), \
                patch.object(manage, 'command', side_effect=call), \
                patch.object(manage.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=1200)):
            self.assertCode('existing_account_requires_review', lambda: manage._preflight(manifest))
        self.assertEqual(len(calls), 2)
        self.assertTrue(all('useradd' not in str(argv) for argv in calls))


if __name__ == '__main__':
    unittest.main()
