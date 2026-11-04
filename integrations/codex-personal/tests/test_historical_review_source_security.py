"""Independent negative boundaries; local Board only, no model or public effects.

The isolated _gate tests exercise parsing, not admission: these test-only
uninitialized objects can never be passed to a worker or acquire authority.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from mcp_views import MCPViews
from test_profile_management import lead
from test_managed_claim_release import claim, enable, release
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical
from blackoutburst_agents.historical_review_source import HistoricalAggregateReviewSource, HistoricalAggregateReadView
from blackoutburst_agents.review_pool import DeliveryReviewPoolDriver, HistoricalPoolDeliveryTests
from blackoutburst_agents.git_publication import API, REMOTE


@pytest.mark.parametrize('change', [None, 'source_session_id', 'target_profile_id', 'source_profile_version',
    'expected_version', 'version', 'manager_profile_id'])
def test_gate_accepts_actual_compact_event_and_refuses_attribution_substitution(lead, tmp_path, change):
    s=lead; enable(s)
    task, target, actor, execution=claim(s)
    released=release(s, task, reason='Inspect '+('x'*1500), evidence='Recorded '+('y'*1500))
    b=s['b']; events=b.list_activity(s['human'], ticket_id=task['id'], limit=10, order='desc')
    event=next(e for e in events if e['event']=='managed_agent_claim_released')
    filters={'project_id':'','ticket_id':task['id'],'after':event['id']-1,'order':'asc'}
    page=MCPViews(b).page(s['actor'], 'list_activity',
        b.list_activity(s['actor'],ticket_id=task['id'],after=event['id']-1,limit=1,order='asc',_mcp_all=True),
        limit=1,filters=filters)
    assert page['items'][0]['detail']['source_session_id']==actor['session_id']
    assert 'recovery_evidence' not in page['items'][0]['detail']
    assert page['items'][0]['detail']['recovery_evidence_preview']
    if change is not None:
        value=page['items'][0]['detail'][change]
        page['items'][0]['detail'][change]=value+1 if type(value) is int else 'changed-'+value
    calls=[]
    def call(name,args):
        calls.append((name,deepcopy(args)))
        assert name=='list_activity'
        assert args=={'ticket_id':task['id'],'after':event['id']-1,'limit':1,'order':'asc'}
        return deepcopy(page)
    # Test-only parser handle. No constructor, registration, claim, Runtime,
    # publication, source or worker path is invoked.
    source=object.__new__(HistoricalAggregateReviewSource)
    source.prior=tmp_path/'prior'; source.prior_job=str(uuid.uuid4())
    folder=source.prior/'lifecycle'/source.prior_job; folder.mkdir(mode=0o700,parents=True)
    p=folder/'claimed.json';p.write_bytes(canonical(task));p.chmod(0o600)
    source.scope={'ticket_id':task['id'],'project_id':task['project_id'],'owner_id':s['human']['owner_id']}
    source.old_scope={'board_session_id':actor['session_id'],'agent_id':target['id']}
    source.old={'selection':{'registration':{'profile':{'version':event['detail']['source_profile_version']}}}}
    source.previous={'kind':'finished'}
    source.expected_ticket_version=released['version'];source.revocation_event_id=event['id']
    if change is None:
        result=source._gate(SimpleNamespace(call=call),released)
        assert result=={'event_id':event['id'],'ticket_version':released['version'],
            'manager_agent_id':s['profile']['id'],'source_session_id':actor['session_id']}
    else:
        with pytest.raises(DevelopmentError,match='historical_review_revocation_required'):
            source._gate(SimpleNamespace(call=call),released)
    assert len(calls)==1
    # The gate read never modifies actual Runtime cleanup or reclaims the task.
    assert b.get_runtime(s['human'])['executions'][0]['state']=='finished'
    assert b.get_ticket(s['human'],task['id'])['ticket']==released


@pytest.mark.parametrize('source',[{},SimpleNamespace(receipt={'status':'complete'})])
def test_hash_dto_cannot_be_driver_or_historical_test_source(source):
    with pytest.raises(DevelopmentError,match='review_pool_concrete_source_required'):
        DeliveryReviewPoolDriver._descriptor(source,str(uuid.uuid4()))
    with pytest.raises(DevelopmentError,match='review_pool_concrete_source_required'):
        HistoricalPoolDeliveryTests(source,str(uuid.uuid4()),{'status':'complete'})


def test_closed_historical_classes_cannot_be_subclassed():
    for base in (HistoricalAggregateReadView,HistoricalAggregateReviewSource):
        with pytest.raises(TypeError,match='Closed historical'):
            type('ForgedHistorical',(base,),{})


@pytest.mark.parametrize('method',['POST','PUT','PATCH','DELETE','HEAD'])
def test_read_transport_refuses_other_http_methods_without_launch(tmp_path,method):
    # Negative-only command validator; no actual reader construction or process.
    reader=object.__new__(HistoricalAggregateReadView)
    reader.root=tmp_path;reader.source=SimpleNamespace(operation_id=str(uuid.uuid4()))
    reader.git=Path('/protected/git');reader.gh=Path('/protected/gh')
    reader.request={'branch':'codex/existing-delivery'};reader.grant={'base_branch':'main'}
    with pytest.raises(DevelopmentError,match='historical_review_command_refused'):
        reader._confirm_launch([str(reader.gh),'api','--hostname','github.com','--method',method,API],
            cwd=reader.root/reader.source.operation_id,git=False)
    with pytest.raises(DevelopmentError,match='historical_review_command_refused'):
        reader._confirm_launch([str(reader.git),'push',REMOTE,'HEAD:main'],
            cwd=reader.root/reader.source.operation_id/'transport',git=True)
