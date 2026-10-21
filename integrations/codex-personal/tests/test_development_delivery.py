"""Real Git/Board/Runtime journals with explicit synthetic Linux observations.

No model/native Linux qualification/network/authentication occurs here. The
Linux document fixture tests strict composition, never attests a native run.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import uuid
import unittest

import test_development_worker as fixture
from test_git_workspace import git
from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import Budgets, DevelopmentError, NamedTest, canonical, digest, prepare_manifest
from blackoutburst_agents.development_delivery import DevelopmentDeliveryProof, DevelopmentPublishedDelivery
from blackoutburst_agents.git_publication import GitHubDraftPublisher, GitHubPublicationGrant, REPOSITORY, REMOTE, API
from blackoutburst_agents.git_delivery import LocalGitDeliveryController
from blackoutburst_agents.git_workspace import LocalGitWorkspaceController, LocalRepositoryGrant
from blackoutburst_agents.linux_test_backend import KIND, NATIVE_SCHEMA, CHECKS
from blackoutburst_agents.linux_test_protocol import EXECUTION_REQUEST_SCHEMA, linux_execution_backend_id


class DevelopmentDeliveryTests(unittest.TestCase):
    setUp=fixture.DevelopmentWorkerTests.setUp
    backend_factory=fixture.DevelopmentWorkerTests.backend_factory
    make_worker=fixture.DevelopmentWorkerTests.make_worker
    prepare=fixture.DevelopmentWorkerTests.prepare
    transport=fixture.DevelopmentWorkerTests.transport
    action=fixture.DevelopmentWorkerTests.action
    successful_actions=fixture.DevelopmentWorkerTests.successful_actions

    def ready(self):
        source=self.root
        (source/'untouched.txt').write_text('also tested\n');(source/'outside.txt').write_text('outside manifest\n')
        git(source,'init','--quiet','--initial-branch=main');git(source,'config','user.name','Synthetic author')
        git(source,'config','user.email','synthetic@example.invalid');git(source,'add','main.py','untouched.txt','outside.txt')
        git(source,'commit','--quiet','-m','Synthetic base');base=git(source,'rev-parse','HEAD')
        state=self.base/'git-state';state.mkdir(mode=0o700);work=self.base/'git-work';work.mkdir(mode=0o700)
        grant=LocalRepositoryGrant('owner','fixture',str(source),str(work),base)
        self.workspaces=LocalGitWorkspaceController(state,grant=grant);self.operation=str(uuid.uuid4())
        self.workspaces.plan(operation_id=self.operation,scope=self.manifest_scope)
        self.root=Path(self.workspaces.materialize(self.operation)['checkout'])
        self.broker_dir=self.base/'git-broker'
        prepare_manifest(self.broker_dir,root=self.root,base_sha=base,scope=self.manifest_scope,
            paths=['main.py','untouched.txt'],resource='repo:fixture',authorize_scope=lambda *_:True,
            tests=[NamedTest('unit',backend_id=self.backend.backend_id)],budgets=Budgets(max_file_bytes=1024))
        self.successful_actions();self.prepare(operation_id=self.operation)
        self.assertEqual(self.worker.run(self.operation)['status'],'review_ready')
        self.synthetic_linux_documents()
        self.proof=DevelopmentDeliveryProof(self.worker,self.workspaces,self.operation)
        dr=self.base/'deliveries';rr=self.base/'review';dr.mkdir(mode=0o700);rr.mkdir(mode=0o700)
        self.deliveries=LocalGitDeliveryController(self.workspaces,dr,rr,test_guard=self.proof)
        self.snapshot=self.deliveries.capture(operation_id=self.operation,scope=self.manifest_scope,
            changes=[dict(path='main.py',sha256=digest(b'value = 2\n'),mode='100644')])
        return self.snapshot

    def synthetic_linux_documents(self):
        op=self.journal.get(self.operation);config=deepcopy(op['configuration']);payload=deepcopy(op['payload'])
        with PrivateDirectory(self.broker_dir) as private:
            manifest=decode(private.read('manifest.json',128*1024));broker=decode(private.read('state.json',2*1024*1024))
            planner=decode(private.read('planner.json',2*1024*1024))
        plan=dict(schema='blackoutburst-linux-execution-plan-v1',root=str(self.root),root_identity=manifest['root_identity'],
            sources={},environment={'synthetic_document_fixture':True},bootstrap_sha256=digest(b'synthetic-bootstrap'))
        plan_hash=digest(canonical(plan));backend_id=linux_execution_backend_id(plan_hash)
        manifest['tests'][0]['backend_id']=backend_id;manifest_hash=digest(canonical(manifest))
        config.update(execution_profile=KIND,linux_execution_plan_sha256=plan_hash,manifest_hash=manifest_hash)
        config['backend_hash']=digest(canonical(dict(backend_id=backend_id,plan_hash=plan_hash,kind=KIND,
            supervision=dict(kind=KIND,sources=plan['sources'],environment=plan['environment'],bootstrap_sha256=plan['bootstrap_sha256']))))
        policy=dict(schema='blackoutburst-linux-planner-policy-v1',checkout=str(self.root),checkout_identity=manifest['root_identity'],
            workspace=str(self.planner_root),workspace_identity=config['pins']['planner_workspace']['identity'],
            environment=plan['environment'],cli_sha256=planner['configuration']['policy_cli_hash'],executable=self.policy.executable)
        config['policy_hash']=digest(canonical(policy))
        with PrivateDirectory(self.backend_dir) as private:
            private.create('execution-plan.json',canonical(plan));private.create('backend-manifest.json',canonical(manifest))
        with PrivateDirectory(self.policy_dir) as private:private.create('planner-plan.json',canonical(policy))
        key,entry=next((key,row) for key,row in broker['operations'].items() if row['kind']=='test')
        entry['detail']=deepcopy(manifest['tests'][0]);entry['request_hash']=digest(canonical({'kind':'test',**entry['detail']}))
        entry['admission']=dict(schema=1,operation_id=key,manifest_hash=manifest_hash,request_hash=entry['request_hash'],
            backend_id=backend_id,root_identity=manifest['root_identity'],dispatch='dispatched')
        planner_hash=planner['review']['snapshot_sha256'];fence=digest(b'synthetic-fence');input_plan=digest(b'synthetic-input-plan')
        request=dict(schema=EXECUTION_REQUEST_SCHEMA,operation_id=key,workflow_id=self.operation,manifest_sha256=manifest_hash,
            plan_sha256=plan_hash,scope_sha256=digest(canonical(manifest['scope'])),input_snapshot_sha256=digest(b'synthetic-input'),
            input_packet_sha256=digest(b'synthetic-packet'),planner_snapshot_sha256=planner_hash,root=str(self.root),
            root_identity=manifest['root_identity'],name='unit',timeout_seconds=entry['detail']['timeout_seconds'],
            max_output_bytes=entry['detail']['max_output_bytes'],input_plan_sha256=input_plan,fence_sha256=fence)
        reads=5*(min(manifest['budgets']['max_file_bytes'],1048576)+1)*len(manifest['files'])
        reservation=dict(workflow_id=self.operation,planner_snapshot_sha256=planner_hash,fence_sha256=fence,
            input_plan_sha256=input_plan,execution_plan_sha256=plan_hash,action_sha256=digest(canonical({'schema':1,'kind':'test','name':'unit'})),
            action_key_sha256=key,prior_state_sha256=digest(canonical(broker)),capture_passes=5,reserved_read_bytes=reads)
        native=dict(schema=NATIVE_SCHEMA,nonce=uuid.uuid4().hex,request_sha256=digest(canonical(request)),plan_sha256=plan_hash,
            source_released=True,source_release_intent=True,exit_code=0,reason='completed',checks={name:True for name in CHECKS},
            launcher_attempted=True,output_bytes=entry['receipt']['output_bytes'],output_sha256=entry['receipt']['output_sha256'],
            cleanup=dict(init_identity_received=True,init_terminal=True,launcher_reaped=True,cleanup_confirmed=True,numeric_pid_signalled=False),
            identity=dict(namespace_identity=[111,222],acquired_by='SCM_RIGHTS_validated',numeric_pid_signalled=False),broker_snapshot_admitted=False)
        directory=self.backend_dir/('execution-'+key);directory.mkdir(mode=0o700);challenge=uuid.uuid4().hex
        first=dict(schema=1,nonce=native['nonce'],sequence=1,started_at=10.0,validated_at=10.1,deadline=11.5)
        window={**first,'sequence':3,'started_at':10.2,'validated_at':10.3,'deadline':11.7}
        job=dict(schema=KIND,nonce=native['nonce'],request=request,backend_directory=str(self.backend_dir),plan_sha256=plan_hash,
            script_sha256=digest(b'synthetic-script'),timeout_seconds=request['timeout_seconds'],max_output_bytes=request['max_output_bytes'],
            control=first,qualification=False,qualification_case=None)
        with PrivateDirectory(directory) as private:
            private.create('job.json',canonical(job));private.create('native-receipt.json',canonical(native))
            private.create('state.json',canonical(dict(schema=1,nonce=native['nonce'],request_sha256=digest(canonical(request)),
                phase='complete',boundary_sequence=2,source_challenge=challenge,native_receipt_sha256=digest(canonical(native)))))
            private.create('source-admission.json',canonical(dict(nonce=native['nonce'],request_sha256=digest(canonical(request)),
                identity=native['identity'],control=window,phase='source_release_intent')))
            private.create('source-window.json',canonical(dict(nonce=native['nonce'],challenge=challenge,
                request_sha256=digest(canonical(request)),window_sha256=digest(canonical(window)))))
            private.create('source-window.ready',b'')
        broker.update(schema=3,manifest_hash=manifest_hash,linux_execution_origin=str(uuid.uuid4()),
            linux_execution_plan_sha256=plan_hash,linux_execution={key:dict(phase='broker_admitted',reservation=reservation,
                request=request,native_receipt_sha256=digest(canonical(native)),admitted_snapshot_sha256=planner_hash)})
        broker['used']['read_bytes']+=reads
        parent=payload['parent_binding'];parent_scope=parent['parent_scope']
        parent_scope['configuration_hash']=digest(canonical(dict(configuration=config,initial_context_hash=payload['initial_context']['fingerprint'])))
        parent['configuration_hash']=parent_scope['configuration_hash']
        planner['configuration'].update(manifest_hash=manifest_hash,policy_hash=config['policy_hash'],parent_operation=deepcopy(parent_scope))
        planner['configuration_hash']=digest(canonical(planner['configuration']))
        with PrivateDirectory(self.broker_dir) as private:
            private.replace('manifest.json',canonical(manifest));private.replace('state.json',canonical(broker));private.replace('planner.json',canonical(planner))
        # This constructs a synthetic archived Linux fixture after simulated
        # execution. It is never a production upgrade or native qualification.
        self.journal._db.execute('UPDATE operations SET configuration_json=?,payload_json=? WHERE id=?',
            (canonical(config).decode(),canonical(payload).decode(),self.operation))
        self.native_directory=directory

    def test_exact_all_manifest_map_reconstructs_distinct_git_snapshot_and_seals(self):
        self.ready();before=(self.broker_dir/'state.json').read_bytes();calls=len(self.backend.calls)
        evidence=self.proof.evidence(self.snapshot)
        planner=decode((self.broker_dir/'planner.json').read_bytes())
        self.assertNotEqual(evidence['snapshot_sha256'],planner['review']['snapshot_sha256'])
        self.assertEqual(evidence['snapshot_sha256'],digest(canonical(self.snapshot)))
        result=self.deliveries.finalize(operation_id=self.operation,snapshot_sha256=evidence['snapshot_sha256'],
            test_evidence=evidence,message='Synthetic tested delivery')
        self.assertNotEqual(result['delivery_sha'],self.workspaces.grant['base_commit']);self.assertEqual(before,(self.broker_dir/'state.json').read_bytes())
        self.assertEqual(calls,len(self.backend.calls))

    def change_document(self,directory,name,change):
        with PrivateDirectory(directory) as private:
            value=decode(private.read(name,2*1024*1024));change(value);private.replace(name,canonical(value))

    def rejected(self):
        with self.assertRaises(DevelopmentError):self.proof.evidence(self.snapshot)

    def test_untouched_manifest_path_is_part_of_tested_snapshot(self):
        self.ready();(self.root/'untouched.txt').write_text('unverified edit\n');self.rejected()
        self.assertEqual((self.root/'untouched.txt').read_text(),'unverified edit\n')

    def test_change_outside_manifest_refuses_even_with_same_selected_files(self):
        self.ready();(self.root/'outside.txt').write_text('unselected edit\n');self.rejected()

    def test_baseline_mode_change_and_added_file_refuse(self):
        self.ready();(self.root/'main.py').chmod(0o755);self.rejected();(self.root/'main.py').chmod(0o644)
        (self.root/'new.txt').write_text('not selected\n');self.rejected()

    def test_input_only_profile_and_unadmitted_native_observation_refuse(self):
        self.ready()
        raw=(self.broker_dir/'state.json').read_bytes()
        self.change_document(self.broker_dir,'state.json',lambda v:v.update(schema=2));self.rejected()
        with PrivateDirectory(self.broker_dir) as p:p.replace('state.json',raw)
        self.change_document(self.broker_dir,'state.json',lambda v:next(iter(v['linux_execution'].values())).update(phase='dispatched',admitted_snapshot_sha256=None))
        self.rejected()

    def test_native_terminal_chain_and_hash_are_required_without_repair(self):
        self.ready();(self.native_directory/'source-window.ready').unlink();self.rejected()
        self.assertFalse((self.native_directory/'source-window.ready').exists())

    def test_changed_named_suite_receipt_refuses(self):
        self.ready()
        self.change_document(self.broker_dir,'state.json',lambda v:next(row for row in v['operations'].values() if row['kind']=='test')['receipt'].update(result='failed'))
        self.rejected()

    def test_missing_suite_and_wrong_planner_snapshot_refuse(self):
        self.ready();raw=(self.broker_dir/'planner.json').read_bytes()
        self.change_document(self.broker_dir,'planner.json',lambda v:v['review']['tests'].clear());self.rejected()
        with PrivateDirectory(self.broker_dir) as p:p.replace('planner.json',raw)
        self.change_document(self.broker_dir,'planner.json',lambda v:v['review'].update(snapshot_sha256='0'*64));self.rejected()

    def test_configuration_and_operation_scope_cannot_drift(self):
        self.ready();op=self.journal.get(self.operation);changed=deepcopy(op['configuration']);changed['model']='other-model'
        self.journal._db.execute('UPDATE operations SET configuration_json=? WHERE id=?',(canonical(changed).decode(),self.operation))
        self.rejected()

    def test_runtime_cleanup_and_unassociated_executions_refuse(self):
        self.ready();record=self.runtime.db.execute('SELECT id,receipt FROM executions LIMIT 1').fetchone()
        receipt=json.loads(record['receipt']);receipt['cleanup_confirmed']=False
        self.runtime.db.execute('UPDATE executions SET receipt=? WHERE id=?',(canonical(receipt).decode(),record['id']))
        self.rejected()

    def published(self,*,prepare=True):
        if prepare:self.ready()
        evidence=self.proof.evidence(self.snapshot)
        self.seal=self.deliveries.finalize(operation_id=self.operation,snapshot_sha256=evidence['snapshot_sha256'],
            test_evidence=evidence,message='Synthetic tested delivery')
        root=self.base/'publications';root.mkdir(mode=0o700)
        gh=self.base/'approved-gh';gh.write_text('#!/bin/sh\nexit 99\n');gh.chmod(0o700)
        python=self.base/'approved-python';python.write_bytes(Path(sys.executable).resolve().read_bytes());python.chmod(0o700)
        remote=self.base/'remote.git';git(self.base,'clone','--bare','--no-hardlinks',self.workspaces.repository,str(remote))
        grant=GitHubPublicationGrant('owner',REPOSITORY,'main',self.workspaces.grant['base_commit'])
        self.publisher=GitHubDraftPublisher(self.deliveries,root,grant=grant,gh_executable=gh,python_executable=python)
        self.prs=[];self.posts=0;self.lookups=0;self.during_lookup=None
        def command(argv,*,cwd,git=False):
            if git:
                assert argv[0]=='/usr/bin/git' and argv[1] in ('ls-remote','push') and REMOTE in argv
                result=subprocess.run([str(remote) if a==REMOTE else a for a in argv],cwd=cwd,
                    env={'PATH':'/usr/bin:/bin','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1','GIT_ALLOW_PROTOCOL':'file'},
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                return result.returncode,result.stdout,result.stderr
            assert argv[0]==str(gh)
            if argv[1:]==['auth','status','--hostname','github.com']:return 0,b'normal synthetic auth\n',b''
            assert argv[1:5]==['api','--hostname','github.com','--method'];method,path=argv[5:7]
            if path=='user':value={'login':'synthetic-user'}
            elif path==API:value=dict(full_name=REPOSITORY,html_url='https://github.com/'+REPOSITORY,permissions={'push':True},archived=False,disabled=False)
            elif method=='POST' and path==API+'/pulls':
                assert argv[7]=='--input';payload=json.loads(Path(argv[8]).read_bytes());self.posts+=1
                value=dict(number=17,html_url='https://github.com/'+REPOSITORY+'/pull/17',state='open',draft=True,
                    user={'login':'synthetic-user'},title=payload['title'],body=payload['body'],
                    head=dict(ref=payload['head'],sha=self.seal['delivery_sha'],repo={'full_name':REPOSITORY}),
                    base=dict(ref='main',sha=self.workspaces.grant['base_commit'],repo={'full_name':REPOSITORY}))
                self.prs.append(value)
            elif method=='GET' and path.startswith(API+'/pulls?'):
                self.lookups+=1
                if self.during_lookup:self.during_lookup()
                value=self.prs
            else:raise AssertionError('unexpected command cannot reach network')
            return 0,canonical(value),b''
        self.publisher._command=command
        self.publisher.publish(operation_id=self.operation,delivery_sha=self.seal['delivery_sha'],title='Synthetic delivery',body='Selected controller summary.')
        self.link=DevelopmentPublishedDelivery(self.proof,self.deliveries,self.publisher)

    def test_board_review_sha_and_pr_share_one_cas_before_claim_release(self):
        self.published();before=self.board.get_ticket(self.human,self.ticket['id'])['ticket']
        updates=[];call=self.client.call
        def tracked(name,args):
            if name=='update_ticket':updates.append(deepcopy(args))
            return call(name,args)
        self.client.call=tracked;posts=self.posts
        result=self.worker.publish(self.operation,delivery_publication=self.link)
        self.assertEqual(result['stage'],'complete',result)
        after=self.board.get_ticket(self.human,self.ticket['id'])['ticket']
        self.assertEqual(len(updates),1);self.assertEqual(after['version'],before['version']+1)
        self.assertEqual(after['status'],'review');self.assertIsNone(after['claimant'])
        self.assertIn(self.seal['delivery_sha'],after['evidence']);self.assertIn('/pull/17',after['evidence'])
        self.assertEqual(posts,self.posts)
        self.assertEqual(self.worker.publish(self.operation,delivery_publication=self.link)['stage'],'complete')
        self.assertEqual(len(updates),1)
        with self.assertRaises(DevelopmentError):self.worker.publish(self.operation)

    def test_publication_interrupted_before_cas_retries_exact_intent_and_authority(self):
        class Interrupted(BaseException): pass
        self.published();before=self.board.get_ticket(self.human,self.ticket['id'])['ticket']
        original=self.client.call;updates=[];interrupted=[]
        def call(name,args):
            if name=='update_ticket':
                updates.append(deepcopy(args))
                if not interrupted:interrupted.append(True);raise Interrupted()
            return original(name,args)
        self.client.call=call
        with self.assertRaises(Interrupted):self.worker.publish(self.operation,delivery_publication=self.link)
        retained=self.journal.get(self.operation)
        self.assertEqual(retained['stage'],'publishing')
        self.assertEqual(self.board.get_ticket(self.human,self.ticket['id'])['ticket'],before)
        calls,tests,posts=len(self.inferences),len(self.backend.calls),self.posts
        result=self.worker.publish(self.operation,delivery_publication=self.link)
        self.assertEqual(result['stage'],'complete',result)
        after=self.board.get_ticket(self.human,self.ticket['id'])['ticket']
        self.assertEqual(after['version'],before['version']+1);self.assertIsNone(after['claimant'])
        self.assertEqual(len(updates),2);self.assertEqual(updates[0],updates[1])
        self.assertEqual(self.journal.get(self.operation)['payload']['publication'],retained['payload']['publication'])
        self.assertEqual((len(self.inferences),len(self.backend.calls),self.posts),(calls,tests,posts))

    def test_revocation_during_github_reads_precedes_any_board_cas(self):
        self.published();before=self.board.get_ticket(self.human,self.ticket['id'])['ticket'];fired=[]
        def revoke():
            if not fired:
                fired.append(True);self.board.set_policy(self.human,True,True,True,True,[],[])
        self.during_lookup=revoke
        result=self.worker.publish(self.operation,delivery_publication=self.link)
        self.assertNotEqual(result['stage'],'complete');self.assertEqual(before,self.board.get_ticket(self.human,self.ticket['id'])['ticket'])
        self.assertEqual(self.posts,1)

    def test_arbitrary_model_metadata_cannot_enable_board_delivery_publication(self):
        self.ready()
        with self.assertRaises(DevelopmentError):
            self.worker.publish(self.operation,delivery_publication={'delivery_sha':'a'*40,'url':'https://example.invalid'})

    def test_lost_board_response_reconciles_without_second_cas_or_pr_post(self):
        self.published();call=self.client.call;updates=[]
        class Crash(BaseException):pass
        def lose_response(name,args):
            result=call(name,args)
            if name=='update_ticket':updates.append(True);raise Crash()
            return result
        self.client.call=lose_response
        with self.assertRaises(Crash):self.worker.publish(self.operation,delivery_publication=self.link)
        self.assertEqual(self.journal.get(self.operation)['stage'],'publishing')
        with self.assertRaises(DevelopmentError):self.worker.publish(self.operation)
        self.client.call=call
        self.assertEqual(self.worker.publish(self.operation,delivery_publication=self.link)['stage'],'complete')
        self.assertEqual(updates,[True]);self.assertEqual(self.posts,1)

    def test_publication_intent_cannot_gain_delivery_parameter_on_retry(self):
        self.ready();call=self.client.call
        class Crash(BaseException):pass
        def interrupt(name,args):
            if name=='update_ticket':raise Crash()
            return call(name,args)
        self.client.call=interrupt
        with self.assertRaises(Crash):self.worker.publish(self.operation)
        self.client.call=call;self.published(prepare=False)
        with self.assertRaises(DevelopmentError):self.worker.publish(self.operation,delivery_publication=self.link)
        self.assertIsNone(self.journal.get(self.operation)['payload']['publication']['delivery'])

    def test_changed_pr_after_retained_board_intent_refuses_without_republication(self):
        self.published();call=self.client.call;updates=[]
        class Crash(BaseException):pass
        def interrupt(name,args):
            if name=='update_ticket':updates.append(True);raise Crash()
            return call(name,args)
        self.client.call=interrupt
        with self.assertRaises(Crash):self.worker.publish(self.operation,delivery_publication=self.link)
        self.client.call=call;self.prs[0]['draft']=False
        result=self.worker.publish(self.operation,delivery_publication=self.link)
        self.assertEqual(result['stage'],'publishing');self.assertEqual(updates,[True]);self.assertEqual(self.posts,1)
        self.assertIsNotNone(self.board.get_ticket(self.human,self.ticket['id'])['ticket']['claimant'])

    def test_guard_missing_run_lock_refuses_without_creating_it(self):
        from blackoutburst_agents.dev_runtime import development_work_id
        self.ready();path=self.store.state_dir/(development_work_id(self.operation)+'.lock');path.unlink()
        self.rejected();self.assertFalse(path.exists())

    def test_guard_never_marks_an_interrupted_turn_or_changes_journals(self):
        self.ready();before=[]
        self.store._db.execute("UPDATE turns SET status='running' WHERE id=(SELECT id FROM turns LIMIT 1)")
        for directory in (self.journal.directory,self.store.state_dir,self.runtime.directory,self.broker_dir):
            before.append({p.name:p.read_bytes() for p in directory.iterdir() if p.is_file()})
        self.rejected()
        after=[{p.name:p.read_bytes() for p in directory.iterdir() if p.is_file()}
            for directory in (self.journal.directory,self.store.state_dir,self.runtime.directory,self.broker_dir)]
        self.assertEqual(before,after)

    def test_stored_evidence_refuses_individually_valid_runtime_receipt_drift(self):
        self.ready();evidence=self.proof.evidence(self.snapshot)
        row=self.runtime.db.execute('SELECT id,receipt FROM executions LIMIT 1').fetchone()
        receipt=json.loads(row['receipt']);receipt['revision']+=1
        self.runtime.db.execute('UPDATE executions SET receipt=? WHERE id=?',(canonical(receipt).decode(),row['id']))
        self.assertNotEqual(self.proof.evidence(self.snapshot),evidence)
        with self.assertRaises(DevelopmentError):
            self.proof(operation_id=self.operation,scope=self.manifest_scope,snapshot=self.snapshot,evidence=evidence)

    def test_extra_runtime_execution_is_not_ignored_as_an_unrelated_receipt(self):
        from blackoutburst_agents.dev_runtime import development_work_id
        from blackoutburst_agents.runtime_journal import RuntimeJournal
        self.ready()
        self.runtime.begin(RuntimeJournal.configuration(self.scope,development_work_id(self.operation),str(uuid.uuid4()),
            'unexpected-tool',provider='local_broker'))
        self.rejected()

    def test_read_guard_and_real_run_store_lock_exclude_each_other(self):
        from blackoutburst_agents.dev_runtime import development_work_id
        from blackoutburst_agents.run_store import StoreError
        self.ready();work_id=development_work_id(self.operation)
        with self.store.work_lock(work_id,inspection_only=True):self.rejected()
        with self.proof._read_work(work_id):
            with self.assertRaises(StoreError):
                with self.store.work_lock(work_id,inspection_only=True):pass
