"""Private storage invariants, independent of network and model calls."""
import hashlib
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

from blackoutburst_agents.context_cache import ContextCache, ContextCacheError, canonical


def artifact(ticket='ticket', text='PRIVATE_CONTEXT', owner='owner'):
    value = {'schema': 'blackoutburst-ticket-context-v1', 'trust': 'untrusted data',
             'scope': {'owner_id': owner, 'agent_id': 'agent', 'project_id': 'project', 'ticket_id': ticket},
             'sources': {}, 'data': {'body': text}, 'omissions': []}
    value['fingerprint'] = hashlib.sha256(canonical(value).encode()).hexdigest()
    return value


class ContextCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'contexts'

    def test_durable_idempotent_revisions_and_scope_isolation(self):
        with ContextCache(self.path) as cache:
            first = cache.save(artifact())
            self.assertEqual(first['revision'], 1)
            self.assertFalse(cache.save(artifact())['changed'])
            changed = cache.save(artifact(text='NEW'))
            self.assertEqual(changed['revision'], 2)
            other = cache.save(artifact(ticket='other'))
            owner = cache.save(artifact(owner='other-owner'))
            self.assertEqual(len({first['context_key'], other['context_key'], owner['context_key']}), 3)
        with ContextCache(self.path) as cache:
            self.assertEqual(cache.get(first['context_key'], revision=1)['data']['body'], 'PRIVATE_CONTEXT')
            self.assertEqual(cache.get(first['context_key'])['data']['body'], 'NEW')
            with self.assertRaisesRegex(ContextCacheError, 'context_fingerprint_changed'):
                cache.get(first['context_key'], fingerprint=first['fingerprint'])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.path/'contexts.sqlite3').stat().st_mode), 0o600)

    def test_forget_removes_only_one_scope_and_requires_current_fingerprint(self):
        with ContextCache(self.path) as cache:
            a = cache.save(artifact())
            b = cache.save(artifact(ticket='other'))
            c = cache.save(artifact(text='NEW'))
            with self.assertRaisesRegex(ContextCacheError, 'context_fingerprint_changed'):
                cache.forget(a['context_key'], a['fingerprint'])
            cache.forget(c['context_key'], c['fingerprint'])
            for revision in (None, 1, 2):
                with self.assertRaisesRegex(ContextCacheError, 'context_not_found'):
                    cache.get(a['context_key'], revision=revision)
            self.assertEqual(cache.get(b['context_key'])['scope']['ticket_id'], 'other')

    def test_retains_ten_revisions_only(self):
        with ContextCache(self.path) as cache:
            for i in range(15):
                saved = cache.save(artifact(text=str(i)))
            self.assertEqual(saved['revision'], 15)
            self.assertEqual(cache.db.execute('SELECT count(*) FROM snapshots').fetchone()[0], 10)
            with self.assertRaisesRegex(ContextCacheError, 'context_not_found'):
                cache.get(saved['context_key'], revision=5)
            self.assertEqual(cache.get(saved['context_key'], revision=6)['data']['body'], '5')

    def test_corruption_and_oversized_artifacts_fail(self):
        with ContextCache(self.path) as cache:
            changed = artifact()
            changed['data']['body'] = 'ALTERED'
            for bad in (changed, artifact(text='é'*48000)):
                with self.assertRaisesRegex(ContextCacheError, 'invalid_context_artifact'):
                    cache.save(bad)
            saved = cache.save(artifact())
            cache.db.execute("UPDATE snapshots SET artifact_json='{}'")
            with self.assertRaisesRegex(ContextCacheError, 'invalid_context_artifact'):
                cache.get(saved['context_key'])

    def test_other_scopes_cannot_be_smuggled_under_saved_key(self):
        with ContextCache(self.path) as cache:
            saved = cache.save(artifact())
            bad = artifact(ticket='other')
            cache.db.execute('UPDATE snapshots SET artifact_json=?,fingerprint=?',
                             (canonical(bad), bad['fingerprint']))
            with self.assertRaisesRegex(ContextCacheError, 'invalid_context_artifact'):
                cache.get(saved['context_key'])

    def test_rejects_insecure_symlinked_files_and_future_schema(self):
        self.path.mkdir(mode=0o755)
        with self.assertRaisesRegex(ContextCacheError, 'insecure_context_directory'):
            ContextCache(self.path)
        self.path.chmod(0o700)
        target = self.path/'other'
        target.write_text('DO_NOT_TOUCH')
        (self.path/'contexts.sqlite3').symlink_to(target)
        with self.assertRaises(ContextCacheError):
            ContextCache(self.path)
        self.assertEqual(target.read_text(), 'DO_NOT_TOUCH')
        (self.path/'contexts.sqlite3').unlink()
        with ContextCache(self.path) as cache:
            cache.db.execute('PRAGMA user_version=99')
        with self.assertRaisesRegex(ContextCacheError, 'unsupported_context_version'):
            ContextCache(self.path)
        with sqlite3.connect(self.path/'contexts.sqlite3') as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 99)

    def test_concurrent_handles_share_transactional_revision(self):
        with ContextCache(self.path) as first, ContextCache(self.path) as second:
            a = first.save(artifact())
            self.assertFalse(second.save(artifact())['changed'])
            b = second.save(artifact(text='changed'))
            self.assertEqual(b['revision'], 2)
            with self.assertRaisesRegex(ContextCacheError, 'context_fingerprint_changed'):
                first.forget(a['context_key'], a['fingerprint'])
