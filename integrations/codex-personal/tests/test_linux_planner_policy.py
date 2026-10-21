"""Pure Linux-policy contracts, with ALL host/proc/CLI/socket observations mocked."""
import copy
import errno
import json
import os
from pathlib import Path
import pickle
import stat
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from blackoutburst_agents.codex_turn import _arguments
from blackoutburst_agents.dev_journal import decode
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical,digest
from blackoutburst_agents import linux_planner_policy as policy_module
from blackoutburst_agents.linux_planner_policy import (CHECKS,LinuxPlannerPolicy,PlannerPolicyError,
    linux_execution_identity,prepare_linux_planner_policy,validate_linux_execution_identity)
from blackoutburst_agents.subscription_probe import ProbeSettings


def environment():
    uid,gid=os.geteuid(),os.getegid()
    return {'schema':policy_module.ENVIRONMENT_SCHEMA,
        'host':{'system':'Linux','release':'synthetic-5.15','machine':'x86_64','node':'fixture',
                'kernel_version':'synthetic kernel','pointer_bits':64},
        'uid':uid,'euid':uid,'gid':gid,'egid':gid,'groups':sorted(os.getgroups()),'procfs_magic':0x9fa0,
        'cgroups':[{'hierarchy':0,'controllers':[],'path':'/synthetic-owner.service'}],
        'no_new_privs':1,'seccomp':2,'seccomp_filters':1,
        'capabilities':{name:0 for name in policy_module.CAP_NAMES},
        'label':{'state':'present','value':'synthetic-label'},
        'namespaces':{name:100+index for index,name in enumerate(policy_module.NS_NAMES)}}


