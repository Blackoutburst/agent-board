"""Explicit prior recovery selection is inert and closed."""
from copy import deepcopy
import uuid
import pytest
from blackoutburst_agents import review_recovery_service as module
from blackoutburst_agents.dev_manifest import DevelopmentError
from test_review_recovery_service import recovery,write
from test_development_queue import case


def test_v2_plan_preserves_the_prior_selection_without_source_or_rpc(recovery):
    f=recovery;value=f.value|dict(schema=module.SCHEMA_V2,prior_recovery_id=str(uuid.uuid4()),
        expected_prior_configuration_sha256='b'*64)
    write(f.path,value)
    plan=module.review_recovery_plan(f.path,root=f.root,owner_id='owner',board_endpoint=f.client.endpoint,max_seconds=300)
    assert plan['prior_recovery_id']==value['prior_recovery_id']
    assert plan['expected_prior_configuration_sha256']=='b'*64
    assert plan['activated'] is plan['launchable'] is plan['execution_qualified'] is False
    assert not (f.root/'review-recoveries').exists()


@pytest.mark.parametrize('fault',['missing_hash','missing_id','same_id','invalid_hash','extra','v1_extra'])
def test_v2_prior_selection_refuses_ambiguous_or_unpinned_history(recovery,fault):
    f=recovery;value=f.value|dict(schema=module.SCHEMA_V2,prior_recovery_id=str(uuid.uuid4()),
        expected_prior_configuration_sha256='b'*64)
    if fault=='missing_hash':del value['expected_prior_configuration_sha256']
    if fault=='missing_id':del value['prior_recovery_id']
    if fault=='same_id':value['prior_recovery_id']=value['recovery_id']
    if fault=='invalid_hash':value['expected_prior_configuration_sha256']='not-a-hash'
    if fault=='extra':value['prior_path']='/untrusted'
    if fault=='v1_extra':value['schema']=module.SCHEMA
    write(f.path,value)
    with pytest.raises(DevelopmentError):module.review_recovery_plan(f.path,root=f.root,owner_id='owner',board_endpoint=f.client.endpoint,max_seconds=300)
