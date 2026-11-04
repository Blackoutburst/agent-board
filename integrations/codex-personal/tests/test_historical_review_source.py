"""Actual two-generation Git/Board/Runtime; only external transports synthetic."""
from contextlib import ExitStack,closing
from copy import deepcopy
from pathlib import Path
import time
import pytest

from blackoutburst_agents.historical_review_source import HistoricalAggregateReviewSource,HistoricalAggregateReadView
from blackoutburst_agents.review_pool import DeliveryReviewPoolDriver
from blackoutburst_agents.review_lifecycle import DeliveryReviewLifecycle
from blackoutburst_agents.worker_pool import WorkerPool
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical,digest
from blackoutburst_agents.dev_journal import PrivateDirectory
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.git_publication import GitHubDraftPublisher
from test_continuation_aggregate_review import run_fixture
from test_runtime_integration import ControlClient
from test_development_aggregate_delivery import linux_lock_observation
from test_git_workspace import git as git_command


def manager(f):
    policy=dict(allow_read=True,allow_messages=True,allow_requests=True,actions=f.actions,
        resources=['repo:fixture'],autonomy='requested',allow_create_projects=False)
    p=f.board.create_agent_profile(f.human,'Recovery Lead',project_ids=[f.project['id']],policy=policy)
    p=f.board.set_agent_profile_management(f.human,p['id'],p['version'],True)
    p=f.board.set_agent_profile_claim_management(f.human,p['id'],p['version'],True)
    device=f.board.create_device(f.human,'Recovery Lead device',[])
    f.board.bind_agent_profile(f.human,p['id'],p['version'],device['id'],'recovery-lead','codex')
    raw=f.board.authenticate(device['token'])
    f.board.register_agent(raw,'recovery-lead','Recovery Lead',[],'codex',False,'recovery-lead')
    return f.board.actor_session(raw,'recovery-lead')