@unittest.skipIf(os.geteuid()==0 or os.getegid()==0,'Policies reject privileged uid/gid')
class LinuxPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name).resolve()
        self.workspace=self.root/'planner';self.workspace.mkdir(mode=0o700)
        self.checkout=self.root/'checkout';self.checkout.mkdir(mode=0o700)
        self.state=self.root/'state';tools=self.root/'tools';tools.mkdir(mode=0o700)
        self.cli=tools/'fake-codex';self.cli.write_text('synthetic unexecuted CLI');self.cli.chmod(0o700)
        self.interpreter=tools/'fake-python';self.interpreter.write_text('synthetic unexecuted interpreter');self.interpreter.chmod(0o700)
        self.version='codex-cli 0.163.0';self.identity=environment();self.commands=[]
        # The integration module list is intentionally incomplete in this branch;
        # tests mock a synthetic pin projection, never invent product hashes.
        self.source_pins={'package':{'path':'/synthetic/public/package','identity':[1,2],'chain':[]},
            'files':{name:{'sha256':digest(name.encode()),'bytes':len(name)} for name in policy_module.LINUX_EXECUTION_SOURCES}}
        identities=patch.object(policy_module,'linux_execution_identity',side_effect=lambda:copy.deepcopy(self.identity))
        identities.start();self.addCleanup(identities.stop)
        sources=patch.object(policy_module,'linux_source_pins',side_effect=lambda:copy.deepcopy(self.source_pins))
        sources.start();self.addCleanup(sources.stop)
        listener=SimpleNamespace(bind=lambda *_:None,listen=lambda *_:None,getsockname=lambda:('127.0.0.1',31415),close=lambda:None)
        sockets=patch.object(policy_module.socket,'socket',return_value=listener);sockets.start();self.addCleanup(sockets.stop)
        self.run_patch=patch.object(policy_module,'_run_bounded',side_effect=self.fake_run)
        self.run=self.run_patch.start();self.addCleanup(self.run_patch.stop)

    def prepare(self,**changes):
        args=dict(workspace=self.workspace,checkout=self.checkout,executable=self.cli,interpreter=self.interpreter,
                  cli_version=self.version);args.update(changes)
        return prepare_linux_planner_policy(self.state,**args)

    def fake_run(self,argv,cwd,timeout,maximum,env):
        self.commands.append((argv,cwd,timeout,maximum,env))
        data=self.version.encode() if argv==[str(self.cli),'--version'] else canonical({'schema':1,'checks':{name:True for name in CHECKS}})
        return SimpleNamespace(failure=None,returncode=0,stdout=data,stderr=b'')

    def refuse(self,call,code=None):
        with self.assertRaises(DevelopmentError) as caught:call()
        if code:self.assertEqual(caught.exception.code,code)

    def test_new_preparation_never_runs_cli_and_default_has_no_qualification(self):
        fingerprint=self.prepare();self.run.assert_not_called()
        with LinuxPlannerPolicy(self.state) as policy:
            self.assertEqual(policy.fingerprint,fingerprint);self.assertEqual(policy.environment,self.identity)
            self.assertEqual(policy.environment_sha256,digest(canonical(self.identity)))
            self.refuse(lambda:policy.validate_for(self.cli,self.workspace),'linux_planner_policy_unqualified')
        self.run.assert_not_called()

    def test_positive_mock_qualification_uses_exact_default_selector_and_canaries(self):
        self.prepare()
        with LinuxPlannerPolicy(self.state) as policy:
            receipt=policy.qualify()
            arguments=policy.validate_for(self.cli,self.workspace)
            self.assertEqual(arguments,policy_module.permission_arguments(self.workspace,executable=self.cli))
            self.assertEqual(receipt['cleanup_confirmed'],True)
            self.assertEqual(receipt['checks'],{name:True for name in CHECKS})
            self.assertEqual(list(self.workspace.iterdir()),[])
            self.assertEqual(len(self.commands),2)
            command=self.commands[1][0]
            self.assertIn('sandbox',command);self.assertNotIn('--ignore-user-config',command)
            self.assertEqual(command[:1+len(arguments)],[str(self.cli),*arguments])
            self.assertEqual(command[1+len(arguments):5+len(arguments)],
                ['sandbox','--permission-profile',policy_module.PROFILE_NAME,'--include-managed-config'])
            self.assertIn('default_permissions="board-linux-planner-read-v1"',command)
            self.assertNotIn('-P',command);self.assertNotIn('exec',command);self.assertNotIn('login',command)
            self.assertEqual(self.commands[1][3],8192)
            self.assertNotIn('OPENAI_API_KEY',self.commands[1][4])
            self.refuse(policy.qualify,'linux_planner_attempt_not_reusable')
        with LinuxPlannerPolicy(self.state) as reopened:reopened.validate_for(self.cli,self.workspace)

    def test_profile_reads_only_the_exact_executable_and_workspace(self):
        public='/opt/blackoutburst-agent-host/releases/'+'7'*64+'/tools/codex'
        expected={':root':'deny',':minimal':'read',':tmpdir':'deny',':slash_tmp':'deny',
                  str(self.workspace):'read',public:'read'}
        self.assertEqual(policy_module.permission_document(self.workspace,executable=public),
                         {'filesystem':expected,'network':{'enabled':False}})
        arguments=policy_module.permission_arguments(self.workspace,executable=public)
        entries=','.join(json.dumps(key)+'='+json.dumps(value) for key,value in expected.items())
        self.assertEqual(arguments,['-c','default_permissions='+json.dumps(policy_module.PROFILE_NAME),
            '-c','permissions.'+policy_module.PROFILE_NAME+'={filesystem={'+entries+'},network={enabled=false}}'])
        self.prepare()
        plan=decode((self.state/'planner-plan.json').read_bytes())
        self.assertEqual(plan['policy'],policy_module.permission_document(self.workspace,executable=self.cli))
        self.assertEqual(plan['policy']['filesystem'][plan['executable_pin']['path']],'read')
        self.assertNotIn(str(self.cli.parent),plan['policy']['filesystem'])
        self.run.assert_not_called()

    def test_profile_requires_an_explicit_canonical_executable_path(self):
        for helper in (policy_module.permission_document,policy_module.permission_arguments):
            with self.assertRaises(TypeError):helper(self.workspace)
            for invalid in ('/',':minimal','tools/codex',str(self.cli)+'/../codex',str(self.cli)+'/',str(self.cli)+'\n'):
                with self.subTest(helper=helper.__name__,path=invalid):
                    self.refuse(lambda:helper(self.workspace,executable=invalid))
        quoted=str(self.cli)+'"quoted,network={enabled=true}'
        arguments=policy_module.permission_arguments(self.workspace,executable=quoted)
        self.assertIn(json.dumps(quoted)+'="read"',arguments[-1])
        self.assertTrue(arguments[-1].endswith(',network={enabled=false}}'))
        self.run.assert_not_called()

    def test_legacy_or_widened_profile_and_forged_executable_never_convert(self):
        for mutation in ('legacy','parent_read','extra_read','executable_directory','other_executable'):
            with self.subTest(mutation=mutation):
                self.setUp();self.prepare()
                path=self.state/'planner-plan.json';plan=decode(path.read_bytes())
                filesystem=plan['policy']['filesystem']
                if mutation=='legacy':filesystem.pop(str(self.cli))
                elif mutation=='parent_read':
                    filesystem.pop(str(self.cli));filesystem[str(self.cli.parent)]='read'
                elif mutation=='extra_read':filesystem[str(self.root)]='read'
                else:
                    replacement=self.cli.parent if mutation=='executable_directory' else self.interpreter
                    plan['executable']=str(replacement)
                    plan['policy']=policy_module.permission_document(self.workspace,executable=replacement)
                encoded=canonical(plan);path.write_bytes(encoded)
                self.refuse(lambda:LinuxPlannerPolicy(self.state),'linux_planner_policy_changed')
                self.assertEqual(path.read_bytes(),encoded)
                self.assertIsNone(decode((self.state/'planner-qualification.json').read_bytes()))
                self.run.assert_not_called()

    def test_directory_symlink_and_noncanonical_executable_refuse_preparation(self):
        link=self.cli.parent/'codex-link';link.symlink_to(self.cli)
        alias=self.root/'tool-alias';alias.symlink_to(self.cli.parent)
        for index,executable in enumerate((self.cli.parent,link,alias/self.cli.name,str(self.cli.parent)+'/../tools/'+self.cli.name)):
            with self.subTest(executable=executable):
                self.refuse(lambda:prepare_linux_planner_policy(self.root/('invalid-'+str(index)),
                    workspace=self.workspace,checkout=self.checkout,executable=executable,
                    interpreter=self.interpreter,cli_version=self.version))
        self.run.assert_not_called()

    def test_qualified_executable_scope_and_symlink_replacement_cannot_be_adopted(self):
        self.prepare()
        with LinuxPlannerPolicy(self.state) as policy:
            policy.qualify();self.run.reset_mock()
            self.refuse(lambda:policy.validate_for(self.interpreter,self.workspace),'linux_planner_policy_scope_mismatch')
            held=self.cli.with_name('held-codex');self.cli.rename(held);self.cli.symlink_to(held)
            self.refuse(lambda:policy.validate_for(self.cli,self.workspace),'linux_planner_policy_changed')
            self.run.assert_not_called()

    def test_exec_and_exact_resume_keep_policy_configuration_before_resume(self):
        self.prepare()
        with LinuxPlannerPolicy(self.state) as policy:
            policy.qualify();self.run.reset_mock()
            arguments=policy.validate_for(self.cli,self.workspace)
            for session in (None,'11111111-2222-4333-8444-555555555555'):
                with self.subTest(session=session):
                    command=_arguments(ProbeSettings(self.cli),self.cli,session,arguments)
                    prefix=[str(self.cli),'exec',*arguments]+(['resume'] if session else [])
                    self.assertEqual(command[:len(prefix)],prefix)
                    self.assertEqual(command[len(prefix)],'--ignore-user-config')
                    self.assertNotIn('--permission-profile',command)
                    self.assertNotIn('--include-managed-config',command)
                    self.assertNotIn('--sandbox',command)
                    self.assertEqual(command[-2:] if session else command[-1:], [session,'-'] if session else ['-'])
            self.run.assert_not_called()

    def test_fresh_directory_exclusive_and_partial_never_converted(self):
        self.state.mkdir(mode=0o700)
        self.refuse(self.prepare,'linux_planner_fresh_directory_required')
        self.assertEqual(list(self.state.iterdir()),[]);self.run.assert_not_called()

    def test_workspace_overlap_nonempty_symlink_and_setuid_tool_refused(self):
        (self.workspace/'data').write_text('ordinary')
        self.refuse(self.prepare);(self.workspace/'data').unlink()
        self.refuse(lambda:self.prepare(checkout=self.workspace))
        alias=self.root/'alias';alias.symlink_to(self.workspace)
        self.refuse(lambda:self.prepare(workspace=alias))
        original=os.fstat;expected=self.cli.stat().st_ino
        def setuid(descriptor):
            info=original(descriptor)
            if info.st_ino!=expected:return info
            values={name:getattr(info,name) for name in dir(info) if name.startswith('st_')}
            values['st_mode'] |= 0o4000
            return SimpleNamespace(**values)
        with patch('os.fstat',side_effect=setuid):self.refuse(self.prepare)
        self.run.assert_not_called()

    def test_actual_failed_outside_write_check_never_waived_or_retried(self):
        self.prepare()
        def fail(argv,*args):
            if argv==[str(self.cli),'--version']:return self.fake_run(argv,*args)
            return SimpleNamespace(failure=None,returncode=78,stdout=canonical({'schema':1,'checks':{
                name:name!='outside_write_denied' for name in CHECKS}}),stderr=b'sensitive untrusted fixture')
        with LinuxPlannerPolicy(self.state) as policy,patch.object(policy_module,'_run_bounded',side_effect=fail):
            self.refuse(policy.qualify,'linux_planner_native_policy_unqualified')
            self.refuse(lambda:policy.validate_for(self.cli,self.workspace),'linux_planner_policy_unqualified')
            self.refuse(policy.qualify,'linux_planner_attempt_not_reusable')
        refusal=decode((self.state/'planner-qualification.json').read_bytes())
        self.assertFalse(refusal['checks']['outside_write_denied']);self.assertEqual(refusal['result'],'refused')
        self.assertTrue(refusal['cleanup_confirmed']);self.assertNotIn('sensitive',canonical(refusal).decode())

    def test_claimed_denial_with_observed_write_retains_unknown_branch_and_cleanup_false(self):
        self.prepare()
        def created(argv,*args):
            result=self.fake_run(argv,*args)
            if 'sandbox' in argv:
                payload=decode(argv[-1].encode());Path(payload['outside_write']).write_text('foreign synthetic unknown')
            return result
        with LinuxPlannerPolicy(self.state) as policy,patch.object(policy_module,'_run_bounded',side_effect=created):
            self.refuse(policy.qualify,'linux_planner_qualification_cleanup_unconfirmed')
        refusal=decode((self.state/'planner-qualification.json').read_bytes())
        self.assertFalse(refusal['checks']['outside_write_denied']);self.assertFalse(refusal['cleanup_confirmed'])
        self.assertTrue(list(self.workspace.iterdir()))
        self.assertEqual(next(self.state.glob('*-write')).read_text(),'foreign synthetic unknown')

    def test_replaced_ancestor_never_cleans_a_foreign_tree(self):
        self.prepare();held=self.root/'held-original'
        def change(argv,*args):
            result=self.fake_run(argv,*args)
            if 'sandbox' in argv:
                self.workspace.rename(held);self.workspace.mkdir(mode=0o700)
                (self.workspace/'unknown').write_text('foreign synthetic')
            return result
        with LinuxPlannerPolicy(self.state) as policy,patch.object(policy_module,'_run_bounded',side_effect=change):
            self.refuse(policy.qualify,'linux_planner_qualification_cleanup_unconfirmed')
        self.assertEqual((self.workspace/'unknown').read_text(),'foreign synthetic')
        self.assertTrue(list(held.iterdir()))

    def test_changed_environment_cli_source_or_namespace_invalidates_without_cli(self):
        for kind in ('environment','namespace','sources','cli'):
            with self.subTest(kind=kind):
                self.setUp();self.prepare()
                with LinuxPlannerPolicy(self.state) as policy:
                    policy.qualify();self.run.reset_mock()
                    if kind=='environment':self.identity['cgroups'][0]['path']='/other.service'
                    elif kind=='namespace':self.identity['namespaces']['mnt']+=1
                    elif kind=='sources':self.source_pins['files']['dev_broker.py']['sha256']='0'*64
                    else:self.cli.write_text('changed CLI')
                    self.refuse(lambda:policy.validate_for(self.cli,self.workspace),'linux_planner_policy_changed')
                    self.run.assert_not_called()

    def test_process_thread_and_serialization_are_not_replayable(self):
        self.prepare()
        with LinuxPlannerPolicy(self.state) as policy:
            policy.qualify()
            for call in (lambda:copy.copy(policy),lambda:pickle.dumps(policy)):
                with self.assertRaises(TypeError):call()
            outcomes=[]
            def foreign():
                try:policy.validate_for(self.cli,self.workspace)
                except DevelopmentError:outcomes.append('refused')
            thread=threading.Thread(target=foreign);thread.start();thread.join(2)
            self.assertEqual(outcomes,['refused'])
            with patch('os.getpid',return_value=os.getpid()+1):self.refuse(lambda:policy.validate_for(self.cli,self.workspace))

    def test_strict_checks_json_types_unknown_keys_and_missing_attempt_cannot_qualify(self):
        for invalid in ({'schema':True,'checks':{name:True for name in CHECKS}},
                        {'schema':1,'checks':{name:1 for name in CHECKS}},
                        {'schema':1,'checks':{},'unknown':'ignored'}):
            with self.subTest(invalid=invalid):
                self.setUp();self.prepare()
                def run(argv,*args):
                    return self.fake_run(argv,*args) if argv==[str(self.cli),'--version'] else SimpleNamespace(
                        failure=None,returncode=0,stdout=canonical(invalid),stderr=b'')
                with LinuxPlannerPolicy(self.state) as policy,patch.object(policy_module,'_run_bounded',side_effect=run):
                    self.refuse(policy.qualify,'linux_planner_canary_output_invalid')
                    self.refuse(lambda:policy.validate_for(self.cli,self.workspace))

    def test_qualification_receipt_or_attempt_hash_change_refuses(self):
        self.prepare()
        with LinuxPlannerPolicy(self.state) as policy:
            policy.qualify();self.run.reset_mock()
            path=self.state/'planner-qualification.json';value=decode(path.read_bytes())
            value['attempt_sha256']='0'*64;path.write_bytes(canonical(value))
            self.refuse(lambda:policy.validate_for(self.cli,self.workspace),'linux_planner_policy_unqualified')
            self.run.assert_not_called()


