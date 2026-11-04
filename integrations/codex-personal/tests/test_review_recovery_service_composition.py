"""Service factory -> real HistoricalSource/Lifecycle/Pool/Board/Runtime.

Original fixture producers receive their final service-shaped directories at
construction, before any pins or proof exists. External model/GitHub/native
transports stay synthetic; no author or old review record is replayed.
"""
from pathlib import Path
import time
import uuid

import pytest

from blackoutburst_agents import review_recovery_service as module
from blackoutburst_agents.continuation_aggregate_review import ContinuationAggregateReviewer
from blackoutburst_agents.development_aggregate_delivery import AggregateDevelopmentDeliveryController
from blackoutburst_agents.dev_journal import PrivateDirectory
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical,digest
from blackoutburst_agents.git_publication import GitHubDraftPublisher
from blackoutburst_agents.git_workspace import LocalGitWorkspaceController
from blackoutburst_agents.historical_review_source import HistoricalAggregateReadView
from test_historical_review_source import manager,run_fixture
from test_runtime_integration import ControlClient
from test_development_aggregate_delivery import linux_lock_observation


def test_service_factory_prepares_dispatches_and_publishes_real_historical_review(
        tmp_path,monkeypatch,linux_lock_observation):
    aggregate_init=AggregateDevelopmentDeliveryController.__init__
    workspace_init=LocalGitWorkspaceController.__init__
    publisher_init=GitHubDraftPublisher.__init__
    reviewer_init=ContinuationAggregateReviewer.__init__
    def final_path(base,leaf):
        target=base/'recovery-target';target.mkdir(mode=0o700,exist_ok=True)
        (target/'aggregate').mkdir(mode=0o700,exist_ok=True)
        path=target/'aggregate'/leaf;path.mkdir(mode=0o700,exist_ok=True)
        return path
    def aggregate(self,directory,*args,**kwargs):
        directory=Path(directory)
        if directory.name=='aggregate':directory=final_path(directory.parent,'delivery')
        aggregate_init(self,directory,*args,**kwargs)
    def workspace(self,directory,*args,**kwargs):
        directory=Path(directory)
        if directory.name=='original-git-state':directory=final_path(directory.parent,'git-state')
        workspace_init(self,directory,*args,**kwargs)
    def publisher(self,deliveries,directory,*args,**kwargs):
        directory=Path(directory)
        if directory.name=='publications':
            base=directory.parent;directory=final_path(base,'publications')
            # Production uses a fixed public interpreter outside the owner
            # tree. Put this fixture's actual binary in a disjoint tools dir.
            tools=base/'publication-tools';tools.mkdir(mode=0o700,exist_ok=True)
            interpreter=tools/'python';interpreter.write_bytes(Path(kwargs['python_executable']).read_bytes())
            interpreter.chmod(0o700);kwargs['python_executable']=interpreter
        publisher_init(self,deliveries,directory,*args,**kwargs)
    def reviewer(self,directory,*args,**kwargs):
        directory=Path(directory)
        if directory.name=='continuation-review-run':directory=directory.parent/'recovery-target/independent-review'
        reviewer_init(self,directory,*args,**kwargs)
    monkeypatch.setattr(AggregateDevelopmentDeliveryController,'__init__',aggregate)
    monkeypatch.setattr(LocalGitWorkspaceController,'__init__',workspace)
    monkeypatch.setattr(GitHubDraftPublisher,'__init__',publisher)
    monkeypatch.setattr(ContinuationAggregateReviewer,'__init__',reviewer)

    def check(f,aggregate,helper,source,calls,turns,author_turns,public,root):
        profile=f.board.get_agent_profile(f.human,helper.reviewer['agent_id'])
        f.board.update_agent_profile(f.human,profile['id'],profile['version'],
            policy=profile['policy']|{'resources':['repo:fixture']})
        lead=manager(f);original=ControlClient.call
        def refused(client,name,args):
            if name=='reserve_execution':raise RuntimeError('Synthetic pretransport refusal')
            return original(client,name,args)
        with monkeypatch.context() as temporary:
            temporary.setattr(ControlClient,'call',refused)
            with pytest.raises(DevelopmentError,match='continuation_review_cleanup_unknown'):helper.run()
        old_job=helper.job;helper.pool.close()
        ticket=f.board.get_ticket(f.human,source.receipt['author_scope']['ticket_id'])['ticket']
        now=time.time()
        with monkeypatch.context() as clock:
            clock.setattr(time,'time',lambda:now+3600)
            released=f.board.release_managed_agent_claim(lead,ticket['id'],ticket['version'],
                'service-retire-prior','Observed pretransport refusal','Preserve all prior records.')
        gate=next(e for e in f.board.list_activity(f.human,ticket_id=ticket['id'],limit=10,order='desc')
            if e['event']=='managed_agent_claim_released')
        state=aggregate.task.snapshot()['state']
        aggregate.task.stop(expected_revision=state['revision'],request_id='stop-original-core')
        old_paths=[source.directory/'source.json',source.directory/'continuation-review.json',
            helper.directory/'pool/pool-state.json',aggregate.task.journal.private.path/'continuation.sqlite3']
        snapshot={str(p):p.read_bytes() for p in old_paths}
        publisher=source.publisher
        def read(view,argv,*,cwd,git=False):
            view._roots();view._confirm_launch(argv,cwd=cwd,git=git)
            return publisher._command(argv,cwd=cwd,git=git)
        monkeypatch.setattr(HistoricalAggregateReadView,'_command',read)
        s=module.ReviewRecoveryService.__new__(module.ReviewRecoveryService)
        s.root=root;s.directory=root/'review-recoveries'/str(uuid.uuid4())
        s.directory.parent.mkdir(mode=0o700);s.directory.mkdir(mode=0o700)
        s.target=source.directory.parents[2]
        assert helper.directory==s.target/'independent-review'
        assert source.workspaces.state_root==s.target/'aggregate/git-state'
        assert publisher.root==s.target/'aggregate/publications'
        s.scope=dict(owner_id='owner',board_endpoint=f.seed.endpoint)
        s.tools=dict(executable=f.tools['codex'],cli_version='codex-cli 0.163.0',interpreter=publisher.python,
            gh_executable=publisher.gh,max_seconds=300)
        s.value=dict(recovery_id=s.directory.name,prior_review_job_id=old_job['id'],
            source_operation_id=source.operation_id,expected_source_sha256=digest((source.directory/'source.json').read_bytes()),
            delivery_sha=source.receipt['delivery_sha'],ticket_version=released['version'],revocation_event_id=gate['id'],
            reviewer_agent_id=helper.reviewer['agent_id'],connection_file=str(root/'connections/reviewer.json'),
            project_id=f.project['id'],ticket_id=ticket['id'],model='explicit-independent-review-model',timeout=120,
            skills=['review'],instruction='Review the exact original source and final test.')
        s.state=dict(job=None);s.pool=s.driver=s.source=s.lifecycle=None
        s._pins=lambda:None  # Config loading/pins have separate real-loader tests.
        with PrivateDirectory(s.directory) as private:
            private.create('registry.json',canonical(s.state))
            try:
                s._start_review(private)
                assert s.job['session_id']!=old_job['session_id'] and not turns
                assert s.pool.dispatch_pending()==[s.job['id']]
                s.pool.threads[s.job['id']].join(timeout=300)
                assert not s.pool.threads[s.job['id']].is_alive()
                s._clean()
                result=s.lifecycle.publish(s.job['id']);s._clean()
                assert result['status']=='complete' and result['delivery_sha']==source.receipt['delivery_sha']
                proof=s._proof();assert len(proof['files'])==9
                assert len(turns)==1 and len(author_turns)==3 and public['posts']==public['pushes']==1
                assert {str(p):p.read_bytes() for p in old_paths}==snapshot
                assert f.board.get_ticket(f.human,ticket['id'])['ticket']['claimant'] is None
            finally:
                if s.pool is not None:s.pool.close()
    run_fixture(tmp_path,monkeypatch,linux_lock_observation,check)
