"""Real local archived result -> commit/PR/Board/handoff composition.

Linux/model/GitHub packets below are explicitly synthetic. This is not native
host qualification, an authenticated GitHub test, or a new model/test execution.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import unittest
from unittest.mock import patch

import test_development_delivery as fixture
from test_development_worker import FixtureClient
from test_runtime_integration import ControlClient
from test_git_workspace import git
from blackoutburst_agents.development_pool import DevelopmentPoolDriver
from blackoutburst_agents.development_pool_delivery import DevelopmentPoolResultDelivery
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.dev_journal import PrivateDirectory,decode
from blackoutburst_agents.dev_manifest import Budgets,DevelopmentError,NamedTest,canonical,digest,prepare_manifest
from blackoutburst_agents.dev_fence import DevelopmentFence
from blackoutburst_agents.git_workspace import LocalGitWorkspaceController,LocalRepositoryGrant
from blackoutburst_agents.git_publication import GitHubDraftPublisher,GitHubPublicationGrant,REPOSITORY,REMOTE,API
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runtime_client import RuntimeBoardClient
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.work_journal import WorkJournal
from blackoutburst_agents.worker_pool import WorkerPool


class Crash(BaseException): pass


@unittest.skipIf(fixture.fixture.Board is None,'Board sources not bundled')
class DevelopmentPoolDeliveryTests(unittest.TestCase):
    def fixture(self, *, assigned=True):
        f=fixture.DevelopmentDeliveryTests();f.setUp();self.addCleanup(f.doCleanups)
        connection=f.base/'connection.json'
        with PrivateDirectory(f.base) as private:
            private.create(connection.name,canonical(dict(mcp_url=f.client.endpoint,token='SYNTHETIC_TOKEN',client='codex',identity_key='worker',name='Worker')))
        f.driver=DevelopmentPoolDriver(f.base/'selections',connection_file=connection,owner_id='owner',agent_id=f.scope['agent_id'],board_endpoint=f.client.endpoint)
        f.pool=WorkerPool(f.base/'pool',concurrency=1,drivers={'development':f.driver.binding()});self.addCleanup(f.pool.close)
        f.job=f.pool.submit(request_id='explicit-result',driver_id='development',payload={'project_id':f.project['id'],'ticket_id':f.ticket['id']})
        f.operation=f.job['id'];context=f.pool._context({**f.job,'payload':{'project_id':f.project['id'],'ticket_id':f.ticket['id']}},create=True)
        f.store.close();f.runtime.close();f.journal.close()
        f.store=RunStore(context.journals['runs']);self.addCleanup(f.store.close)
        f.runtime=RuntimeJournal(context.journals['runtime']);self.addCleanup(f.runtime.close)
        f.journal=WorkJournal(context.journals['work']);self.addCleanup(f.journal.close)
        f.scope['board_session_id']=f.job['session_id'];f.manifest_scope['session_id']=f.job['session_id']
        f.client.session_id=f.job['session_id'];f.control=ControlClient(f.board,f.raw,f.job['session_id']);f.worker=f.make_worker()
        if assigned:
            f.ticket=f.board.update_ticket(f.human,f.ticket['id'],f.ticket['version'],assignee_agent_id=f.scope['agent_id'])
        source=f.root;(source/'untouched.txt').write_text('also tested\n');(source/'outside.txt').write_text('outside manifest\n')
        git(source,'init','--quiet','--initial-branch=main');git(source,'config','user.name','Synthetic author');git(source,'config','user.email','synthetic@example.invalid')
        git(source,'add','main.py','untouched.txt','outside.txt');git(source,'commit','--quiet','-m','Synthetic base');base=git(source,'rev-parse','HEAD')
        state=f.base/'git-state';state.mkdir(mode=0o700);work=f.base/'git-work';work.mkdir(mode=0o700)
        f.workspaces=LocalGitWorkspaceController(state,grant=LocalRepositoryGrant('owner','agent-board',str(source),str(work),base))
        f.workspaces.plan(operation_id=f.operation,scope=f.manifest_scope);f.root=Path(f.workspaces.materialize(f.operation)['checkout'])
        f.broker_dir=f.base/'git-broker'
        prepare_manifest(f.broker_dir,root=f.root,base_sha=base,scope=f.manifest_scope,paths=['main.py','untouched.txt'],resource='repo:fixture',
            authorize_scope=lambda *_:True,tests=[NamedTest('unit',backend_id=f.backend.backend_id)],budgets=Budgets(max_file_bytes=1024))
        f.successful_actions();f.prepare(operation_id=f.operation);self.assertEqual(f.worker.run(f.operation)['status'],'review_ready')
        f.synthetic_linux_documents();op=f.journal.get(f.operation)
        # Synthetic archival conversion updates the same canonical binding as
        # its planner configuration. Product code never repairs this metadata.
        with PrivateDirectory(f.base/'anchor') as private:
            fence=decode(private.read('fence.json',2*1024*1024));row=next(iter(fence['records'].values()))
            planner=decode((f.broker_dir/'planner.json').read_bytes())
            row['binding']['configuration_hash']=digest(canonical(planner['configuration']))
            row['binding']['parent_scope']=deepcopy(op['payload']['parent_binding']['parent_scope'])
            DevelopmentFence._validate(object.__new__(DevelopmentFence),fence);private.replace('fence.json',canonical(fence))
        for folder,name in ((f.backend_dir,'qualification.json'),(f.policy_dir,'planner-qualification.json')):
            with PrivateDirectory(folder) as private:private.create(name,canonical({'synthetic_archived_observation':True}))
        instruction=f.base/'instruction.txt'
        with PrivateDirectory(f.base) as private:private.create(instruction.name,op['configuration']['instruction'].encode())
        selection=dict(schema=1,operation_id=f.operation,worker_id=f.job['worker_id'],session_id=f.job['session_id'],scope=f.scope,
            broker_directory=str(f.broker_dir),backend_directory=str(f.backend_dir),planner_policy_directory=str(f.policy_dir),
            instruction_file=str(instruction),model=op['configuration']['model'],budgets=op['configuration']['budgets'])
        selected=f.base/'selection.json'
        with PrivateDirectory(f.base) as private:private.create(selected.name,canonical(selection))
        binding=f.driver.bind_selection(f.job,selected)
        with PrivateDirectory(context.journals['queue']) as private:
            private.create('development-prepare-intent.json',canonical(dict(operation_id=f.operation,session_id=f.job['session_id'],selection_sha256=binding['selection_sha256'])))
            private.create('development-run-intent.json',canonical(dict(operation_id=f.operation,session_id=f.job['session_id'])))
            private.create('development-result.json',canonical(dict(operation_id=f.operation,session_id=f.job['session_id'],code_status='private_review_ready',ticket_published=False,cleanup_confirmed=True)))
        f.pool._update(f.operation,state='settled',outcome='complete',cleanup_confirmed=True,execution_active=False,reason='driver_finished')
        # Reopening in the product must use actual existing databases; no old
        # worker/client object is returned by the test connection factory.
        f.store.close();f.runtime.close();f.journal.close()
        f.gh=f.base/'approved-gh';f.gh.write_text('#!/bin/sh\nexit 99\n');f.gh.chmod(0o700)
        f.python=f.base/'approved-python';f.python.write_bytes(Path(sys.executable).resolve().read_bytes());f.python.chmod(0o700)
        f.remote=f.base/'remote.git';git(f.base,'clone','--bare','--no-hardlinks',str(source),str(f.remote))
        f.pushes=f.posts=0;f.prs=[];f.clients=[];f.controls=[];f.commands=[];f.lost_release=False;f.bad_runtime=False
        def client(path,*,session_id,timeout):
            self.assertEqual(session_id,f.job['session_id']);c=FixtureClient(f.board,f.raw,session_id)
            c.connection_identity=tuple(file_document(path)[1]['identity']);c.close=lambda:None
            c.adopt_registration=lambda:setattr(c,'actor',f.board.actor_session(f.raw,session_id))
            call=c.call
            def tracked(name,args):
                result=call(name,args)
                if name=='release_ticket' and f.lost_release:f.lost_release=False;raise Crash()
                return result
            c.call=tracked;f.clients.append(c);return c
        def control(c):
            value=ControlClient(f.board,f.raw,c.session_id);value.close=lambda:None;call=value.call
            def observed(name,args):
                result=call(name,args)
                if f.bad_runtime and name=='lookup_execution':result={**result,'execution':{**result['execution'],'cleanup_confirmed':False}}
                return result
            value.call=observed;f.controls.append(value);return value
        def command(publisher,argv,*,cwd,git=False):
            f.commands.append(list(argv))
            if git:
                self.assertIn(REMOTE,argv);self.assertIn(argv[1],('ls-remote','push'))
                if argv[1]=='push':f.pushes+=1
                result=subprocess.run([str(f.remote) if a==REMOTE else a for a in argv],cwd=cwd,
                    env={'PATH':'/usr/bin:/bin','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1','GIT_ALLOW_PROTOCOL':'file'},stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                return result.returncode,result.stdout,result.stderr
            self.assertEqual(argv[0],str(f.gh))
            if argv[1:]==['auth','status','--hostname','github.com']:return 0,b'synthetic normal auth\n',b''
            self.assertEqual(argv[1:5],['api','--hostname','github.com','--method']);method,path=argv[5:7]
            if path=='user':result={'login':'synthetic-user'}
            elif path==API:result=dict(full_name=REPOSITORY,html_url='https://github.com/'+REPOSITORY,permissions={'push':True},archived=False,disabled=False)
            elif path.startswith(API+'/pulls?'):result=f.prs
            elif method=='POST' and path==API+'/pulls':
                f.posts+=1;payload=json.loads(Path(argv[8]).read_bytes());request=json.loads((Path(cwd)/'publication.json').read_bytes())['request']
                result=dict(number=17,html_url='https://github.com/'+REPOSITORY+'/pull/17',state='open',draft=True,user={'login':'synthetic-user'},title=payload['title'],body=payload['body'],
                    head=dict(ref=payload['head'],sha=request['delivery_sha'],repo={'full_name':REPOSITORY}),base=dict(ref='main',sha=base,repo={'full_name':REPOSITORY}));f.prs.append(result)
            else:raise AssertionError('Unknown command cannot reach network')
            return 0,canonical(result),b''
        for item in (patch('blackoutburst_agents.development_pool_delivery.canonical_anchor',return_value=f.base/'anchor'),
                     patch('blackoutburst_agents.development_pool.DevelopmentBoardClient.from_connection_file',side_effect=client),
                     patch.object(RuntimeBoardClient,'from_worker',side_effect=control),patch.object(GitHubDraftPublisher,'_command',new=command)):
            item.start();self.addCleanup(item.stop)
        f.pipeline=DevelopmentPoolResultDelivery(f.base/'result-deliveries',pool=f.pool,driver=f.driver,workspaces=f.workspaces,
            publication_grant=GitHubPublicationGrant('owner',REPOSITORY,'main',base),gh_executable=f.gh,python_executable=f.python)
        f.changes=[dict(path='main.py',sha256=digest(b'value = 2\n'),mode='100644')]
        return f

    @staticmethod
    def deliver(f,**options):
        values=dict(changes=f.changes,commit_message='Synthetic tested delivery',pr_title='Explicit synthetic result',pr_body='Controller selected result.')
        values.update(options);return f.pipeline.deliver(f.operation,**values)

    def test_actual_archived_result_reopens_and_delivers_once_without_model_test_or_cleanup_effect(self):
        f=self.fixture();inferences=len(f.inferences);tests=len(f.backend.calls);before=f.board.get_ticket(f.human,f.ticket['id'])['ticket']
        result=self.deliver(f);after=f.board.get_ticket(f.human,f.ticket['id'])['ticket']
        self.assertEqual(result['status'],'complete');self.assertEqual((f.pushes,f.posts),(1,1));self.assertEqual(after['version'],before['version']+2)
        self.assertEqual(after['status'],'review');self.assertIsNone(after['claimant']);self.assertIsNone(after['assignee_agent_id'])
        self.assertIn(result['delivery_sha'],after['evidence']);self.assertIn('/pull/17',after['evidence'])
        self.assertEqual((len(f.inferences),len(f.backend.calls)),(inferences,tests))
        self.assertTrue(f.controls);self.assertTrue(all(name=='lookup_execution' for c in f.controls for name in c.calls))
        self.assertTrue(all(c.register_count==0 for c in f.clients))
        calls=len(f.commands);self.assertEqual(self.deliver(f),result);self.assertEqual(len(f.commands),calls)
        self.assertEqual(f.pipeline.status(f.operation),result)
        self.assertEqual(git(f.remote,'rev-parse','refs/heads/main'),f.workspaces.grant['base_commit'])

    def test_lost_handoff_response_reuses_same_key_without_republication(self):
        f=self.fixture();f.lost_release=True
        with self.assertRaises(Crash):self.deliver(f)
        self.assertEqual(f.pipeline.status(f.operation)['stage'],'handoff-intent')
        before=f.board.get_ticket(f.human,f.ticket['id'])['ticket'];self.assertIsNone(before['assignee_agent_id'])
        result=self.deliver(f);self.assertEqual(result['status'],'complete');self.assertEqual((f.pushes,f.posts),(1,1))
        self.assertEqual(f.board.get_ticket(f.human,f.ticket['id'])['ticket'],before)
        releases=[args for c in f.clients for name,args in c.calls if name=='release_ticket'];self.assertEqual(len(releases),2);self.assertEqual(releases[0],releases[1])

    def test_settled_marker_never_substitutes_for_fresh_runtime_receipts(self):
        f=self.fixture();f.bad_runtime=True
        with self.assertRaises(DevelopmentError):self.deliver(f)
        self.assertEqual((f.pushes,f.posts),(0,0));self.assertFalse((f.pipeline.paths['deliveries']/f.operation).exists())

    def test_missing_author_assignment_refuses_before_git_capture(self):
        f=self.fixture(assigned=False)
        with self.assertRaises(DevelopmentError) as caught:self.deliver(f)
        self.assertEqual(caught.exception.code,'development_pool_delivery_author_assignment_required')
        self.assertEqual((f.pushes,f.posts),(0,0));self.assertFalse((f.pipeline.paths['deliveries']/f.operation).exists())

    def test_missing_fence_stays_missing_and_does_not_recreate_a_row(self):
        f=self.fixture();path=f.base/'anchor'/'fence.json';path.unlink()
        with self.assertRaises(DevelopmentError):self.deliver(f)
        self.assertFalse(path.exists());self.assertEqual((f.pushes,f.posts),(0,0))

    def test_stop_is_local_and_prevents_delivery(self):
        f=self.fixture();self.assertTrue(f.pipeline.stop(f.operation)['stop_requested']);self.assertFalse(f.clients);self.assertFalse(f.controls)
        with self.assertRaises(DevelopmentError):self.deliver(f)
        self.assertEqual((f.pushes,f.posts),(0,0))

    def test_pool_global_stop_refuses_at_boundary_without_dispatch_or_publication(self):
        f=self.fixture();f.pool.stop()
        with self.assertRaises(DevelopmentError):self.deliver(f)
        self.assertEqual((f.pushes,f.posts),(0,0));self.assertFalse(f.controls)

    def test_operator_change_or_deletion_cannot_replace_retained_request(self):
        f=self.fixture();f.bad_runtime=True
        with self.assertRaises(DevelopmentError):self.deliver(f)
        f.bad_runtime=False
        with self.assertRaises(DevelopmentError):self.deliver(f,pr_title='Changed operator title')
        with self.assertRaises(DevelopmentError):self.deliver(f,changes=[dict(path='main.py',sha256=None,mode=None)])
        self.assertEqual((f.pushes,f.posts),(0,0))

    def test_output_root_cannot_create_directories_in_checkout_or_through_alias(self):
        f=self.fixture()
        def construct(path):
            return DevelopmentPoolResultDelivery(path,pool=f.pool,driver=f.driver,workspaces=f.workspaces,
                publication_grant=GitHubPublicationGrant('owner',REPOSITORY,'main',f.workspaces.grant['base_commit']),gh_executable=f.gh,python_executable=f.python)
        target=f.root/'delivery-output'
        with self.assertRaises(DevelopmentError):construct(target)
        self.assertFalse(target.exists())
        alias=f.base/'alias';alias.symlink_to(f.workspaces.workspace_root,target_is_directory=True)
        with self.assertRaises(DevelopmentError):construct(alias/'escaped-output')
        self.assertFalse((f.workspaces.workspace_root/'escaped-output').exists())

    def test_unqualified_pool_result_cannot_be_promoted_by_local_result_text(self):
        f=self.fixture()
        f.pool._update(f.operation,state='retained',cleanup_confirmed=False,execution_active=None,reason='cleanup_unconfirmed')
        with self.assertRaises(DevelopmentError):self.deliver(f)
        self.assertFalse(f.clients);self.assertEqual((f.pushes,f.posts),(0,0))

    def test_historical_receipt_cannot_invent_a_different_commit_or_pr(self):
        f=self.fixture();self.deliver(f)
        folder=f.pipeline.paths['operations']/f.operation
        with PrivateDirectory(folder) as private:
            value=decode(private.read('complete.json',512*1024));value['delivery_sha']='b'*40
            private.replace('complete.json',canonical(value))
        before=(f.pushes,f.posts,len(f.commands))
        with self.assertRaises(DevelopmentError):f.pipeline.status(f.operation)
        with self.assertRaises(DevelopmentError):self.deliver(f)
        self.assertEqual((f.pushes,f.posts,len(f.commands)),before)

    def test_missing_real_component_receipt_refuses_without_repair_or_effect(self):
        for component in ('deliveries','publications','handoff','board'):
            with self.subTest(component=component):
                f=self.fixture();self.deliver(f)
                work=f.pool.journal.private.path/'workers'/f.job['worker_id']/'work'
                if component=='board':
                    with sqlite3.connect(work/'work-journal.sqlite3') as db:
                        raw=db.execute('SELECT payload_json FROM operations WHERE id=?',(f.operation,)).fetchone()[0]
                        payload=json.loads(raw);payload['publication_confirmed']=False
                        db.execute('UPDATE operations SET payload_json=? WHERE id=?',(canonical(payload).decode(),f.operation))
                    removed=None
                else:
                    removed=(work/('review-handoff-'+f.operation+'.json') if component=='handoff' else
                        f.pipeline.paths[component]/f.operation/('delivery.json' if component=='deliveries' else 'publication.json'))
                    removed.unlink()
                before=(f.pushes,f.posts,len(f.commands))
                with self.assertRaises(DevelopmentError):f.pipeline.status(f.operation)
                with self.assertRaises(DevelopmentError):self.deliver(f)
                self.assertEqual((f.pushes,f.posts,len(f.commands)),before)
                if removed is not None:self.assertFalse(removed.exists())

    def test_complete_status_reads_real_journals_without_clients_constructor_or_disk_write(self):
        f=self.fixture();expected=self.deliver(f)
        work=f.pool.journal.private.path/'workers'/f.job['worker_id']/'work'
        before={p.name:p.read_bytes() for p in work.iterdir() if p.is_file()}
        calls=(len(f.clients),len(f.controls),len(f.commands))
        with patch.object(WorkJournal,'__init__',side_effect=AssertionError('Status cannot initialize/migrate a journal')):
            self.assertEqual(f.pipeline.status(f.operation),expected)
        self.assertEqual({p.name:p.read_bytes() for p in work.iterdir() if p.is_file()},before)
        self.assertEqual((len(f.clients),len(f.controls),len(f.commands)),calls)

    def test_historical_status_refuses_pending_sqlite_recovery_without_touching_it(self):
        f=self.fixture();self.deliver(f)
        work=f.pool.journal.private.path/'workers'/f.job['worker_id']/'work'
        with PrivateDirectory(work) as private:private.create('work-journal.sqlite3-journal',b'pending recovery is not a completed receipt')
        before={p.name:p.read_bytes() for p in work.iterdir() if p.is_file()}
        with self.assertRaises(DevelopmentError):f.pipeline.status(f.operation)
        self.assertEqual({p.name:p.read_bytes() for p in work.iterdir() if p.is_file()},before)