@unittest.skipIf(os.geteuid()==0 or os.getegid()==0,'Identity is deliberately nonroot')
class LinuxIdentityTests(unittest.TestCase):
    def setUp(self):
        self.env=environment();self.uid=os.geteuid();self.gid=os.getegid();self.groups=sorted(os.getgroups())
        caps='\n'.join(name+':\t'+('00000000000000ff' if name=='CapBnd' else '0000000000000000') for name in policy_module.CAP_NAMES)
        self.status=('Name:\tsynthetic\nUid:\t'+(' '.join([str(self.uid)]*4))+'\nGid:\t'+(' '.join([str(self.gid)]*4))+
            '\nGroups:\t'+' '.join(map(str,self.groups))+'\nNoNewPrivs:\t1\nSeccomp:\t2\nSeccomp_filters:\t1\n'+caps+'\n').encode()
        self.env['capabilities']['CapBnd']=255
        self.pid,self.tid=1234,5678
        self.base=f'/proc/{self.pid}/task/{self.tid}'
        self.files={self.base+'/status':self.status,self.base+'/cgroup':b'0::/synthetic-owner.service\n',
                    self.base+'/attr/current':b'synthetic-label\n'}
        self.directories={'/proc',f'/proc/{self.pid}',f'/proc/{self.pid}/task',self.base,self.base+'/attr',self.base+'/ns'}
        self.fds={};self.position={};self.opened=[];self.closed=[];self.next_fd=100

    def open(self,name,flags,mode=0o777,*,dir_fd=None):
        self.assertTrue(flags & os.O_NOFOLLOW);self.assertFalse(flags & (os.O_CREAT|os.O_TRUNC|os.O_WRONLY))
        path=name if dir_fd is None else self.fds[dir_fd]+'/'+name
        self.opened.append(path)
        if path not in self.directories and path not in self.files:raise FileNotFoundError(errno.ENOENT,'synthetic absent')
        self.next_fd+=1;self.fds[self.next_fd]=path;self.position[self.next_fd]=0
        return self.next_fd

    def read(self,descriptor,maximum):
        path=self.fds[descriptor];data=self.files[path]
        if isinstance(data,OSError):raise data
        start=self.position[descriptor];part=data[start:start+maximum];self.position[descriptor]+=len(part)
        return part

    def readlink(self,name,*,dir_fd):
        if name=='self':return str(self.pid)
        if name=='thread-self':return f'{self.pid}/task/{self.tid}'
        self.assertEqual(self.fds[dir_fd],self.base+'/ns')
        return name.split('_for_children')[0]+':['+str(self.env['namespaces'][name])+']'

    def patches(self):
        from contextlib import ExitStack
        stack=ExitStack();self.addCleanup(stack.close)
        for name,value in [('sys.platform','linux'),('_host',{'system':'Linux','release':'synthetic-5.15','machine':'x86_64'})]:
            if name=='sys.platform':stack.enter_context(patch.object(policy_module.sys,'platform',value))
            else:stack.enter_context(patch.object(policy_module,name,return_value=value))
        uname=SimpleNamespace(sysname='Linux',nodename='fixture',release='synthetic-5.15',version='synthetic kernel',machine='x86_64')
        for target,kwargs in [('os.uname',{'return_value':uname}),('os.getpid',{'return_value':self.pid}),
            ('threading.get_native_id',{'return_value':self.tid}),('os.open',{'side_effect':self.open}),
            ('os.read',{'side_effect':self.read}),('os.close',{'side_effect':lambda fd:self.closed.append(fd)}),
            ('os.readlink',{'side_effect':self.readlink}),('os.fstat',{'side_effect':lambda fd:SimpleNamespace(
                st_mode=0o40555 if self.fds[fd] in self.directories else 0o100444)})]:stack.enter_context(patch(target,**kwargs))
        stack.enter_context(patch.object(policy_module,'_procfs_magic',return_value=0x9fa0))
        return stack

    def refuse(self,call):
        with self.assertRaises(PlannerPolicyError) as caught:call()
        self.assertEqual(caught.exception.code,'linux_planner_environment_unconfirmed')

    def test_exact_own_thread_metadata_double_collection_and_no_external_read(self):
        self.patches()
        result=linux_execution_identity()
        self.assertEqual(result,self.env)
        self.assertEqual(set(self.opened),self.directories|set(self.files))
        self.assertEqual(set(self.closed),set(self.fds))
        self.assertNotIn('pid',result);self.assertNotIn('tid',result)
        self.assertEqual(len([name for name in self.opened if name.endswith('/status')]),2)

    def test_unsupported_host_root_and_procfs_are_refused_without_cli(self):
        with patch.object(policy_module.sys,'platform','darwin'),patch.object(policy_module,'_procfs_magic',side_effect=AssertionError('native call')):
            self.refuse(linux_execution_identity)
        self.patches()
        with patch.object(policy_module,'_procfs_magic',return_value=123):self.refuse(linux_execution_identity)
        with patch('os.geteuid',return_value=0):self.refuse(linux_execution_identity)

    def test_label_absence_only_on_known_missing_or_unsupported(self):
        self.patches();self.files[self.base+'/attr/current']=OSError(errno.ENOTSUP,'synthetic')
        self.assertEqual(linux_execution_identity()['label'],{'state':'absent'})
        self.files[self.base+'/attr/current']=PermissionError(errno.EACCES,'sensitive fixture')
        self.refuse(linux_execution_identity)
        del self.files[self.base+'/attr/current']
        self.assertEqual(linux_execution_identity()['label'],{'state':'absent'})

    def test_unknown_capability_duplicate_status_growth_and_setuid_refuse(self):
        for bad in (self.status+b'CapNew:\t0\n',self.status+b'NoNewPrivs:\t1\n',b'x'*32769,
                    self.status.replace(b'CapEff:\t0000000000000000',b'CapEff:\t0000000000000001'),
                    self.status.replace(('Uid:\t'+(' '.join([str(self.uid)]*4))).encode(),b'Uid:\t0 0 0 0')):
            with self.subTest(bad=bad[:40]):
                with self.patches():
                    self.files[self.base+'/status']=bad;self.refuse(linux_execution_identity)
                self.files[self.base+'/status']=self.status

    def test_status_or_namespace_context_drift_between_collections_refuses(self):
        with patch.object(policy_module,'_observe_identity',side_effect=[self.env,{**self.env,'no_new_privs':0}]):
            self.refuse(linux_execution_identity)
        self.patches();self.env['namespaces']['mnt']=0
        self.refuse(linux_execution_identity)

    def test_environment_codec_is_strict_and_is_not_an_authority(self):
        changes=(lambda v:v.update(extra=True),lambda v:v.update(euid=True),lambda v:v.update(procfs_magic=True),
            lambda v:v['host'].update(pointer_bits=True),lambda v:v['label'].update(unexpected='x'),
            lambda v:v['capabilities'].update(CapEff=1),lambda v:v['cgroups'][0].update(path='/a/../b'),
            lambda v:v['cgroups'][0].update(path='/unicode-é'),lambda v:v['namespaces'].update(mnt=True))
        for change in changes:
            value=copy.deepcopy(self.env);change(value)
            self.refuse(lambda:validate_linux_execution_identity(value))
        self.assertEqual(validate_linux_execution_identity(self.env),self.env)

    def test_cgroup_duplicate_hierarchy_and_traversal_are_rejected(self):
        for data in (b'0::/a\n0::/b\n',b'0::/a/../b\n',b'0::/a//b\n',b'1:cpu,cpu:/x\n',b'0:cpu:/x\n'):
            self.refuse(lambda:policy_module._parse_cgroups(data))
        self.assertEqual(policy_module._parse_cgroups(b'1:cpuacct,cpu:/unit\n0::/unit\n'),[
            {'hierarchy':0,'controllers':[],'path':'/unit'},
            {'hierarchy':1,'controllers':['cpu','cpuacct'],'path':'/unit'}])


