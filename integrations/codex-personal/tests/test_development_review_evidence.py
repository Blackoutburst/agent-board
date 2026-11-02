"""Actual sealed proof metadata; transport/native observations stay synthetic."""
import pytest

from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
from blackoutburst_agents.delivery_review_material import validate_review_evidence
import test_development_delivery as finite_fixture
from test_development_aggregate_delivery import aggregate_fixture, linux_lock_observation

V1='controller-test-evidence-v1'
V2='controller-test-evidence-v2'


@pytest.fixture
def delivery():
    f=finite_fixture.DevelopmentDeliveryTests();f.setUp()
    try:
        f.ready()
        yield f
    finally:f.doCleanups()


def test_finite_metadata_is_derived_from_actual_pinned_plan_broker_native_and_runtime(delivery):
    f=delivery
    original={p:p.read_bytes() for directory in (f.broker_dir,f.runtime.directory,f.store.state_dir,f.journal.directory)
        for p in directory.iterdir() if p.is_file()}
    calls=len(f.backend.calls)
    evidence=f.proof.evidence(f.snapshot)
    review=validate_review_evidence(evidence)
    assert review==evidence['review'] and review['kind']=='development'
    assert review['source_snapshot_sha256']==evidence['snapshot_sha256']==digest(canonical(f.snapshot))
    planner=decode((f.broker_dir/'planner.json').read_bytes())
    broker=decode((f.broker_dir/'state.json').read_bytes())
    assert review['planner_snapshot_sha256']==planner['review']['snapshot_sha256']
    assert review['planner_snapshot_sha256']!=review['source_snapshot_sha256']
    row=review['tests'][0];operation=broker['operations'][row['operation_id']]
    job=decode((f.native_directory/'job.json').read_bytes())
    assert row['test_receipt_sha256']==digest(canonical(operation['receipt']))
    assert row['native_receipt_sha256']==digest((f.native_directory/'native-receipt.json').read_bytes())
    assert row['script_sha256']==job['script_sha256']==digest(b'synthetic-script')
    assert (row['timeout_seconds'],row['max_output_bytes'])==(job['timeout_seconds'],job['max_output_bytes'])
    assert row['result']=='passed' and row['native_exit_code']==0 and row['cleanup_confirmed'] is row['source_released'] is True
    assert row['output_bytes']==operation['receipt']['output_bytes'] and row['output_sha256']==operation['receipt']['output_sha256']
    assert 'synthetic-script' not in canonical(review).decode()
    seal=f.deliveries.finalize(operation_id=f.operation,snapshot_sha256=evidence['snapshot_sha256'],
        test_evidence=evidence,message='Sealed real review metadata')
    assert seal['test_evidence']==evidence
    assert {p:p.read_bytes() for p in original}==original and len(f.backend.calls)==calls


def test_legacy_v1_hashes_and_seal_remain_exact_after_v2_recalculation(delivery):
    f=delivery
    legacy=f.proof.evidence(f.snapshot,schema=V1)
    assert set(legacy)=={'schema','snapshot_sha256','receipts'}
    # This is the published v1 hash contract, not a v2 projection stripped of
    # its review field. The test uses the real validated documents/receipts.
    op=f.proof._operation();manifest,broker,planner,policy,docs=f.proof._documents(op)
    runtime=f.proof._runtime(op,manifest,planner,policy)
    binding=dict(schema='development-tested-git-binding-v1',operation_id=f.operation,
        configuration_sha256=digest(canonical(f.proof.configuration)),manifest_sha256=f.proof.configuration['manifest_hash'],
        planner_document_sha256=digest(docs['planner.json']),broker_document_sha256=digest(docs['state.json']),
        planner_snapshot_sha256=planner['review']['snapshot_sha256'],git_snapshot_sha256=legacy['snapshot_sha256'],runtime_cleanup_sha256=runtime)
    rows=[]
    for name,p in sorted(planner['review']['tests'].items()):
        native=broker['linux_execution'][p['operation_id']]['native_receipt_sha256']
        rows.append(dict(name=name,sha256=digest(canonical({**binding,'test_receipt_sha256':p['receipt_sha256'],'native_receipt_sha256':native}))))
    assert legacy['receipts']==rows
    seal=f.deliveries.finalize(operation_id=f.operation,snapshot_sha256=legacy['snapshot_sha256'],test_evidence=legacy,message='Historical v1')
    assert f.proof.evidence(f.snapshot)['schema']==V2
    assert f.proof(operation_id=f.operation,scope=f.manifest_scope,snapshot=f.snapshot,evidence=legacy) is True
    with f.workspaces._locked(f.operation) as (workspace,_),f.deliveries._record(f.deliveries.delivery_root,f.operation) as (private,_):
        assert f.deliveries._sealed(f.operation,workspace,private)['seal']==seal


