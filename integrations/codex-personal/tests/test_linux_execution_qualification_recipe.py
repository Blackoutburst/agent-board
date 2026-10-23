"""Recipe refusal/held-byte tests only, with no Linux/backend/CLI invocation."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import qualify_linux_execution_backend as recipe
import launch_linux_execution_backend_qualification as launcher
from blackoutburst_agents.linux_planner_policy import LINUX_EXECUTION_SOURCES


class RecipeTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name).resolve();self.package=self.root/'package';self.package.mkdir(mode=0o700)
        self.files={}
        for name in recipe.ALLOWED_SOURCES:
            data=b'# explicitly synthetic public fixture; never imported\n';(self.package/name).write_bytes(data)
            self.files[name]={'sha256':recipe.sha(data),'bytes':len(data)}
        self.inventory=self.root/'inventory.json';self.raw=recipe.canonical({'schema':'blackoutburst-linux-policy-source-inventory-v1','files':self.files})
        self.inventory.write_bytes(self.raw);self.expected=recipe.sha(self.raw)

    def args(self):
        return recipe.parse_arguments(['--run-synthetic-backend-matrix','--package',str(self.package),
            '--source-inventory',str(self.inventory),'--expected-source-inventory-sha256',self.expected,
            '--interpreter',sys.executable,'--expected-interpreter-sha256','0'*64,'--unshare','/synthetic/unshare',
            '--expected-unshare-sha256','1'*64,'--expected-node','synthetic','--expected-release','synthetic',
            '--expected-uid','1000','--root',str(self.root/'new'),'--output',str(self.root/'report.json')])

    def test_inventory_requires_exact_full_roster_and_retains_bytes_without_import(self):
        self.assertEqual(len(recipe.ALLOWED_SOURCES),84)
        self.assertEqual(recipe.ALLOWED_SOURCES,tuple(sorted(set(recipe.ALLOWED_SOURCES))))
        # The held-byte launcher and both concrete execution policies must pin
        # the exact closed import graph, including the separate command helper.
        self.assertEqual(LINUX_EXECUTION_SOURCES,recipe.ALLOWED_SOURCES)
        self.assertIn("git_publication_guardian.py",recipe.ALLOWED_SOURCES)
        before=set(sys.modules)
        inventory,sources=recipe.load_sources(str(self.package),str(self.inventory),self.expected)
        self.assertEqual(inventory['files'],self.files);self.assertEqual(set(sources),set(recipe.ALLOWED_SOURCES))
        self.assertEqual(set(sys.modules),before)

    def test_direct_or_mocked_run_refuses_before_reading_sources_or_creating_fixtures(self):
        args=self.args()
        with patch.object(recipe,'load_sources',side_effect=AssertionError('no source or native action')):
            with self.assertRaises(recipe.Refused):recipe.real_guard(args)
        with patch.object(recipe,'VERIFIED_RUNNER_SHA256','a'*64,create=True):
            with self.assertRaises(recipe.Refused):recipe.real_guard(args)
        self.assertFalse((self.root/'new').exists());self.assertFalse((self.root/'report.json').exists())

    def test_wrong_held_runner_hash_fails_before_compilation(self):
        runner=self.root/'qualify_linux_execution_backend.py';runner.write_text('raise RuntimeError("never executed")\n')
        with self.assertRaises(ValueError):launcher.main([str(runner),'0'*64,'--'])
        self.assertEqual(launcher.held_public(str(runner),96000),runner.read_bytes())

    def test_fixture_and_report_paths_are_exclusive_and_unknown_files_are_retained(self):
        root=recipe.fixture_root(str(self.root/'new'));foreign=root/'unknown';foreign.write_text('unowned synthetic fixture')
        with self.assertRaises(FileExistsError):recipe.fixture_root(str(root))
        self.assertEqual(foreign.read_text(),'unowned synthetic fixture')
        report=self.root/'report.json';recipe.write_report(str(report),{'result':'refused','cleanup_confirmed':False})
        with self.assertRaises(FileExistsError):recipe.write_report(str(report),{'result':'unexpected'})
        self.assertEqual(recipe.decode(report.read_bytes())['result'],'refused')

    def test_symlink_source_ancestor_and_wrong_inventory_digest_are_refused(self):
        alias=self.root/'alias';alias.symlink_to(self.package)
        for path,pin in ((alias,self.expected),(self.package,'0'*64)):
            with self.assertRaises((recipe.Refused,OSError)):recipe.load_sources(str(path),str(self.inventory),pin)

    def test_no_implicit_cli_account_or_service_arguments_exist(self):
        args=self.args();self.assertFalse(hasattr(args,'codex'));self.assertFalse(hasattr(args,'home'))
        self.assertFalse(hasattr(args,'account'));self.assertFalse(hasattr(args,'service'))
        self.assertTrue(args.run_synthetic_backend_matrix)