@unittest.skipIf(os.geteuid()==0 or os.getegid()==0,'Policies reject privileged uid/gid')
class LinuxPolicyFailureTests(unittest.TestCase):
    setUp=LinuxPolicyTests.setUp
    prepare=LinuxPolicyTests.prepare
    fake_run=LinuxPolicyTests.fake_run
    refuse=LinuxPolicyTests.refuse
    def test_cleanup_failed_runner_never_reports_positive_cleanup(self):
        self.prepare()
        def failed(argv,*args):
            result=self.fake_run(argv,*args)
            if 'sandbox' in argv:result.failure='cleanup_failed'
            return result
        with LinuxPlannerPolicy(self.state) as policy,patch.object(policy_module,'_run_bounded',side_effect=failed):
            self.refuse(policy.qualify,'linux_planner_qualification_cleanup_unconfirmed')
        value=decode((self.state/'planner-qualification.json').read_bytes())
        self.assertFalse(value['cleanup_confirmed']);self.assertEqual(value['result'],'refused')

    def test_ambiguous_attempt_marker_write_cannot_launch_or_retry(self):
        self.prepare()
        with LinuxPlannerPolicy(self.state) as policy:
            original=policy.private.create
            def create(name,data):
                original(name,data)
                if name=='planner-attempt.json':raise DevelopmentError('private_write_failed')
            with patch.object(policy.private,'create',side_effect=create):
                self.refuse(policy.qualify,'linux_planner_attempt_not_reusable')
            self.run.assert_not_called()
            self.refuse(policy.qualify,'linux_planner_attempt_not_reusable')
            self.run.assert_not_called()

    def test_foreign_lock_fd_is_not_unlocked_or_closed(self):
        self.prepare();foreign=self.root/'foreign';foreign.write_text('synthetic foreign');foreign.chmod(0o600)
        with LinuxPlannerPolicy(self.state) as policy:
            descriptor=policy._lock_fd;os.close(descriptor)
            replacement=os.open(foreign,os.O_RDWR);self.assertEqual(replacement,descriptor)
            self.refuse(lambda:policy.validate_for(self.cli,self.workspace))
        self.assertTrue(os.fstat(replacement));os.close(replacement)

    def test_lock_closed_during_call_invalidates_with_a_bounded_failure(self):
        self.prepare()
        with LinuxPlannerPolicy(self.state) as policy:
            descriptor=policy._lock_fd
            with self.assertRaises(PlannerPolicyError) as caught:
                with policy._locked():os.close(descriptor)
            self.assertEqual(caught.exception.code,'linux_planner_policy_changed')
            self.assertTrue(policy._invalid)
            self.refuse(lambda:policy.validate_for(self.cli,self.workspace),'linux_planner_policy_closed')
        self.run.assert_not_called()

    def test_lock_replaced_during_call_never_unlocks_or_closes_unknown_fd(self):
        self.prepare();foreign=self.root/'foreign';foreign.write_text('synthetic foreign');foreign.chmod(0o600)
        with LinuxPlannerPolicy(self.state) as policy:
            descriptor=policy._lock_fd
            with patch.object(policy_module.fcntl,'flock',wraps=policy_module.fcntl.flock) as flock:
                with self.assertRaises(PlannerPolicyError) as caught:
                    with policy._locked():
                        os.close(descriptor)
                        replacement=os.open(foreign,os.O_RDWR);self.assertEqual(replacement,descriptor)
                self.assertEqual(caught.exception.code,'linux_planner_policy_changed')
                self.assertEqual(len(flock.call_args_list),1)
                self.assertEqual(flock.call_args.args[1],policy_module.fcntl.LOCK_EX|policy_module.fcntl.LOCK_NB)
        self.assertTrue(os.fstat(replacement));os.close(replacement)
        self.run.assert_not_called()

    def test_private_source_schema_bool_alias_cannot_match_actual_pin(self):
        self.prepare()
        # Rewrite a complete synthetic plan before construction: canonical
        # numeric-vs-boolean identity must still be compared without coercion.
        path=self.state/'planner-plan.json';value=decode(path.read_bytes())
        value['workspace_chain'][0]['uid']=False if value['workspace_chain'][0]['uid']==0 else True
        path.write_bytes(canonical(value))
        self.refuse(lambda:LinuxPlannerPolicy(self.state),'linux_planner_policy_changed')

    def test_source_inventory_absent_or_symbolic_new_module_is_not_invented(self):
        package=self.root/'incomplete-package';package.mkdir(mode=0o700)
        absent='linux_execution_qualification.py'
        for name in policy_module.LINUX_EXECUTION_SOURCES:
            if name!=absent:(package/name).write_bytes(b'# synthetic public source; never imported\n')
        with patch.object(policy_module,'__file__',str(package/'linux_planner_policy.py')), \
             patch.object(policy_module,'linux_source_pins',wraps=original_source_pins):
            self.refuse(self.prepare,'linux_planner_sources_unconfirmed')
            (package/absent).symlink_to(package/'linux_planner_policy.py')
            self.state=self.root/'state-symlink'
            self.refuse(self.prepare,'linux_planner_sources_unconfirmed')
        self.run.assert_not_called()