@pytest.mark.parametrize('fault',['output_hash','script_hash','snapshot','missing_review','extra_private','schema_downgrade','stripped_downgrade'])
def test_forged_or_downgraded_v2_evidence_cannot_be_sealed(delivery,fault):
    f=delivery;evidence=f.proof.evidence(f.snapshot)
    if fault=='output_hash':evidence['review']['tests'][0]['output_sha256']='a'*64
    elif fault=='script_hash':evidence['review']['tests'][0]['script_sha256']='b'*64
    elif fault=='snapshot':evidence['review']['source_snapshot_sha256']='c'*64
    elif fault=='missing_review':evidence.pop('review')
    elif fault=='extra_private':evidence['review']['tests'][0]['output']='PRIVATE OUTPUT MUST NOT BE FORWARDED'
    else:
        evidence['schema']=V1
        if fault=='stripped_downgrade':evidence.pop('review')
    before=(f.deliveries.delivery_root/f.operation/'delivery.json').read_bytes()
    with pytest.raises(DevelopmentError):
        f.deliveries.finalize(operation_id=f.operation,snapshot_sha256=digest(canonical(f.snapshot)),test_evidence=evidence,message='Refused')
    assert (f.deliveries.delivery_root/f.operation/'delivery.json').read_bytes()==before


def test_script_job_drift_is_refused_without_decoding_or_running_verifier(delivery):
    f=delivery
    # v1 never published this new field. Its old exact proof path stays intact;
    # v2 additionally binds the projected script identity to the pinned plan.
    legacy=f.proof.evidence(f.snapshot,schema=V1)
    with PrivateDirectory(f.native_directory) as private:
        job=decode(private.read('job.json',64*1024));job['script_sha256']='a'*64;private.replace('job.json',canonical(job))
    assert f.proof.evidence(f.snapshot,schema=V1)==legacy
    with pytest.raises(DevelopmentError):f.proof.evidence(f.snapshot)


@pytest.mark.parametrize('schema',[V1,V2])
def test_two_generation_aggregate_preserves_sealed_final_test_origin_and_v1_hashes(tmp_path,monkeypatch,linux_lock_observation,schema):
    def check(f,aggregate,worker,directory,turns):
        real=aggregate.proof.evidence
        if schema==V1:
            # Produce a genuine old-format seal, then remove this fixture seam
            # before verifying it with the new default-v2 implementation.
            def old(snapshot,**kwargs):return real(snapshot,schema=kwargs.get('schema',V1))
            monkeypatch.setattr(aggregate.proof,'evidence',old)
        seal=aggregate.seal(message='Review metadata composition')
        monkeypatch.setattr(aggregate.proof,'evidence',real)
        snapshot=decode((aggregate.paths['deliveries']/aggregate.operation_id/'delivery.json').read_bytes())['snapshot']
        assert seal['test_evidence']['schema']==schema
        assert aggregate.proof(operation_id=aggregate.operation_id,scope=snapshot['request']['scope'],snapshot=snapshot,evidence=seal['test_evidence']) is True
        assert aggregate.proof.evidence(snapshot,schema=schema)==seal['test_evidence']
        assert aggregate.proof.evidence(snapshot)['schema']==V2
        if schema==V2:
            review=validate_review_evidence(seal['test_evidence'])
            assert review['kind']=='aggregate' and review['original_public_base']==f.commit
            assert aggregate.final_proof.workspaces.grant['base_commit']!=f.commit
            assert review['source_snapshot_sha256']!=seal['snapshot_sha256']
            final=aggregate._final_evidence(aggregate._source_capture())
            assert review['source_snapshot_sha256']==final['snapshot_sha256']
            assert review['tests']==final['review']['tests']
            assert review['runtime_cleanup_sha256']==final['review']['runtime_cleanup_sha256']
            assert review['lineage_sha256']==digest(canonical(aggregate._lineage()))
        assert len(turns)==3
        assert aggregate.seal(message='Review metadata composition')=={**seal,'historical':True}
    aggregate_fixture(tmp_path,monkeypatch,linux_lock_observation,check)
