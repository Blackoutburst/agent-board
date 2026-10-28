"""Identity-only cleanup of final inspection refusal; all effects synthetic."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from blackoutburst_agents.development_cleanup import _effect_receipt
from blackoutburst_agents.dev_broker import DevelopmentBroker
from blackoutburst_agents.dev_journal import decode
from blackoutburst_agents.dev_manifest import DevelopmentError, NamedTest
import test_development_broker as broker_fixture
import test_development_worker as worker_fixture


@pytest.fixture
def refused_final():
    f=worker_fixture.DevelopmentWorkerTests();f.setUp()
    try:
        f.actions=[f.action('final',summary='Premature final, no test proof.')]
        prepared=f.prepare();operation=prepared['operation_id']
        result=f.worker.run(operation)
        assert result['status']=='needs_review' and result['reason']=='final_test_proof_missing'
        yield f,operation,result
    finally:f.doCleanups()


def test_final_refusal_finishes_only_exact_runtime_without_promoting_or_replaying(refused_final):
    f,operation,result=refused_final
    before_op=f.journal.get(operation);before_ticket=f.read_ticket()
    planner_before=(f.broker_dir/'planner.json').read_bytes()
    broker_before=(f.broker_dir/'state.json').read_bytes()
    inference_count=len(f.inferences)
    assert not f.backend.calls
    assert f.board.get_runtime(f.human)['active_count']==1
    with patch.object(DevelopmentBroker,'__init__',side_effect=AssertionError('no broker construction')), \
         patch.object(f.backend,'qualified_for',side_effect=AssertionError('no qualification')), \
         patch.object(f.backend,'run',side_effect=AssertionError('no test')), \
         patch.object(f.backend,'lookup',side_effect=AssertionError('no native lookup')), \
         patch.object(f.policy,'validate_for',side_effect=AssertionError('no source/policy admission')):
        cleaned=f.worker.reconcile_cleanup(operation)
        replay=f.worker.reconcile_cleanup(operation)
    assert cleaned==replay and cleaned['status']=='needs_review' and cleaned['cleanup_confirmed'] is True
    assert cleaned['reason']=='development_identity_cleanup_only'
    assert f.board.get_runtime(f.human)['active_count']==0
    assert f.journal.get(operation)==before_op and f.read_ticket()==before_ticket
    assert (f.broker_dir/'planner.json').read_bytes()==planner_before
    assert (f.broker_dir/'state.json').read_bytes()==broker_before
    assert (f.root/'main.py').read_text()=='value = 1\n' and len(f.inferences)==inference_count
    tool=[r for r in f.board.get_runtime(f.human)['executions'] if r['provider']=='local_broker']
    assert len(tool)==1 and (tool[0]['state'],tool[0]['outcome'],tool[0]['cleanup_confirmed'])==('finished','blocked',True)
    state=decode(planner_before)
    assert state['phase']=='needs_review' and state['step']['tool_finished'] is False
    assert state['step']['receipt'] is None and not state['tests'] and state['review'] is None


@pytest.mark.parametrize('kind',['patch','test'])
def test_existing_operation_cannot_masquerade_as_final_inspection(refused_final,kind):
    f,_,_=refused_final
    planner=decode((f.broker_dir/'planner.json').read_bytes())
    manifest=decode((f.broker_dir/'manifest.json').read_bytes())
    broker=decode((f.broker_dir/'state.json').read_bytes())
    from blackoutburst_agents.dev_manifest import digest
    broker['operations'][digest(planner['step']['action_key'].encode())]={'kind':kind}
    with pytest.raises(DevelopmentError,match='^development_cleanup_identity_changed$'):
        _effect_receipt(planner['step'],manifest,broker)


def test_named_test_read_file_is_an_effect_despite_matching_runtime_label():
    f=broker_fixture.DevelopmentBrokerTests();f.setUp()
    try:
        # A new independent fixture manifest admits this valid colliding name.
        other=f.base/'named-read-file'
        from blackoutburst_agents.dev_manifest import prepare_manifest
        prepare_manifest(other,root=f.root,base_sha='a'*40,scope=f.scope,
            paths=['src/main.py'],resource='repo:fixture',authorize_scope=f.authorized,
            tests=[NamedTest('read_file',backend_id=f.backend.backend_id)])
        broker=DevelopmentBroker(other,authorize_scope=f.authorized,enabled=True,test_backend=f.backend)
        try:
            broker.run_test('same-step','read_file')
            state=decode((other/'state.json').read_bytes())
            step={'action':{'schema':1,'kind':'final','summary':'A forged kind is not an inspection proof.'},
                  'action_key':'same-step','receipt':None}
            with pytest.raises(DevelopmentError,match='^development_cleanup_identity_changed$'):
                _effect_receipt(step,broker.manifest,state)
        finally:broker.close()
    finally:f.doCleanups()


@pytest.mark.parametrize('kind',['read','patch','test'])
def test_other_actions_without_receipts_keep_missing_cleanup(refused_final,kind):
    f,_,_=refused_final
    planner=decode((f.broker_dir/'planner.json').read_bytes())
    manifest=decode((f.broker_dir/'manifest.json').read_bytes())
    broker=decode((f.broker_dir/'state.json').read_bytes())
    step=deepcopy(planner['step'])
    step['action']=({'schema':1,'kind':'read','path':'main.py'} if kind=='read' else
        {'schema':1,'kind':'test','name':'unit'} if kind=='test' else
        {'schema':1,'kind':'patch','path':'main.py','expected_sha256':manifest['files']['main.py']['sha256'],'content':'changed\n'})
    with pytest.raises(DevelopmentError,match='^development_cleanup_receipt_missing$'):
        _effect_receipt(step,manifest,broker)


def test_unknown_model_cleanup_still_prevents_finishing_final_runtime(refused_final):
    f,operation,_=refused_final
    original=f.store.turn_result
    def unconfirmed(turn_id):
        result=deepcopy(original(turn_id));result['verification']['cleanup_confirmed']=False
        return result
    with patch.object(f.store,'turn_result',side_effect=unconfirmed), \
         pytest.raises(DevelopmentError,match='^development_cleanup_receipt_missing$'):
        f.worker.reconcile_cleanup(operation)
    assert f.board.get_runtime(f.human)['active_count']==1
    tool=[r for r in f.board.get_runtime(f.human)['executions'] if r['provider']=='local_broker']
    assert len(tool)==1 and tool[0]['state']=='running' and tool[0]['cleanup_confirmed'] is False


def test_linux_final_non_dispatch_is_blocked_but_input_only_profile_is_not_cleanup():
    f=broker_fixture.DevelopmentBrokerTests();f.setUp()
    try:
        step={'action':{'schema':1,'kind':'final','summary':'No inspection receipt.'},
              'action_key':'final-step','receipt':None}
        broker={'schema':3,'linux_execution_plan_sha256':'a'*64,'operations':{}}
        configuration={'execution_profile':'linux-python-execution-v1',
                       'linux_execution_plan_sha256':'a'*64}
        assert _effect_receipt(step,f.broker.manifest,broker,configuration=configuration)=='blocked'
        with pytest.raises(DevelopmentError,match='^development_cleanup_identity_changed$'):
            _effect_receipt(step,f.broker.manifest,broker,configuration={**configuration,'linux_execution_plan_sha256':'b'*64})
        with pytest.raises(DevelopmentError,match='^development_input_preparation_is_not_cleanup_proof$'):
            _effect_receipt(step,f.broker.manifest,{'schema':2,'operations':{}},configuration=configuration)
    finally:f.doCleanups()
