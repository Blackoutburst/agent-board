"""Pure recipe guards/held-source tests; NEVER run a CLI or Linux qualification."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import qualify_linux_planner_policy as recipe
import launch_linux_planner_policy_qualification as launcher
from blackoutburst_agents.linux_planner_policy import LINUX_EXECUTION_SOURCES


class RecipeTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name).resolve()
        self.package=self.root/'package';self.package.mkdir(mode=0o700)
        self.files={}
        for name in recipe.ALLOWED_SOURCES:
            data=b'# synthetic public module; never executed\n'
            (self.package/name).write_bytes(data)
            self.files[name]={'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}
        self.inventory=self.root/'inventory.json'
        self.data=recipe.canonical({'schema':'blackoutburst-linux-policy-source-inventory-v1','files':self.files})
        self.inventory.write_bytes(self.data);self.expected=recipe.sha(self.data)

    def refuse(self,call):
        with self.assertRaises((recipe.Refused,ValueError,OSError)):call()

    def args(self):
        return recipe.parse_arguments(['--run-synthetic-policy-probe','--package',str(self.package),
            '--source-inventory',str(self.inventory),'--expected-source-inventory-sha256',self.expected,
            '--codex','/public/fake-codex','--expected-cli-sha256','0'*64,'--cli-version','codex-cli 0.163.0',
            '--interpreter',sys.executable,'--expected-interpreter-sha256','1'*64,
            '--expected-node','synthetic','--expected-release','synthetic','--expected-uid',str(os.geteuid()),
            '--root',str(self.root/'attempt'),'--output',str(self.root/'report.json')])

    def test_closed_roster_agrees_with_product_and_reads_only_held_public_bytes(self):
        self.assertEqual(recipe.ALLOWED_SOURCES,LINUX_EXECUTION_SOURCES)
        before=set(sys.modules)
        inventory,sources=recipe.load_sources(str(self.package),str(self.inventory),self.expected)
        self.assertEqual(inventory['files'],self.files)
        self.assertEqual(set(sources),set(LINUX_EXECUTION_SOURCES))
        self.assertFalse(any(name.startswith('blackoutburst_agents.') for name in set(sys.modules)-before))
        # Loading means holding data bytes here, never executing those modules.
        self.assertEqual(sources['__init__.py'],b'# synthetic public module; never executed\n')

    def test_wrong_inventory_hash_unknown_names_or_file_growth_refuse(self):
        self.refuse(lambda:recipe.load_sources(str(self.package),str(self.inventory),'0'*64))
        invalid={'schema':'blackoutburst-linux-policy-source-inventory-v1','files':{**self.files,'unknown.py':{'sha256':'0'*64,'bytes':0}}}
        self.inventory.write_bytes(recipe.canonical(invalid))
        self.refuse(lambda:recipe.load_sources(str(self.package),str(self.inventory),recipe.sha(self.inventory.read_bytes())))
        self.inventory.write_bytes(self.data);(self.package/'__init__.py').write_bytes(b'changed source')
        self.refuse(lambda:recipe.load_sources(str(self.package),str(self.inventory),self.expected))

    def test_symbolic_source_leaf_and_ancestor_are_never_followed(self):
        original=self.package/'__init__.py';original.rename(self.package/'held-original.py');original.symlink_to(self.package/'held-original.py')
        self.refuse(lambda:recipe.load_sources(str(self.package),str(self.inventory),self.expected))
        alias=self.root/'alias';alias.symlink_to(self.package)
        self.refuse(lambda:recipe.load_sources(str(alias),str(self.inventory),self.expected))

    def test_direct_or_instrumented_launch_refuses_before_linux_or_any_cli(self):
        args=self.args()
        with patch.object(recipe,'VERIFIED_RUNNER_SHA256','0'*64,create=True),patch.object(recipe,'load_sources',side_effect=AssertionError('source access')):
            self.refuse(lambda:recipe.real_guard(args))
        with patch.object(recipe,'load_sources',side_effect=AssertionError('source access')):
            self.refuse(lambda:recipe.real_guard(args))

    def test_report_is_exclusive_and_never_overwrites_existing_data(self):
        path=self.root/'report.json';value={'result':'refused','cleanup_confirmed':False}
        recipe.write_report(str(path),value)
        self.assertEqual(json.loads(path.read_bytes()),value)
        self.refuse(lambda:recipe.write_report(str(path),{'result':'unexpected'}))
        self.assertEqual(json.loads(path.read_bytes()),value)

    def test_launcher_holds_exact_bytes_and_refuses_wrong_hash_before_execution(self):
        runner=self.root/'qualify_linux_planner_policy.py'
        runner.write_text('raise RuntimeError("must never execute on pin failure")\n')
        self.refuse(lambda:launcher.main([str(runner),'0'*64,'--']))
        held=launcher.held_public(str(runner),96*1024)
        self.assertEqual(held,runner.read_bytes())
        runner.unlink();runner.symlink_to(self.inventory)
        self.refuse(lambda:launcher.held_public(str(runner),96*1024))

    def test_no_fake_linux_success_receipt_is_emitted_by_guard_tests(self):
        args=self.args();args.expected_uid='1'
        self.refuse(lambda:recipe.real_guard(args))
        self.assertFalse((self.root/'report.json').exists())
        self.assertFalse((self.root/'attempt').exists())