def test_pretransport_refusal_can_receive_one_new_review_of_same_published_delivery(
        tmp_path,monkeypatch,linux_lock_observation):
    def check(f,aggregate,helper,source,calls,turns,author_turns,public,root):
        profile=f.board.get_agent_profile(f.human,helper.reviewer['agent_id'])
        f.board.update_agent_profile(f.human,profile['id'],profile['version'],
            policy=profile['policy']|{'resources':['repo:fixture']})
        lead=manager(f)
        original=ControlClient.call;blocked=[True];unknown=[False]
        def control(client,name,args):
            if name=='reserve_execution' and blocked[0]:raise RuntimeError('Synthetic refusal before admission')
            if name=='lookup_execution' and unknown[0]:return {'found':True}
            return original(client,name,args)
        monkeypatch.setattr(ControlClient,'call',control)
        with pytest.raises(DevelopmentError,match='continuation_review_cleanup_unknown'):helper.run()
        assert len(turns)==0 and len(author_turns)==3
        old_job=helper.job;old_pool=helper.pool.status()
        assert old_pool['jobs'][0]['state']=='retained' and old_pool['jobs'][0]['cleanup_confirmed'] is False
        helper.pool.close();blocked[0]=False
        original_public=source.receipt
        current=f.board.get_ticket(f.human,source.receipt['author_scope']['ticket_id'])['ticket']
        now=time.time()
        with monkeypatch.context() as clock:
            clock.setattr(time,'time',lambda:now+3600)
            released=f.board.release_managed_agent_claim(lead,current['id'],current['version'],
                'retire-prior-pretransport','Inspect refusal before transport','No old reservation; preserve all old state.')
        assert released['claimant'] is None and released['version']==current['version']+1
        events=f.board.list_activity(f.human,ticket_id=current['id'],limit=10,order='desc')
        gate=next(e for e in events if e['event']=='managed_agent_claim_released')
        # The old source stops being live. Its historical artifacts remain the
        # same source/test/publication, and are not made into an old permit.
        state=aggregate.task.snapshot()['state']
        aggregate.task.stop(expected_revision=state['revision'],request_id='retire-original-core')
        with pytest.raises(DevelopmentError):source.status(source.operation_id)
        old_files=[source.directory/'source.json',source.directory/'continuation-review.json',
            helper.directory/'pool/pool-state.json',helper.directory/'lifecycle'/old_job['id']/'stop.json',
            aggregate.directory/'aggregate.json',aggregate.paths['deliveries']/source.operation_id/'delivery.json',
            aggregate.task.journal.private.path/'continuation.sqlite3']
        prior_worker=helper.directory/'pool/workers'/old_job['worker_id']
        old_files.extend(prior_worker/name for name in ('runtime/runtime.sqlite3','runs/runs.sqlite3','work/work-journal.sqlite3'))
        snapshot={str(p):p.read_bytes() for p in old_files}
        publisher=source.publisher
        remote=f.base/'remote.git'
        def advance_main(label):
            parent=git_command(remote,'rev-parse','refs/heads/main')
            tip=git_command(remote,'-c','user.name=Synthetic later author','-c','user.email=later@example.invalid',
                'commit-tree',f.commit+'^{tree}','-p',parent,'-m',label)
            git_command(remote,'update-ref','refs/heads/main',tip,parent)
            return tip
        # A real descendant exists on main before the historical admission. The
        # PR API still reports its original base, as it may during a ref update.
        first_tip=advance_main('A companion change after the sealed delivery')
        assert first_tip!=source.receipt['original_public_base']
        with PrivateDirectory(publisher.root/source.operation_id) as private:
            with pytest.raises(DevelopmentError,match='git_publication_base_changed'):
                publisher._remote(private,publisher._read(private))
        from blackoutburst_agents.development_worker import DevelopmentBoardWorker
        from blackoutburst_agents.development_aggregate_delivery import AggregateDevelopmentDeliveryController
        from blackoutburst_agents.linux_test_backend import SupervisedLinuxNamedPythonBackend
        def forbidden(*args,**kwargs):pytest.fail('Recovery reopened an author/model/test/publication effect')
        original_publish=GitHubDraftPublisher.publish
        monkeypatch.setattr(DevelopmentBoardWorker,'prepare',forbidden)
        monkeypatch.setattr(DevelopmentBoardWorker,'run',forbidden)
        monkeypatch.setattr(AggregateDevelopmentDeliveryController,'__init__',forbidden)
        monkeypatch.setattr(SupervisedLinuxNamedPythonBackend,'run',forbidden)
        monkeypatch.setattr(GitHubDraftPublisher,'publish',forbidden)
        def read(view,argv,*,cwd,git=False):
            view._roots();view._confirm_launch(argv,cwd=cwd,git=git)
            return publisher._command(argv,cwd=cwd,git=git)
        monkeypatch.setattr(HistoricalAggregateReadView,'_command',read)
        with ExitStack() as stack:
            def fresh(label):
                driver=DeliveryReviewPoolDriver(f.base/(label+'-selections'),
                    connection_file=root/'connections/reviewer.json',owner_id='owner',
                    agent_id=helper.reviewer['agent_id'],board_endpoint=f.seed.endpoint)
                pool=stack.enter_context(closing(WorkerPool(f.base/(label+'-pool'),concurrency=1,drivers={'review':driver.binding()})))
                job=pool.submit(request_id=label,driver_id='review',payload={'project_id':f.project['id'],'ticket_id':current['id']})
                options=dict(prior_review_directory=helper.directory,prior_job_id=old_job['id'],source_directory=source.directory,
                    publication_root=publisher.root,workspace_state_root=source.workspaces.state_root,git_executable=source.workspaces.git,
                    expected_source_sha256=digest((source.directory/'source.json').read_bytes()),
                    expected_delivery_sha=source.receipt['delivery_sha'],expected_ticket_version=released['version'],
                    revocation_event_id=gate['id'],job=job,connection_file=root/'connections/reviewer.json',owner_id='owner',
                    agent_id=helper.reviewer['agent_id'],project_id=f.project['id'],ticket_id=current['id'],board_endpoint=f.seed.endpoint,
                    gh_executable=publisher.gh,interpreter=publisher.python)
                return driver,pool,job,options
            driver,pool,job,options=fresh('unknown-old-runtime')
            unknown[0]=True
            before=sum(n=='claim_ticket' for n,_ in calls)
            with pytest.raises(DevelopmentError,match='historical_review_previous_unresolved'):
                HistoricalAggregateReviewSource(f.base/'unknown-source',**options)
            unknown[0]=False
            assert sum(n=='claim_ticket' for n,_ in calls)==before and not turns
            head=public['prs'][0]['head']['sha'];public['prs'][0]['head']['sha']='0'*40
            with pytest.raises(DevelopmentError):HistoricalAggregateReviewSource(f.base/'changed-head-source',**options)
            public['prs'][0]['head']['sha']=head
            assert sum(n=='claim_ticket' for n,_ in calls)==before and not turns
            with pytest.raises(DevelopmentError,match='historical_review_revocation_required'):
                HistoricalAggregateReviewSource(f.base/'wrong-gate-source',**(options|{'revocation_event_id':gate['id']-1}))
            assert sum(n=='claim_ticket' for n,_ in calls)==before and not turns
            driver,pool,job,options=fresh('changed-context')
            history=HistoricalAggregateReviewSource(f.base/'changed-context-source',**options)
            project=f.board.get_project(f.human,f.project['id'])
            f.board.update_project(f.human,project['id'],project['version'],description='Fresh project context')
            lifecycle=DeliveryReviewLifecycle(f.base/'changed-context-lifecycle',pool=pool,driver=driver,source=history,
                executable=f.tools['codex'],cli_version='codex-cli 0.163.0',interpreter=f.tools['python'])
            with pytest.raises(DevelopmentError,match='historical_review_context_changed'):
                lifecycle.prepare(job,source_operation_id=history.operation_id,instruction='Review the same immutable delivery.',
                    material=history.material(),model='explicit-independent-review-model',timeout=120)
            assert sum(n=='claim_ticket' for n,_ in calls)==before and not turns
            driver,pool,job,options=fresh('revoked-permission')
            history=HistoricalAggregateReviewSource(f.base/'revoked-permission-source',**options)
            lifecycle=DeliveryReviewLifecycle(f.base/'revoked-permission-lifecycle',pool=pool,driver=driver,source=history,
                executable=f.tools['codex'],cli_version='codex-cli 0.163.0',interpreter=f.tools['python'])
            policy=f.board.get_settings(f.human)['policy']
            f.board.set_policy(f.human,True,True,True,False,policy['actions'],policy['resources'])
            with pytest.raises(DevelopmentError):
                lifecycle.prepare(job,source_operation_id=history.operation_id,instruction='Review the same immutable delivery.',
                    material=history.material(),model='explicit-independent-review-model',timeout=120)
            f.board.set_policy(f.human,policy['enabled'],policy['allow_read'],policy['allow_reply'],policy['allow_requests'],
                policy['actions'],policy['resources'])
            assert sum(n=='claim_ticket' for n,_ in calls)==before and not turns
            driver,pool,job,options=fresh('new-review')
            history=HistoricalAggregateReviewSource(f.base/'new-review-source',**options)
            assert history.receipt==original_public and history.admission['previous']['kind']=='retired_before_transport'
            assert history.admission['original_frame_sha256']==source.frame_sha256
            assert history.new_job['session_id']!=old_job['session_id']
            material=history.material()
            def during_read():
                public['during_lookup']=None
                public['prs'][0]['base']['sha']=advance_main('Another change during the historical public read')
            public['during_lookup']=during_read
            assert history.status(history.operation_id)==original_public
            assert history.material()==material and history.receipt['original_public_base']==f.commit
            assert public['prs'][0]['base']['sha'] not in (f.commit,first_tip)
            # All identity fields except the current base tip remain exact.
            row=deepcopy(public['prs'][0]);reader=history.reader
            for path,value in [(('head','sha'),'0'*40),(('head','ref'),'other-head'),
                    (('head','repo','full_name'),'Other/repo'),(('base','ref'),'other-main'),
                    (('base','repo','full_name'),'Other/repo'),(('base','sha'),'not-a-sha'),
                    (('title',),'Other title'),(('body',),'Other body'),(('user','login'),'OtherAuthor'),
                    (('draft',),False),(('state',),'closed'),(('html_url',),'https://example.invalid/pull/17')]:
                altered=deepcopy(row);part=altered
                for key in path[:-1]:part=part[key]
                part[path[-1]]=value
                with pytest.raises(DevelopmentError):reader._pr(reader.publication,altered)
            assert reader._pr(reader.publication,row)==original_public['pull_request']
            assert row==public['prs'][0]
            with pytest.raises(DevelopmentError,match='git_publication_pr_mismatch'):
                publisher._pr(reader.publication,row)
            # Missing, malformed, duplicate or unexpected refs never qualify a
            # historical read. The command still passes its closed argv check.
            branch=reader.request['branch'];base_ref='refs/heads/main';head_ref='refs/heads/'+branch
            valid_base=(public['prs'][0]['base']['sha']+'\t'+base_ref+'\n').encode()
            valid_head=(history.receipt['delivery_sha']+'\t'+head_ref+'\n').encode()
            normal_read=reader._command
            for raw in (valid_head,valid_base,b'bad\t'+base_ref.encode()+b'\n'+valid_head,
                    valid_base+valid_base+valid_head,valid_base+valid_head+b'0'*40+b'\trefs/heads/unselected\n',
                    valid_base+b'0'*40+b'\t'+head_ref.encode()+b'\n'):
                def broken_refs(argv,*,cwd,git=False):
                    reader._confirm_launch(argv,cwd=cwd,git=git)
                    return (0,raw,b'') if git else normal_read(argv,cwd=cwd,git=git)
                with monkeypatch.context() as refs:
                    refs.setattr(reader,'_command',broken_refs)
                    with PrivateDirectory(reader.root/history.operation_id) as private:
                        with pytest.raises(DevelopmentError):reader._remote(private,reader.publication)
            assert sum(n=='claim_ticket' for n,_ in calls)==before and not turns
            lifecycle=DeliveryReviewLifecycle(f.base/'new-review-lifecycle',pool=pool,driver=driver,source=history,
                executable=f.tools['codex'],cli_version='codex-cli 0.163.0',interpreter=f.tools['python'])
            assert material==history.old['material']
            prepared=lifecycle.prepare(job,source_operation_id=history.operation_id,instruction='Review the same immutable delivery.',
                material=material,model='explicit-independent-review-model',timeout=120)
            assert prepared['status']=='prepared'
            assert pool.dispatch_pending()==[job['id']]
            pool.threads[job['id']].join(timeout=300);assert not pool.threads[job['id']].is_alive()
            settled=pool.status()['jobs'][0]
            assert (settled['state'],settled['outcome'],settled['cleanup_confirmed'])==('settled','complete',True),settled
            result=lifecycle.publish(job['id']);assert result['status']=='complete'
            assert lifecycle.publish(job['id'])==result
            ticket=f.board.get_ticket(f.human,current['id'])['ticket']
            assert ticket['claimant'] is None and 'Add a boundary test' in ticket['outcome']
            assert len(turns)==1 and len(author_turns)==3
            assert public['posts']==public['pushes']==1
            assert {str(p):p.read_bytes() for p in old_files}==snapshot
            with pytest.raises(DevelopmentError,match='git_publication_readonly_view'):
                original_publish(history.reader,operation_id=history.operation_id,
                    delivery_sha=history.receipt['delivery_sha'],title='Forbidden',body='Forbidden')
            for argv,cwd,git in [
                ([str(history.reader.gh),'api','--hostname','github.com','--method','POST','repos/Blackoutburst/agent-board/pulls'],
                 history.reader.root/history.operation_id,False),
                ([str(history.reader.git),'push','https://github.com/Blackoutburst/agent-board.git','HEAD:main'],
                 history.reader.root/history.operation_id/'transport',True)]:
                with pytest.raises(DevelopmentError,match='historical_review_command_refused'):
                    GitHubDraftPublisher._command(history.reader,argv,cwd=cwd,git=git)
    run_fixture(tmp_path,monkeypatch,linux_lock_observation,check)


