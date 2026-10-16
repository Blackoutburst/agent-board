"""Controller-owned planner policy: fake qualification, no model/subprocess."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from blackoutburst_agents.codex_turn import _arguments, execute_turn
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.planner_policy import (CHECKS, PlannerPolicy, PlannerPolicyError,
    permission_arguments, prepare_planner_policy)
from blackoutburst_agents.subscription_probe import DEFAULT_DISABLED_FEATURES, ProbeSettings
import test_codex_turn as codex_turn_fixtures


@unittest.skipIf(os.geteuid() == 0, 'Native planner policies refuse root controllers')
class PlannerPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='planner-policy-unit-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace, self.checkout, self.state = (self.root / n for n in ('planner','checkout','state'))
        self.workspace.mkdir(mode=0o700);self.checkout.mkdir(mode=0o700)
        tools = self.root / 'tools';tools.mkdir(mode=0o700)
        self.cli = tools / 'fake-codex'
        self.cli.write_text('synthetic unexecuted CLI');self.cli.chmod(0o700)
        self.interpreter = tools / 'fake-python'
        self.interpreter.write_text('synthetic unexecuted interpreter');self.interpreter.chmod(0o700)
        self.version = 'codex-cli 0.162.0-alpha.17.2'
        self.host = patch('blackoutburst_agents.planner_policy._host', return_value={
            'system':'Darwin','release':'synthetic','machine':'synthetic'})
        self.host.start();self.addCleanup(self.host.stop)
        listener = SimpleNamespace(bind=lambda *_:None, listen=lambda *_:None,
                                   getsockname=lambda:('127.0.0.1',31415), close=lambda:None)
        sockets = patch('blackoutburst_agents.planner_policy.socket.socket', return_value=listener)
        sockets.start();self.addCleanup(sockets.stop)
        self.commands = []

    def prepare(self):
        return prepare_planner_policy(self.state, workspace=self.workspace, checkout=self.checkout,
            executable=self.cli, interpreter=self.interpreter, cli_version=self.version)

    def fake_run(self, argv, cwd, timeout, maximum, env):
        self.commands.append((argv,cwd,dict(env)))
        output = self.version.encode() if argv == [str(self.cli),'--version'] else json.dumps({
            'schema':1,'checks':{name:True for name in CHECKS}}).encode()
        return SimpleNamespace(failure=None,returncode=0,stdout=output,stderr=b'')

    @contextmanager
    def qualified(self):
        fingerprint = self.prepare()
        with PlannerPolicy(self.state) as policy:
            with patch('blackoutburst_agents.planner_policy._run_bounded', side_effect=self.fake_run):
                receipt = policy.qualify()
            self.assertEqual(receipt['fingerprint'],fingerprint)
            yield policy

    def test_default_is_unqualified_and_preparation_never_launches_anything(self):
        with patch('blackoutburst_agents.planner_policy._run_bounded') as run:
            self.prepare()
            with PlannerPolicy(self.state) as policy:
                with self.assertRaises(PlannerPolicyError) as error:
                    policy.validate_for(self.cli,self.workspace)
            self.assertEqual(error.exception.code,'planner_policy_unqualified')
            run.assert_not_called()

    def test_exact_default_selector_is_qualified_without_exec_login_or_named_profile_flag(self):
        with self.qualified() as policy:
            self.assertEqual(policy.validate_for(self.cli,self.workspace),permission_arguments(str(self.workspace)))
            self.assertEqual(policy.workspace_identity,tuple(policy._plan['workspace_identity']))
            self.assertEqual(len(self.commands),2)
            argv,cwd,env = self.commands[1]
            self.assertIn('sandbox',argv);self.assertNotIn('-P',argv);self.assertNotIn('--sandbox',argv)
            self.assertIn('default_permissions="board-planner-read-v1"',argv)
            self.assertNotIn('exec',argv);self.assertNotIn('login',argv)
            self.assertEqual(cwd,str(self.workspace));self.assertEqual(list(self.workspace.iterdir()),[])
            self.assertFalse(any(p.name.startswith('outside-canary') for p in self.state.iterdir()))
            self.assertNotIn('OPENAI_API_KEY',env)
        with PlannerPolicy(self.state) as reopened:
            reopened.validate_for(self.cli,self.workspace)

    def test_nonempty_alias_and_checkout_overlap_are_refused(self):
        (self.workspace/'AGENTS.md').write_text('ordinary fixture')
        with self.assertRaises(PlannerPolicyError):self.prepare()
        (self.workspace/'AGENTS.md').unlink()
        with self.assertRaises(PlannerPolicyError):
            prepare_planner_policy(self.state,workspace=self.workspace,checkout=self.workspace,
                executable=self.cli,interpreter=self.interpreter,cli_version=self.version)
        alias = self.root/'alias';alias.symlink_to(self.workspace,target_is_directory=True)
        with self.assertRaises(DevelopmentError):
            prepare_planner_policy(self.state,workspace=alias,checkout=self.checkout,
                executable=self.cli,interpreter=self.interpreter,cli_version=self.version)

    def test_changed_cli_or_workspace_identity_cannot_use_prior_qualification(self):
        with self.qualified() as policy:
            self.cli.write_text('changed synthetic CLI')
            with self.assertRaises(PlannerPolicyError):policy.validate_for(self.cli,self.workspace)
        self.cli.write_text('synthetic unexecuted CLI')
        with PlannerPolicy(self.state) as policy:
            self.workspace.rename(self.root/'old-planner');self.workspace.mkdir(mode=0o700)
            with self.assertRaises(PlannerPolicyError):policy.validate_for(self.cli,self.workspace)

    def test_file_inserted_after_qualification_blocks_transport_before_doctor_or_model(self):
        with self.qualified() as policy:
            (self.workspace/'ordinary.txt').write_text('new data')
            with patch('blackoutburst_agents.codex_turn.inspect_cli') as doctor, patch(
                    'blackoutburst_agents.codex_turn.subprocess.Popen') as launch:
                result = execute_turn(ProbeSettings(self.cli),'private prompt',self.workspace,planner_policy=policy)
            self.assertEqual(result['reason'],'planner_policy_unqualified_or_changed')
            doctor.assert_not_called();launch.assert_not_called()

    def test_guard_failure_is_never_qualified_and_does_not_fall_back(self):
        self.prepare()
        with PlannerPolicy(self.state) as policy:
            def fail(argv,*args):
                if argv == [str(self.cli),'--version']:return self.fake_run(argv,*args)
                return SimpleNamespace(failure=None,returncode=78,stdout=json.dumps({
                    'schema':1,'checks':{name:name!='outside_read_denied' for name in CHECKS}}).encode(),stderr=b'')
            with patch('blackoutburst_agents.planner_policy._run_bounded',side_effect=fail):
                with self.assertRaises(PlannerPolicyError):policy.qualify()
            with self.assertRaises(PlannerPolicyError):policy.validate_for(self.cli,self.workspace)
            self.assertEqual(list(self.workspace.iterdir()),[])

    def test_arguments_preserve_default_and_opt_in_disables_all_existing_surfaces(self):
        settings = ProbeSettings(self.cli)
        baseline = _arguments(settings,self.cli,None)
        self.assertEqual(baseline[:4],[str(self.cli),'exec','--sandbox','read-only'])
        with self.qualified() as policy:
            selected = _arguments(settings,self.cli,None,policy.validate_for(self.cli,self.workspace))
            self.assertNotIn('--sandbox',selected);self.assertNotIn('-P',selected)
            self.assertIn('--ignore-user-config',selected)
            for feature in DEFAULT_DISABLED_FEATURES:self.assertIn('features.'+feature+'=false',selected)
            for value in ('approval_policy="never"','forced_login_method="chatgpt"',
                          'model_provider="openai"','web_search="disabled"'):
                self.assertIn(value,selected)
            self.assertEqual(selected[-1],'-')

    def test_change_during_doctor_is_rechecked_before_model_launch(self):
        with self.qualified() as policy:
            def change(settings):
                (self.workspace/'ordinary.txt').write_text('changed during metadata')
                return {'ok':True,'authentication':'chatgpt','version':self.version}
            with patch('blackoutburst_agents.codex_turn.inspect_cli',side_effect=change), patch(
                    'blackoutburst_agents.codex_turn.subprocess.Popen') as launch:
                result = execute_turn(ProbeSettings(self.cli),'private prompt',self.workspace,planner_policy=policy)
            self.assertEqual(result['reason'],'planner_policy_unqualified_or_changed')
            launch.assert_not_called()

    def test_explicit_policy_cannot_be_used_for_a_different_scope_or_after_close(self):
        with self.qualified() as policy:
            with self.assertRaises(PlannerPolicyError):policy.validate_for(self.cli,self.checkout)
            with self.assertRaises(PlannerPolicyError):policy.validate_for(self.interpreter,self.workspace)
        with self.assertRaises(PlannerPolicyError):policy.validate_for(self.cli,self.workspace)

    def test_unknown_policy_objects_are_refused_before_any_client_call(self):
        with patch('blackoutburst_agents.codex_turn.inspect_cli') as doctor, patch(
                'blackoutburst_agents.codex_turn.subprocess.Popen') as launch:
            result = execute_turn(ProbeSettings(self.cli),'private prompt',self.workspace,planner_policy=object())
        self.assertEqual(result['reason'],'planner_policy_unqualified_or_changed')
        doctor.assert_not_called();launch.assert_not_called()

    def test_simulated_text_turn_receives_qualified_selector_and_exact_resume(self):
        self.directory = self.cli.parent
        capture = self.directory/'captured-arguments.json'
        self.cli = codex_turn_fixtures.CodexTurnTests.fake_cli(self, f"""
            with open({str(capture)!r},'w') as output:
                json.dump(args,output)
            success()
        """)
        with self.qualified() as policy:
            result = execute_turn(ProbeSettings(self.cli,timeout_seconds=2), 'private synthetic prompt',
                                  self.workspace, planner_policy=policy,
                                  resume_session_id='11111111-2222-4333-8444-555555555555')
        self.assertEqual(result['status'],'completed')
        arguments = json.loads(capture.read_text())
        self.assertNotIn('--sandbox',arguments)
        self.assertIn('default_permissions="board-planner-read-v1"',arguments)
        self.assertEqual(arguments[-2:],['11111111-2222-4333-8444-555555555555','-'])
        self.assertEqual(arguments.count('resume'),1)
        self.assertNotIn('private synthetic prompt',arguments)
        self.assertEqual(list(self.workspace.iterdir()),[])

    def test_corrupt_or_incomplete_receipt_never_authorizes_a_turn(self):
        with self.qualified() as policy:
            receipt = self.state/'planner-qualification.json'
            for value in (None, {'schema':1,'fingerprint':policy.fingerprint,'checks':{},'cleanup_confirmed':True},
                          {'schema':1,'fingerprint':policy.fingerprint,'checks':{k:1 for k in CHECKS},'cleanup_confirmed':True}):
                receipt.write_text(json.dumps(value))
                with self.assertRaises(PlannerPolicyError):policy.validate_for(self.cli,self.workspace)
