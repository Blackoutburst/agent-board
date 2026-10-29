"""Closed classification only; no exception text, paths or raw RPC content."""
import json
import pytest
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.development_pool_delivery import (
    DevelopmentPoolResultDelivery,_delivery_failure,delivery_boundary_diagnostic,_DELIVERY_PHASE)
from blackoutburst_agents.work_journal import JournalError
from deploy.worker_pool.entry import _delivery_diagnostic


@pytest.mark.parametrize('error,category,code',[
    (BoardReadError('timeout'),'board_read_error','timeout'),
    (BoardReadError('PRIVATE_RPC_TEXT'),'board_read_error','unknown'),
    (JournalError('scope_busy'),'journal_error','scope_busy'),
    (JournalError('PRIVATE_JOURNAL_PATH'),'journal_error','unknown'),
    (OSError('PRIVATE_OS_PATH'),'os_error','os_error'),
    (KeyError('PRIVATE_RESULT'),'key_error','missing_key'),
    (ValueError('PRIVATE_DATA'),'value_error','invalid_value'),
    (TypeError('PRIVATE_TYPE'),'type_error','invalid_type'),
    (RuntimeError('PRIVATE_TEXT'),'runtime_error','runtime_error'),
    (Exception('PRIVATE_CUSTOM'),'unknown_error','unknown')])
def test_closed_classification_at_emission_and_entry(error,category,code):
    failure=_delivery_failure(error,'board_handoff')
    assert type(failure) is DevelopmentError and failure.code=='development_pool_delivery_unavailable'
    expected={'status':'delivery_diagnostic','kind':'development_result_delivery','phase':'board_handoff',
        'category':category,'code':code}
    assert _delivery_diagnostic(failure)==expected and 'PRIVATE_' not in json.dumps(expected)
    assert type(failure._development_delivery_diagnostic) is tuple


@pytest.mark.parametrize('value',[
    None,{},[],('board_handoff','board_read_error','PRIVATE_TEXT'),
    ('PRIVATE_PHASE','board_read_error','timeout'),('delivery','PRIVATE_TYPE','timeout'),
    ('delivery','os_error','timeout'),('delivery','board_read_error','timeout','extra')])
def test_entry_rejects_unclosed_or_inconsistent_attributes(value):
    error=DevelopmentError('development_pool_delivery_unavailable');error._development_delivery_diagnostic=value
    assert delivery_boundary_diagnostic(value) is None and _delivery_diagnostic(error) is None


def test_unknown_subclass_cannot_supply_exception_property_content():
    class Hostile(BoardReadError):
        def __getattribute__(self,name):
            if name in ('code','__dict__'):raise AssertionError('Never read unknown exception fields')
            return super().__getattribute__(name)
    error=Hostile('PRIVATE_CUSTOM')
    result=_delivery_diagnostic(_delivery_failure(error,'delivery'))
    assert result['category']=='unknown_error' and result['code']=='unknown'


def test_context_local_phase_is_reset_and_original_development_errors_preserved(monkeypatch):
    pipeline=object.__new__(DevelopmentPoolResultDelivery)
    def broken(*args,**kwargs):
        _DELIVERY_PHASE.set('board_handoff');raise BoardReadError('timeout')
    monkeypatch.setattr(pipeline,'_deliver',broken)
    assert _DELIVERY_PHASE.get()=='delivery'
    with pytest.raises(DevelopmentError) as caught:pipeline.deliver('unused',changes=[],commit_message='',pr_title='',pr_body='')
    assert _delivery_diagnostic(caught.value)['phase']=='board_handoff' and _DELIVERY_PHASE.get()=='delivery'
    error=DevelopmentError('development_claim_lost')
    def refused(*args,**kwargs):raise error
    monkeypatch.setattr(pipeline,'_deliver',refused)
    with pytest.raises(DevelopmentError) as caught:pipeline.deliver('unused',changes=[],commit_message='',pr_title='',pr_body='')
    assert caught.value is error and _delivery_diagnostic(error) is None


def test_entry_prints_only_validated_closed_delivery_projection(monkeypatch,capsys):
    from deploy.worker_pool import entry
    failure=_delivery_failure(KeyError('PRIVATE_MODEL_OR_PATH'),'board_handoff')
    def refuse(*_):raise failure
    monkeypatch.setattr(entry.p,'verify_release',refuse)
    assert entry.main(['--release','/synthetic/release','--owner','synthetic','run-workflow','--batch','/synthetic/batch'])==3
    rows=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(rows)==2 and rows[-1]==_delivery_diagnostic(failure)
    assert 'PRIVATE_' not in json.dumps(rows) and set(rows[-1])=={'status','kind','phase','category','code'}
