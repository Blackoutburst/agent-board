"""Strict public entry projection of qualification-only metadata."""
from copy import deepcopy

import pytest

from deploy.worker_pool import entry
from blackoutburst_agents.dev_manifest import DevelopmentError
from test_linux_qualification_diagnostic import observation
from test_worker_pool_planner_diagnostic import run_entry


def test_entry_exports_closed_metadata_and_preserves_false_and_null():
    error=DevelopmentError('linux_qualification_cleanup_unconfirmed')
    error._linux_backend_qualification_diagnostic=observation()
    projected=entry._backend_qualification_diagnostic(error)
    assert projected=={'status':'qualification_diagnostic','kind':'linux_backend',**observation()}
    error._linux_backend_qualification_diagnostic['native']['cleanup']['cleanup_confirmed']='/secret'
    assert projected['native']['cleanup']['cleanup_confirmed'] is False


@pytest.mark.parametrize('value',[None,{},[],{'case':'eof_running','stdout':'/secret'}])
def test_entry_refuses_incomplete_foreign_payload(value):
    error=DevelopmentError('linux_qualification_cleanup_unconfirmed')
    error._linux_backend_qualification_diagnostic=deepcopy(value)
    assert entry._backend_qualification_diagnostic(error) is None


def test_foreign_exception_type_cannot_export_even_valid_metadata():
    error=type('ForeignError',(DevelopmentError,),{})('linux_qualification_cleanup_unconfirmed')
    error._linux_backend_qualification_diagnostic=observation()
    assert entry._backend_qualification_diagnostic(error) is None


def test_actual_entry_keeps_reason_line_and_adds_only_closed_observation(run_entry):
    error=DevelopmentError('linux_qualification_cleanup_unconfirmed')
    error._linux_backend_qualification_diagnostic=observation()
    assert run_entry(error)==[
        {'status':'needs_review','reason':'linux_qualification_cleanup_unconfirmed'},
        {'status':'qualification_diagnostic','kind':'linux_backend',**observation()}]


def test_observer_failure_never_changes_entry_summary_or_exit(run_entry,monkeypatch):
    from blackoutburst_agents import linux_execution_qualification as qualification
    error=DevelopmentError('linux_qualification_cleanup_unconfirmed')
    error._linux_backend_qualification_diagnostic=observation()
    def unavailable(*_):raise OSError('PRIVATE diagnostic failure')
    monkeypatch.setattr(qualification,'qualification_diagnostic',unavailable)
    assert run_entry(error)==[{'status':'needs_review','reason':'linux_qualification_cleanup_unconfirmed'}]
