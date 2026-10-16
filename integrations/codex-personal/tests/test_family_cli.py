"""Pure preflight boundaries, including paths not created yet."""
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.dev_planner import _directory_pin
from blackoutburst_agents.family_cli import _directory_outside,_preflight,_resume_preflight,run


class FamilyCliBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name).resolve()
        self.root=self.base/'checkout';self.root.mkdir(mode=0o700)
        self.roots={'parent':_directory_pin(self.root)}

    def test_missing_private_directory_inside_checkout_is_rejected_without_creation(self):
        path=self.root/'new'/'runs'
        with self.assertRaises(DevelopmentError):_directory_outside(path,self.roots)
        self.assertFalse((self.root/'new').exists())

    def test_existing_root_and_its_ancestor_are_rejected(self):
        for path in (self.root,self.base):
            with self.subTest(path=path),self.assertRaises(DevelopmentError):
                _directory_outside(path,self.roots)

    def test_absent_sibling_with_common_parent_is_allowed_without_creation(self):
        path=self.base/'private'/'runs'
        _directory_outside(path,self.roots)
        self.assertFalse((self.base/'private').exists())

    def test_existing_sibling_is_allowed(self):
        path=self.base/'private';path.mkdir(mode=0o700)
        _directory_outside(path,self.roots)

    def test_symlink_and_traversal_are_refused(self):
        link=self.base/'alias';link.symlink_to(self.root,target_is_directory=True)
        for path in (link,link/'new',self.base/'missing'/'..'/'new'):
            with self.subTest(path=path),self.assertRaises(DevelopmentError):
                _directory_outside(path,self.roots)


class FamilyCliExistingStateTests(unittest.TestCase):
    def fixture(self):
        from test_family_worker import FamilyWorkerTests
        fixture=FamilyWorkerTests('test_prepare_reserves_three_roots_but_opens_no_remote_family_or_runtime')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        return fixture

    def test_valid_prepare_and_resume_preflight_have_no_filesystem_effect(self):
        from blackoutburst_agents.family_journal import SLOTS
        fixture=self.fixture()
        spec={'schema':1,'request':fixture.request,'workflows':{slot:{
            'journal':str(fixture.workflows[slot].journal.directory),
            'runs':str(fixture.workflows[slot].store.state_dir),
            'runtime':str(fixture.workflows[slot].runtime.directory),
            'registration':fixture.registrations[slot]} for slot in SLOTS}}
        args=SimpleNamespace(journal=fixture.base/'new-family',config_file=fixture.base/'plan.json',
                             connection_file=fixture.base/'connection.json')
        before=sorted(str(path.relative_to(fixture.base)) for path in fixture.base.rglob('*'))
        self.assertEqual(len(_preflight(args,spec)),3)
        self.assertEqual(before,sorted(str(path.relative_to(fixture.base)) for path in fixture.base.rglob('*')))
        fixture.worker.prepare(fixture.request)
        args.journal=fixture.journal.directory
        before=sorted(str(path.relative_to(fixture.base)) for path in fixture.base.rglob('*'))
        _resume_preflight(args,fixture.journal.read())
        self.assertEqual(before,sorted(str(path.relative_to(fixture.base)) for path in fixture.base.rglob('*')))

    def test_offline_status_and_stop_do_not_construct_journal_fence_or_stores(self):
        from blackoutburst_agents.family_journal import FamilyJournal
        fixture=self.fixture();fixture.worker.prepare(fixture.request)
        for command in ('status','stop-local'):
            with self.subTest(command=command),patch.object(FamilyJournal,'__init__',side_effect=AssertionError('No constructor')),redirect_stdout(StringIO()) as output:
                code=run(SimpleNamespace(journal=fixture.journal.directory,family_command=command))
            result=json.loads(output.getvalue());self.assertEqual(code,0)
            self.assertEqual(result['stop_requested'],command=='stop-local')
