"""Private metadata only: no Board, model, command or native sandbox."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest

from blackoutburst_agents.family_journal import FamilyJournal,SLOTS
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.dev_planner import _directory_pin


class FamilyJournalTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temp.cleanup);self.base=Path(temp.name).resolve()
        roots={};stores={}
        for slot in SLOTS:
            root=self.base/slot;root.mkdir(mode=0o700);roots[slot]=_directory_pin(root)
            stores[slot]={}
            for kind in ('runs','runtime'):
                path=self.base/(slot+'-'+kind);path.mkdir(mode=0o700);stores[slot][kind]=_directory_pin(path)
        self.config={'scope':dict(owner_id='owner',agent_id='agent',project_id='project',ticket_id='ticket',
            board_endpoint='https://board.test/mcp',board_session_id='root'),'roots':roots,'child_stores':stores}
        self.config['provisional_scopes']={slot:{**self.config['scope'],'board_session_id':self.config['scope']['board_session_id'] if slot=='parent' else slot} for slot in SLOTS}
        self.config['pins']={}
        for slot in SLOTS:
            self.config['pins'][slot]={}
            for kind in ('journal','broker_directory'):
                path=self.base/(slot+'-'+kind);path.mkdir(mode=0o700)
                self.config['pins'][slot][kind]=_directory_pin(path)
        self.journal=FamilyJournal(self.base/'family',_test_anchor=self.base/'anchor')
        self.addCleanup(self.journal.close)

    def test_prepare_allocates_ids_and_exact_reopen_cannot_rekey(self):
        state=self.journal.prepare(self.config)
        self.assertTrue(state['fence_ready']);self.assertEqual(len(set(state['workflow_ids'].values())),3)
        again=self.journal.prepare(self.config);self.assertEqual(again,state)
        other=FamilyJournal(self.base/'different-journal',_test_anchor=self.base/'anchor');self.addCleanup(other.close)
        with self.assertRaises(DevelopmentError):other.prepare(self.config)
        self.journal.verify(state)

    def test_stop_does_not_require_active_execution_lock(self):
        state=self.journal.prepare(self.config)
        with self.journal.lock():
            self.assertFalse(self.journal.stopped());self.assertTrue(self.journal.request_stop())
            self.assertTrue(self.journal.stopped())
            self.assertEqual(self.journal.read(),state)
        self.assertTrue(self.journal.stopped())

    def test_save_requires_lock_and_cas_and_never_replaces_configuration(self):
        state=self.journal.prepare(self.config)
        with self.assertRaises(DevelopmentError):self.journal.save(state)
        with self.journal.lock():
            previous=deepcopy(state);state['phase']='opening';self.journal.save(state)
            with self.assertRaises(DevelopmentError):self.journal.save(previous)
            changed=deepcopy(state);changed['configuration']['scope']['owner_id']='other'
            with self.assertRaises(DevelopmentError):self.journal.save(changed)
        self.assertEqual(self.journal.read(),state)

    def test_bound_is_checked_before_atomic_write(self):
        state=self.journal.prepare(self.config)
        with self.journal.lock():
            state['payload']['oversized']='a'*(2*1024*1024)
            with self.assertRaises(DevelopmentError):self.journal.save(state)
        self.assertNotIn('oversized',self.journal.read()['payload'])

    def test_changed_directory_or_overlap_refuses_without_adoption(self):
        state=self.journal.prepare(self.config)
        root=Path(self.config['roots']['child_0']['path']);root.rename(self.base/'old-root');root.mkdir(mode=0o700)
        with self.assertRaises(DevelopmentError):self.journal.verify(state)
        other=deepcopy(self.config);other['roots']['child_0']=_directory_pin(root)
        with self.assertRaises(DevelopmentError):self.journal.prepare(other)

    def test_nested_roots_refuse_before_journal_creation(self):
        config=deepcopy(self.config);nested=Path(config['roots']['parent']['path'])/'nested';nested.mkdir(mode=0o700)
        config['roots']['child_0']=_directory_pin(nested)
        with self.assertRaises(DevelopmentError):self.journal.prepare(config)
        self.assertFalse((self.journal.directory/'family.json').exists())

    def test_symlink_marker_and_lock_are_refused(self):
        self.journal.prepare(self.config)
        (self.base/'foreign').write_text('ordinary')
        (self.journal.directory/'stop.json').symlink_to(self.base/'foreign')
        with self.assertRaises(DevelopmentError):self.journal.stopped()
        self.assertEqual((self.base/'foreign').read_text(),'ordinary')


if __name__=='__main__':unittest.main()