original_source_pins = policy_module.linux_source_pins


class LinuxPublicSourceTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name).resolve();self.package=self.root/'package';self.package.mkdir(mode=0o700)
        self.source=self.package/'linux_planner_policy.py';self.source.write_bytes(b'# public synthetic bytes, never imported\n');self.source.chmod(0o644)
        self.real_fstat,self.real_stat=os.fstat,os.stat
        self.public={tuple(policy_module.identity(self.real_stat(path))) for path in (self.package,self.source)}

    def stat_proxy(self,info,owner,mode=None):
        if tuple(policy_module.identity(info)) not in self.public:return info
        fields={name:getattr(info,name) for name in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_uid','st_mode','st_nlink')}
        fields['st_uid']=owner
        if mode is not None and not stat.S_ISDIR(fields['st_mode']):fields['st_mode']=stat.S_IFREG|mode
        return SimpleNamespace(**fields)

    def observe(self,owner,mode=None):
        from contextlib import ExitStack
        stack=ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch.object(policy_module,'__file__',str(self.source)))
        stack.enter_context(patch.object(policy_module,'LINUX_EXECUTION_SOURCES',('linux_planner_policy.py',)))
        stack.enter_context(patch.object(policy_module.os,'fstat',side_effect=lambda fd:self.stat_proxy(self.real_fstat(fd),owner,mode)))
        stack.enter_context(patch.object(policy_module.os,'stat',side_effect=lambda *a,**k:self.stat_proxy(self.real_stat(*a,**k),owner,mode)))
        return stack

    def test_root_owned_package_and_public_module_are_accepted_as_metadata_only(self):
        with self.observe(0):pins=policy_module.linux_source_pins()
        self.assertEqual(pins['files']['linux_planner_policy.py'],{'sha256':digest(self.source.read_bytes()),'bytes':self.source.stat().st_size})
        self.assertEqual(pins['package']['chain'][-1]['uid'],0)

    def test_current_owner_package_is_accepted_without_mocking_source_reader(self):
        with self.observe(os.geteuid()):pins=policy_module.linux_source_pins()
        self.assertEqual(pins['files']['linux_planner_policy.py']['sha256'],digest(self.source.read_bytes()))

    def test_foreign_owner_or_writable_public_module_is_refused(self):
        for owner,mode in ((os.geteuid()+100,None),(0,0o664)):
            with self.subTest(owner=owner,mode=mode),self.observe(owner,mode):
                with self.assertRaises(PlannerPolicyError) as caught:policy_module.linux_source_pins()
                self.assertEqual(caught.exception.code,'linux_planner_sources_unconfirmed')

    def test_symbolic_module_unknown_name_and_growth_after_stat_are_refused(self):
        with self.observe(os.geteuid()):
            parent=policy_module.absolute_directory(self.package)
            try:
                with self.assertRaises(PlannerPolicyError):policy_module._public_source_at(parent,'unknown.py',100)
                original=os.read;first=True
                def grow(fd,maximum):
                    nonlocal first
                    if first:
                        first=False
                        with self.source.open('ab') as stream:stream.write(b'changed')
                    return original(fd,maximum)
                with patch.object(policy_module.os,'read',side_effect=grow):
                    with self.assertRaises(PlannerPolicyError):policy_module._public_source_at(parent,self.source.name,self.source.stat().st_size)
            finally:os.close(parent)
        held=self.package/'held';self.source.rename(held);self.source.symlink_to(held)
        with self.observe(os.geteuid()):
            with self.assertRaises(PlannerPolicyError):policy_module.linux_source_pins()
        self.assertTrue(held.exists())