def test_host_only_source_does_not_expand_native_or_family_rosters():
    from blackoutburst_agents import family_sources,linux_planner_policy
    from deploy.worker_pool.package import required_sdk_modules
    assert 'historical_review_source.py' not in family_sources.MODULES
    assert 'historical_review_source.py' not in linux_planner_policy.LINUX_EXECUTION_SOURCES
    pins=family_sources.controller_sources()
    assert sum(row['bytes'] for row in pins['files'].values())<=2*1024*1024
    assert 'historical_review_source' not in required_sdk_modules('0.48.3')
    assert 'historical_review_source' not in required_sdk_modules('0.48.4')
    assert 'historical_review_source' in required_sdk_modules('0.49.0')


def test_native_entry_imports_do_not_load_the_host_recovery_module():
    import subprocess,sys
    script='''
import importlib,importlib.abc,sys
class HostForbidden(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path,target=None):
        if fullname=='blackoutburst_agents.historical_review_source':
            raise AssertionError('Host-only recovery imported by native entry')
sys.meta_path.insert(0,HostForbidden())
for name in ('family_cli','linux_test_namespace','linux_test_supervisor','linux_planner_policy'):
    importlib.import_module('blackoutburst_agents.'+name)
assert 'blackoutburst_agents.historical_review_source' not in sys.modules
'''
    subprocess.run([sys.executable,'-B','-c',script],check=True,capture_output=True)
