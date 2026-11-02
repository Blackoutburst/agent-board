"""The actual standing tick discovers once and reopens the same Core next pass."""
from copy import deepcopy
import json

from blackoutburst_agents.dev_manifest import canonical,digest
from blackoutburst_agents.continuation_journal import ContinuationJournal
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.development_queue import DevelopmentQueueAdmission
from test_queued_continuation_service import queued_service,actual_service,linux_lock_observation,target


def test_real_tick_discovers_once_and_preserves_core_across_two_queued_generations(queued_service):
    f,root,_,ticket,_,calls,sequence,_=queued_service
    batch=json.loads((root/'config/batch.json').read_bytes());job=batch['jobs'][0]
    entry={k:deepcopy(v) for k,v in job.items() if k not in
        ('name','agent_id','project_id','ticket_id','connection_file','queue_admission')}
    entry['entry_id']=job['name'];entry['repository']={k:entry['repository'][k]
        for k in ('repository_id','repository','base_commit')}
    profile=f.board.get_agent_profile(f.human,f.agent)
    catalog=dict(schema='development-queue-catalog-v1',owner_id='owner',agent_id=f.agent,
        profile_version=profile['version'],board_endpoint='https://board.test/mcp',project_id=ticket['project_id'],
        connection_file=job['connection_file'],timeout_seconds=700,entries=[entry])
    path=root/'config/standing.json';path.write_bytes(canonical(catalog));path.chmod(0o600)
    class Discovery:
        endpoint=catalog['board_endpoint'];session_id='standing-discovery'
        connection_path=job['connection_file'];connection_pin=file_document(connection_path)[1]
        reads=0
        def registration(self,*,autonomous=False):
            return f.board.register_agent(f.raw,self.session_id,'Standing fixture',[],'codex',autonomous,'worker')
        def call(self,name,args):
            assert name in ('get_work','get_ticket','get_project')
            self.reads+=1
            return getattr(f.board,name)(f.board.actor_session(f.raw,self.session_id),**args)
    client=Discovery();workflow=str(root/'config/workflow.json')
    controller=DevelopmentQueueAdmission(root,client)
    plan=controller.tick_plan(path,workflow,checkpoint_after_actions=2)
    arguments=dict(approved_sha256=plan['approval_sha256'],executable=f.tools['codex'],cli_version='codex-cli 1.2.3',
        interpreter=f.tools['python'],unshare=f.tools['unshare'],gh_executable=f.tools['codex'],max_seconds=700,
        checkpoint_after_actions=2)
    first=controller.tick(path,workflow,**arguments)
    assert first['phase']=='paused_cycle',first
    reads=client.reads;released=f.board.get_ticket(f.human,ticket['id'])['ticket']
    assert released['status']=='todo' and released['claimant'] is None
    sequence.write_text(json.dumps([dict(schema=1,kind='read',path='one.py'),dict(schema=1,kind='read',path='one.py')]))
    # A new process/controller would have the same protected pointer/session;
    # no rediscovery or finite-batch execution is involved.
    second=DevelopmentQueueAdmission(root,client).tick(path,workflow,**arguments)
    assert second['phase']=='paused_cycle',second
    assert second['request_id']==first['request_id'] and second['logical_task_id']==first['logical_task_id']
    assert client.reads==reads and len(calls.read_text().splitlines())==4
    assert len(f.queue_claims)==2 and f.queue_claims[1]['expected_version']==released['version']
    with ContinuationJournal.open(target(root,first['logical_task_id'])/'logical') as journal:
        current=journal.snapshot()['state']
        assert current['cycle_seq']==2 and current['used_total']['turns']==4
    assert f.board.get_runtime(f.human)['active_count']==0
