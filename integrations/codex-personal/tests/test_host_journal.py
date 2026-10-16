"""Private host controls and owner-wide locking, without network or models."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from blackoutburst_agents.host_config import HostError, validate, load, connection_digest
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.cli import main


def configuration(root):
    connection = root/'connection.json'
    connection.write_text(json.dumps({'mcp_url':'https://board.test/mcp','token':'test-secret',
        'client':'codex','identity_key':'fixture','name':'Fixture'}))
    connection.chmod(0o600)
    return {'schema_version':1,'owner_id':'owner','board_endpoint':'https://board.test/mcp',
        'interval_seconds':5,'max_pages':1,'page_limit':50,'dispatch_budget':1,'targets':[
        {'id':'primary','agent_id':'agent','project_id':'project','ticket_id':'ticket','board_session_id':'session',
         'profile_version':1,'connection_file':str(connection),'instruction':'PRIVATE_INSTRUCTION',
         'workspace':str(root),'executable':str(Path(sys.executable).resolve()),'model':'fixture-model','timeout':10,'after':0}]}


class HostJournalTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve()
        self.config=validate(configuration(self.root),bind_connections=True)
        self.host=HostJournal(self.root/'host');self.addCleanup(self.host.close)
        self.host.configure(self.config)
        self.locks=self.root/'locks'

    def locked(self): return self.host.owner_lock(lock_directory=self.locks)

    def test_default_disabled_and_configure_is_idempotent_private_metadata(self):
        status=self.host.status()
        self.assertEqual(status['status'],'disabled')
        self.assertEqual(status['remaining_dispatches'],0)
        self.assertEqual(self.host.configure(self.config),status)
        self.assertNotIn('PRIVATE',json.dumps(status));self.assertNotIn('test-secret',json.dumps(status))
        self.assertEqual((self.root/'host').stat().st_mode&0o777,0o700)
        self.assertEqual((self.root/'host/host.sqlite3').stat().st_mode&0o777,0o600)

    def test_config_conflict_and_bounds(self):
        for key,value in [('dispatch_budget',21),('interval_seconds',0),('max_pages',5),('page_limit',True)]:
            invalid=deepcopy(self.config);invalid[key]=value
            with self.assertRaises(HostError):validate(invalid)
        modified=deepcopy(self.config);modified['dispatch_budget']=2
        with self.assertRaises(HostError):self.host.configure(modified)
        for key,value in [('model',None),('profile_version',False),('token','forbidden')]:
            invalid=deepcopy(self.config);invalid['targets'][0][key]=value
            with self.assertRaises(HostError):validate(invalid)

    def test_connection_path_private_and_hash_is_pinned(self):
        target=self.config['targets'][0]
        self.assertEqual(target['connection_sha256'],connection_digest(target['connection_file']))
        Path(target['connection_file']).chmod(0o644)
        with self.assertRaises(HostError):connection_digest(target['connection_file'])

    def test_duplicate_targets_and_different_profile_session_refused(self):
        invalid=deepcopy(self.config);invalid['targets'].append(deepcopy(invalid['targets'][0]))
        with self.assertRaises(HostError):validate(invalid)
        invalid['targets'][1].update(id='second',ticket_id='second',board_session_id='another')
        with self.assertRaises(HostError):validate(invalid)

    def test_enable_cas_stop_and_budget_replay(self):
        with self.locked():
            with self.assertRaises(HostError):self.host.enable(0)
            result=self.host.enable(1)
            batch={'id':'batch','request_id':'request','subscription_id':'subscription'}
            a=self.host.reserve_dispatch('primary',batch,result['activation_id'])
            self.assertEqual(self.host.reserve_dispatch('primary',batch,result['activation_id']),a)
            self.assertEqual(self.host.status()['remaining_dispatches'],0)
            self.host.link_worker('batch',a['worker_id']);self.host.stop()
            with self.assertRaises(HostError):self.host.reserve_dispatch('primary',batch,result['activation_id'])
        self.assertEqual(self.host.status()['status'],'disabled')

    def test_dispatch_id_is_preallocated_and_cannot_be_relinked(self):
        with self.locked():
            activation=self.host.enable(1)['activation_id']
            dispatch=self.host.reserve_dispatch('primary',
                {'id':'batch','request_id':'request','subscription_id':'subscription'},activation)
            self.assertEqual(str(uuid.UUID(dispatch['worker_id'])),dispatch['worker_id'])
            with self.assertRaises(HostError):
                self.host.link_worker('batch',str(uuid.uuid4()))
            self.assertEqual(self.host.dispatches()[0],dispatch)

    def test_legacy_empty_dispatch_id_is_refused_without_adoption(self):
        with self.locked():
            activation=self.host.enable(1)['activation_id']
            self.host.reserve_dispatch('primary',
                {'id':'batch','request_id':'request','subscription_id':'subscription'},activation)
        with sqlite3.connect(self.root/'host/host.sqlite3') as db:
            db.execute("UPDATE dispatches SET worker_id=''")
        with self.assertRaises(HostError) as error:
            self.host.status()
        self.assertEqual(error.exception.code,'host_corrupt')

    def test_owner_lock_cannot_be_bypassed_with_other_journal(self):
        other=HostJournal(self.root/'other');self.addCleanup(other.close);other.configure(self.config)
        with self.locked():
            with self.assertRaises(HostError) as error:
                with other.owner_lock(lock_directory=self.locks):pass
            self.assertEqual(error.exception.code,'host_busy')
        with self.assertRaises(HostError) as error:
            with other.owner_lock(lock_directory=self.locks):pass
        self.assertEqual(error.exception.code,'host_directory_conflict')

    def test_stop_works_from_second_connection_while_owner_lock_held(self):
        other=HostJournal(self.root/'host');self.addCleanup(other.close)
        with self.locked():
            active=self.host.enable(1)['activation_id'];other.stop()
            self.assertTrue(self.host.cancelled(active));other.stop()
            self.assertEqual(self.host.status()['revision'],3)

    def test_profile_pin_cannot_be_changed(self):
        with self.locked():
            self.host.pin_profile('primary','abc');self.host.pin_profile('primary','abc')
            with self.assertRaises(HostError):self.host.pin_profile('primary','def')

    def test_future_corrupt_and_symlink_refused(self):
        self.host.close()
        db=sqlite3.connect(self.root/'host/host.sqlite3');db.execute('PRAGMA user_version=99');db.close()
        with self.assertRaises(HostError):HostJournal(self.root/'host')
        link=self.root/'link';link.symlink_to(self.root/'host')
        with self.assertRaises(HostError):HostJournal(link)
        other=self.root/'insecure';other.mkdir(mode=0o755)
        with self.assertRaises(HostError):HostJournal(other)

    def test_cli_status_stop_offline_and_no_secrets(self):
        for command in ('status','stop','tick'):
            output=io.StringIO()
            with patch('blackoutburst_agents.host_journal.canonical_directory',return_value=self.locks), \
                    patch('blackoutburst_agents.host_supervisor.default_resources') as resources,redirect_stdout(output):
                code=main(['host','--journal',str(self.root/'host'),command])
            self.assertEqual(code,0);resources.assert_not_called()
            self.assertNotIn('PRIVATE',output.getvalue());self.assertNotIn(str(self.root),output.getvalue())

    def test_config_file_rejects_symlink_duplicate_keys_and_insecure_mode(self):
        path=self.root/'config.json';path.write_text(json.dumps(configuration(self.root)));path.chmod(0o600)
        self.assertEqual(load(path)['owner_id'],'owner')
        link=self.root/'config-link';link.symlink_to(path)
        with self.assertRaises(HostError):load(link)
        path.write_text('{"owner_id":"a","owner_id":"b"}')
        with self.assertRaises(HostError):load(path)
        path.chmod(0o644)
        with self.assertRaises(HostError):load(path)

    def test_canonical_root_uses_os_account_not_path_home(self):
        from blackoutburst_agents.host_journal import canonical_directory
        with patch('blackoutburst_agents.host_journal.pwd.getpwuid',return_value=SimpleNamespace(pw_dir='/fixed-os-user')), \
                patch('pathlib.Path.home',return_value=Path('/different-launcher-home')):
            self.assertEqual(canonical_directory(),Path('/fixed-os-user/.local/state/blackoutburst-agents/hosts'))

    def test_second_os_process_cannot_hold_owner_lock(self):
        code = """from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.host_config import HostError
import sys
with HostJournal(sys.argv[1]) as host:
    try:
        with host.owner_lock(lock_directory=sys.argv[2]): pass
    except HostError as error:
        sys.exit(75 if error.code=='host_busy' else 76)
"""
        with self.locked():
            result=subprocess.run([sys.executable,'-c',code,str(self.root/'host'),str(self.locks)],
                                  capture_output=True,timeout=10)
        self.assertEqual(result.returncode,75,result.stderr.decode())
        self.assertEqual(result.stdout,b'')

    def test_routing_policy_owner_is_bound_before_any_network(self):
        from test_model_policy import source
        from blackoutburst_agents.model_policy import routing_configuration
        config=deepcopy(self.config)
        config['targets'][0]['model']=None
        config['targets'][0]['routing']=routing_configuration(source(),'simple',owner_id='owner-a')
        with self.assertRaises(HostError):validate(config,bind_connections=True)
        config['owner_id']='owner-a'
        result=validate(config,bind_connections=True)
        self.assertEqual(result['targets'][0]['routing']['policy']['owner_id'],'owner-a')
