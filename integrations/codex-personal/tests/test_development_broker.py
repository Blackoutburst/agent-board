"""Synthetic broker checks only: no model, command, network or real credentials."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from blackoutburst_agents.dev_broker import DevelopmentBroker
from blackoutburst_agents.dev_manifest import (Budgets, DevelopmentError, NamedTest, digest,
                                              prepare_manifest)


class Interrupted(BaseException):
    pass


class FakeTestBackend:
    backend_id = 'fixture-tests'

    def __init__(self):
        self.calls = []
        self.lookups = []
        self.results = {}
        self.qualified = True
        self.interrupt = False

    def qualified_for(self, manifest_hash, suite):
        return self.qualified

    def run(self, request):
        self.calls.append(request)
        result = {'status': 'passed', 'cleanup_confirmed': True, 'output_bytes': 2,
                  'output_sha256': digest(b'ok')}
        self.results[request.operation_id] = result
        if self.interrupt:
            raise Interrupted()
        return result

    def lookup(self, request):
        self.lookups.append(request)
        return self.results.get(request.operation_id)


class DevelopmentBrokerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'checkout'
        self.root.mkdir(mode=0o700)
        (self.root / 'src').mkdir(mode=0o700)
        (self.root / 'src/main.py').write_text('value = 1\n')
        self.directory = self.base / 'private-state'
        self.scope = dict(owner_id='owner', agent_id='profile', device_id='host', session_id='chat',
                          project_id='project', ticket_id='ticket', profile_version=2)
        self.allow = True
        self.checks = []
        self.backend = FakeTestBackend()
        self.prepare()
        self.broker = self.open()

    def authorized(self, scope, action, resource):
        self.checks.append((scope, action, resource))
        return self.allow

    def prepare(self, **changes):
        values = dict(root=self.root, base_sha='a' * 40, scope=self.scope, paths=['src/main.py'],
            resource='repo:fixture', authorize_scope=self.authorized,
            tests=[NamedTest('unit', backend_id='fixture-tests')])
        values.update(changes)
        return prepare_manifest(self.directory, **values)

    def open(self, **options):
        values = dict(authorize_scope=self.authorized, enabled=True, test_backend=self.backend)
        values.update(options)
        broker = DevelopmentBroker(self.directory, **values)
        self.addCleanup(broker.close)
        return broker

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(DevelopmentError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def old_hash(self):
        return digest((self.root / 'src/main.py').read_bytes())

    def patch_file(self, key='patch-one', content='value = 2\n', expected=None):
        return self.broker.patch_file(key, 'src/main.py', expected or self.old_hash(), content)

    def state(self):
        return json.loads((self.directory / 'state.json').read_text())

    def test_default_disabled_and_no_backend_executes(self):
        disabled = self.open(enabled=False)
        before = (self.root / 'src/main.py').read_bytes()
        for method, args in [(disabled.read_file, ('src/main.py',)),
                             (disabled.patch_file, ('key', 'src/main.py', self.old_hash(), 'new')),
                             (disabled.run_test, ('test', 'unit')), (disabled.diff, ())]:
            self.error('broker_disabled', method, *args)
        self.assertEqual((self.root / 'src/main.py').read_bytes(), before)
        self.assertFalse(self.backend.calls)

    def test_manifest_and_journal_are_private_and_plan_is_immutable(self):
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        for path in self.directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.error('private_file_exists', self.prepare)
        view = self.broker.manifest
        view['files']['elsewhere'] = {'sha256': '0' * 64, 'bytes': 0}
        view['scope']['owner_id'] = 'other-owner'
        self.assertEqual(self.broker.manifest['scope']['owner_id'], 'owner')
        self.error('path_not_allowed', self.broker.read_file, 'elsewhere')
        changed = self.broker.manifest
        changed['base_sha'] = 'b' * 40
        (self.directory / 'manifest.json').write_text(json.dumps(changed))
        self.error('manifest_changed', self.broker.read_file, 'src/main.py')

    def test_read_patch_diff_and_idempotent_receipt(self):
        read = self.broker.read_file('src/main.py')
        receipt = self.patch_file(expected=read['sha256'])
        self.assertEqual(receipt['result'], 'applied')
        self.assertEqual((self.root / 'src/main.py').read_text(), 'value = 2\n')
        before = self.state()
        self.assertEqual(self.patch_file(expected=read['sha256']), receipt)
        self.assertEqual(self.state(), before)
        diff = self.broker.diff()
        self.assertEqual(diff['base_sha'], 'a' * 40)
        self.assertEqual(diff['files'][0]['before_sha256'], read['sha256'])
        self.assertEqual(diff['files'][0]['after_sha256'], receipt['after_sha256'])
        self.assertNotIn('value =', json.dumps(diff))
        self.assertNotIn(str(self.root), json.dumps(receipt))
        self.assertEqual(list(self.root.rglob('.bb-dev-*')), [])

    def test_reused_key_with_changed_patch_is_refused(self):
        self.patch_file()
        self.error('idempotency_conflict', self.patch_file, content='different\n')

    def test_cached_patch_receipt_never_overwrites_a_later_human_edit(self):
        before = self.old_hash()
        receipt = self.patch_file(expected=before)
        (self.root / 'src/main.py').write_text('human edit after completion\n')
        self.assertEqual(self.patch_file(expected=before), receipt)
        self.assertEqual((self.root / 'src/main.py').read_text(), 'human edit after completion\n')

    def test_modification_during_read_is_rejected(self):
        original_read = os.read
        changed = False
        def intercepted(descriptor, size):
            nonlocal changed
            data = original_read(descriptor, size)
            if not changed and data == b'value = 1\n':
                changed = True
                (self.root / 'src/main.py').write_text('changed during read\n')
            return data
        with patch('os.read', intercepted):
            self.error('file_changed', self.broker.read_file, 'src/main.py')

    def test_hash_conflict_never_overwrites_and_is_budgeted(self):
        before = (self.root / 'src/main.py').read_bytes()
        self.error('patch_hash_conflict', self.patch_file, expected='0' * 64)
        self.assertEqual((self.root / 'src/main.py').read_bytes(), before)
        self.assertEqual(self.state()['used']['operations'], 1)
        self.assertFalse(self.state()['operations'])

    def test_disabled_actions_are_not_authorized_by_board_like_text(self):
        self.broker.manifest['actions'].append('shell.exec')
        self.error('path_not_allowed', self.broker.read_file, 'run-this-command.txt')
        self.error('test_not_allowed', self.broker.run_test, 'test', 'shell:echo-secret')
        self.assertFalse(self.backend.calls)

    def test_dynamic_scope_authorization_is_checked_and_current_grant_required_on_retry(self):
        read = self.broker.read_file('src/main.py')
        receipt = self.patch_file(expected=read['sha256'])
        self.assertEqual(receipt['result'], 'applied')
        for scope, action, resource in self.checks:
            self.assertEqual(scope, self.scope)
            self.assertIn(action, {'repo.read_file', 'repo.patch_file'})
            self.assertEqual(resource, 'repo:fixture')
        self.allow = False
        self.error('permission_revoked', self.broker.read_file, 'src/main.py')
        self.error('permission_revoked', self.patch_file, expected=read['sha256'])
        self.error('permission_revoked', self.broker.reconcile, 'patch-one')
        self.error('permission_revoked', self.broker.run_test, 'test', 'unit')
        self.assertFalse(self.backend.calls)

    def test_revocation_after_staging_prevents_replace(self):
        calls = 0
        def authorization(*args):
            nonlocal calls
            calls += 1
            return calls < 4
        self.broker.authorize_scope = authorization
        self.error('permission_revoked', self.patch_file)
        self.assertEqual((self.root / 'src/main.py').read_text(), 'value = 1\n')
        self.broker.authorize_scope = self.authorized
        self.error('stage_cleanup_requires_review', self.broker.reconcile, 'patch-one')
        self.error('reconciliation_required', self.patch_file, key='other-key')
        stages = list(self.root.rglob('.bb-dev-*'))
        self.assertEqual(len(stages), 1)
        # Synthetic operator cleanup after inspecting the retained stage.
        stages[0].unlink()
        self.assertEqual(self.broker.reconcile('patch-one')['result'], 'not_applied')
        self.assertEqual(list(self.root.rglob('.bb-dev-*')), [])

    def test_changed_inode_between_staging_and_replace_is_not_overwritten(self):
        calls = 0
        def authorization(*args):
            nonlocal calls
            calls += 1
            if calls == 4:
                other = self.root / 'src/other'
                other.write_text('human update\n')
                other.replace(self.root / 'src/main.py')
            return True
        self.broker.authorize_scope = authorization
        self.error('file_changed', self.patch_file)
        self.assertEqual((self.root / 'src/main.py').read_text(), 'human update\n')
        self.broker.authorize_scope = self.authorized
        self.assertEqual(self.broker.reconcile('patch-one')['status'], 'needs_review')
        self.error('reconciliation_required', self.patch_file, key='new-key')

    def test_parent_replaced_before_commit_is_detected(self):
        calls = 0
        def authorization(*args):
            nonlocal calls
            calls += 1
            if calls == 4:
                (self.root / 'src').rename(self.root / 'old-src')
                (self.root / 'src').mkdir(mode=0o700)
                (self.root / 'src/main.py').write_text('new directory\n')
            return True
        self.broker.authorize_scope = authorization
        self.error('parent_changed', self.patch_file)
        self.assertEqual((self.root / 'src/main.py').read_text(), 'new directory\n')
        self.assertEqual((self.root / 'old-src/main.py').read_text(), 'value = 1\n')

    def test_root_replaced_or_symlinked_never_redirects_effects(self):
        self.root.rename(self.base / 'original-root')
        self.root.mkdir(mode=0o700)
        self.error('workspace_identity_changed', self.broker.read_file, 'src/main.py')
        self.root.rmdir()
        self.root.symlink_to(self.base / 'original-root', target_is_directory=True)
        self.error('directory_access_refused', self.broker.read_file, 'src/main.py')

    def test_file_and_directory_symlinks_are_refused(self):
        original = self.root / 'src/main.py'
        saved = self.base / 'private-secret'
        original.rename(saved)
        original.symlink_to(saved)
        self.error('file_access_refused', self.broker.read_file, 'src/main.py')
        original.unlink()
        saved.rename(original)
        (self.root / 'src').rename(self.base / 'external-src')
        (self.root / 'src').symlink_to(self.base / 'external-src', target_is_directory=True)
        self.error('file_access_refused', self.broker.read_file, 'src/main.py')

    def test_hardlinks_fifo_and_group_writable_targets_are_refused(self):
        target = self.root / 'src/main.py'
        link = self.base / 'same-inode'
        os.link(target, link)
        self.error('file_type_not_allowed', self.broker.read_file, 'src/main.py')
        link.unlink()
        target.chmod(0o666)
        self.error('file_type_not_allowed', self.broker.read_file, 'src/main.py')
        target.unlink()
        os.mkfifo(target, 0o600)
        self.error('file_type_not_allowed', self.broker.read_file, 'src/main.py')

    def test_traversal_private_and_nonlisted_paths_are_refused(self):
        for path in ('../secret', '/etc/passwd', 'src/../main.py', 'src//main.py', 'src\\main.py', 'src/\x00bad'):
            with self.subTest(path=path):
                self.error('invalid_path', self.broker.read_file, path)
        for path in ('.git/config', '.ssh/id_rsa', '.env', 'SRC/.ENV.prod', 'auth.json', 'src/key.pem', '.codex/config.toml'):
            with self.subTest(path=path):
                self.error('private_path', self.broker.read_file, path)
        self.error('path_not_allowed', self.broker.read_file, 'src/other.py')

    def test_obvious_credentials_refused_without_returning_values(self):
        samples = ['-----BEGIN PRIVATE KEY-----\nfixture', 'sk-' + 'a' * 32,
                   '{"refresh_token":"synthetic-fixture-value"}', 'Bearer ' + 'a' * 32]
        for value in samples:
            with self.subTest(value=value[:12]):
                error = self.error('secret_content_refused', self.patch_file, content=value)
                self.assertNotIn(value, str(error))
                (self.root / 'src/main.py').write_text(value)
                self.error('secret_content_refused', self.broker.read_file, 'src/main.py')
        self.assertFalse(self.state()['operations'])

    def test_oversize_and_invalid_text_are_bounded(self):
        self.error('file_too_large', self.patch_file, content='x' * 65537)
        self.error('invalid_patch', self.patch_file, content='\ud800')
        (self.root / 'src/main.py').write_bytes(b'\xff')
        self.error('file_not_utf8', self.broker.read_file, 'src/main.py')
        (self.root / 'src/main.py').write_bytes(b'\0')
        self.error('file_not_text', self.broker.read_file, 'src/main.py')

    def test_crash_after_intent_is_reconciled_without_reapplying(self):
        save = self.broker.journal.save
        def crash(state):
            save(state)
            if state['operations']:
                raise Interrupted()
        with patch.object(self.broker.journal, 'save', crash), self.assertRaises(Interrupted):
            self.patch_file()
        reopened = self.open()
        self.assertEqual(reopened.status('patch-one')['status'], 'intent')
        self.error('reconciliation_required', reopened.patch_file, 'patch-one', 'src/main.py', self.old_hash(), 'value = 2\n')
        self.assertEqual(reopened.reconcile('patch-one')['result'], 'not_applied')
        self.assertEqual((self.root / 'src/main.py').read_text(), 'value = 1\n')

    def test_crash_after_atomic_replace_recovers_receipt_without_second_write(self):
        with patch.object(self.broker, '_finish', side_effect=Interrupted), self.assertRaises(Interrupted):
            self.patch_file()
        target = self.root / 'src/main.py'
        before = target.stat()
        reopened = self.open()
        recovered = reopened.reconcile('patch-one')
        self.assertEqual(recovered['result'], 'verified_after_restart')
        self.assertEqual(target.stat().st_ino, before.st_ino)
        self.assertEqual(target.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(reopened.reconcile('patch-one'), recovered)

    def test_changed_stage_is_never_deleted_by_reconciliation(self):
        with patch.object(self.broker, '_finish', side_effect=Interrupted), self.assertRaises(Interrupted):
            self.patch_file()
        operation = next(iter(self.state()['operations'].values()))
        staged = self.root / 'src' / self.broker._stage_name(operation)
        staged.write_text('unrelated content')
        self.error('stage_cleanup_requires_review', self.broker.reconcile, 'patch-one')
        self.assertTrue(staged.exists())

    def test_journal_is_serialized_and_second_broker_refuses_concurrent_effect(self):
        other = self.open()
        with self.broker.journal.lock():
            self.error('broker_busy', other.patch_file, 'other', 'src/main.py', self.old_hash(), 'other\n')
        self.assertEqual((self.root / 'src/main.py').read_text(), 'value = 1\n')

    def test_test_backend_must_be_explicitly_qualified_and_named(self):
        for backend in (None, object()):
            other = self.open(test_backend=backend)
            self.error('test_backend_unqualified', other.run_test, 'test', 'unit')
        self.backend.qualified = False
        self.error('test_backend_unqualified', self.broker.run_test, 'test', 'unit')
        self.backend.qualified = True
        self.backend.backend_id = 'different-backend'
        self.error('test_backend_unqualified', self.broker.run_test, 'test', 'unit')
        self.assertFalse(self.backend.calls)
        self.assertEqual(self.state()['used']['tests'], 0)

    def test_named_test_receipt_and_retries_never_execute_twice(self):
        receipt = self.broker.run_test('test-key', 'unit')
        self.assertEqual(receipt['result'], 'passed')
        self.assertEqual(self.broker.run_test('test-key', 'unit'), receipt)
        self.assertEqual(len(self.backend.calls), 1)
        request = self.backend.calls[0]
        self.assertEqual(request.timeout_seconds, 60)
        self.assertEqual(request.max_output_bytes, 32768)
        self.assertNotIn(str(self.root), json.dumps(receipt))
        self.assertNotIn('content', json.dumps(receipt))

    def test_lost_test_result_uses_lookup_never_reexecutes(self):
        self.backend.interrupt = True
        with self.assertRaises(Interrupted):
            self.broker.run_test('test-key', 'unit')
        reopened = self.open()
        self.error('reconciliation_required', reopened.run_test, 'test-key', 'unit')
        self.assertEqual(reopened.reconcile('test-key')['result'], 'passed')
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(len(self.backend.lookups), 1)

    def test_unknown_test_effect_or_cleanup_keeps_scope_blocked(self):
        self.backend.interrupt = True
        with self.assertRaises(Interrupted):
            self.broker.run_test('test-key', 'unit')
        self.backend.results.clear()
        self.assertEqual(self.broker.reconcile('test-key')['status'], 'needs_review')
        self.error('reconciliation_required', self.patch_file)
        operation_id = self.backend.calls[0].operation_id
        self.backend.results[operation_id] = {'status': 'cancelled', 'cleanup_confirmed': False,
            'output_bytes': 0, 'output_sha256': digest(b'')}
        self.assertEqual(self.broker.reconcile('test-key')['status'], 'needs_review')
        self.backend.results[operation_id]['cleanup_confirmed'] = True
        self.assertEqual(self.broker.reconcile('test-key')['result'], 'cancelled')

    def test_backend_diagnostics_and_unknown_fields_are_never_persisted(self):
        self.backend.run = lambda _: {'status': 'passed', 'secret': 'synthetic-secret-diagnostic'}
        self.error('invalid_test_receipt', self.broker.run_test, 'test-key', 'unit')
        self.assertNotIn('synthetic-secret', (self.directory / 'state.json').read_text())
        self.assertEqual(self.broker.status('test-key')['status'], 'intent')

    def test_budget_reservations_survive_restart_and_failed_reads(self):
        other_state = self.base / 'limited-state'
        prepare_manifest(other_state, root=self.root, base_sha='a' * 40, scope=self.scope,
            paths=['src/main.py'], resource='repo:fixture', authorize_scope=self.authorized,
            budgets=Budgets(max_operations=2, max_file_bytes=32, max_read_bytes=32, max_patch_bytes=10, max_tests=0))
        with DevelopmentBroker(other_state, authorize_scope=self.authorized, enabled=True) as limited:
            limited.read_file('src/main.py')
        with DevelopmentBroker(other_state, authorize_scope=self.authorized, enabled=True) as limited:
            self.error('file_too_large', limited.read_file, 'src/main.py')
            self.error('operations_budget_exhausted', limited.read_file, 'src/main.py')
            self.error('operations_budget_exhausted', limited.patch_file, 'patch', 'src/main.py', self.old_hash(), 'long patch\n')

    def test_invalid_manifest_inputs_and_insecure_private_files_refused(self):
        other = self.base / 'new-state'
        for value in (True, -1, 0, 257):
            with self.subTest(value=value):
                self.error('invalid_budget', prepare_manifest, other, root=self.root, base_sha='a' * 40,
                    scope=self.scope, paths=['src/main.py'], resource='repo:fixture',
                    authorize_scope=self.authorized, budgets=Budgets(max_operations=value))
        (self.directory / 'state.json').chmod(0o644)
        self.error('insecure_private_file', self.broker.status, 'missing')

    def test_corrupt_receipt_cannot_disclose_arbitrary_private_text(self):
        self.patch_file()
        state = self.state()
        operation = next(iter(state['operations'].values()))
        operation['receipt']['diagnostic'] = 'synthetic-private-value'
        (self.directory / 'state.json').write_text(json.dumps(state))
        error = self.error('journal_corrupt', self.broker.status, 'patch-one')
        self.assertNotIn('synthetic-private-value', str(error))

    def test_hardlinked_or_symlinked_private_documents_are_refused(self):
        state = self.directory / 'state.json'
        saved = self.base / 'saved-state'
        os.link(state, saved)
        self.error('insecure_private_file', self.broker.status, 'missing')
        state.unlink()
        state.symlink_to(saved)
        self.error('private_read_failed', self.broker.status, 'missing')

    def test_manifest_inside_workspace_and_symlinked_private_directory_refused(self):
        self.error('journal_inside_workspace', prepare_manifest, self.root / 'state', root=self.root,
            base_sha='a' * 40, scope=self.scope, paths=['src/main.py'], resource='repo:fixture', authorize_scope=self.authorized)
        alias = self.base / 'alias'
        alias.symlink_to(self.directory, target_is_directory=True)
        self.error('directory_access_refused', DevelopmentBroker, alias, authorize_scope=self.authorized)


if __name__ == '__main__':
    unittest.main()
