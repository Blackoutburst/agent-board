"""Independent tests for fresh RunStore issuance and lock lifetime."""
import pytest


def test_stale_real_entry_is_refused_after_lock_reacquisition():
    from test_runtime_guard_security import RuntimeGuardSecurityTests
    from blackoutburst_agents import runtime_uninvoked
    from blackoutburst_agents.runtime_guard import RuntimeControlError
    case=RuntimeGuardSecurityTests();case.setUp()
    try:
        guard=case.guard()
        with case.store.work_lock(case.work['id']):
            entry=case.store.begin_turn(case.work['id'],'fresh','synthetic',_runtime_guard=guard)
            proof=entry['_runtime_entry']
        with case.store.work_lock(case.work['id']):
            with pytest.raises(RuntimeControlError,match='runtime_uninvoked_proof_invalid'):
                runtime_uninvoked.before(guard,case.store.get_work(case.work['id']),entry['turn']['id'],proof)
        assert not case.client.calls and not case.journal.list_status()
        assert case.model_calls==0
    finally:case.doCleanups()


def test_faked_begin_turn_dictionary_cannot_issue_a_cleanup_entry():
    from test_runtime_guard_security import RuntimeGuardSecurityTests
    from blackoutburst_agents import runtime_uninvoked
    from blackoutburst_agents.runtime_guard import RuntimeControlError
    case=RuntimeGuardSecurityTests();case.setUp()
    try:
        guard=case.guard()
        with case.store.work_lock(case.work['id']):
            actual=case.store.begin_turn(case.work['id'],'fresh','synthetic')
            assert '_runtime_entry' not in actual
            fake={'cached':False,'turn':actual['turn'],'_runtime_entry':{'store':case.store,'guard':guard,'fresh':True}}
            with pytest.raises(RuntimeControlError,match='runtime_uninvoked_proof_invalid'):
                runtime_uninvoked.before(guard,case.store.get_work(case.work['id']),fake['turn']['id'],fake['_runtime_entry'])
        assert not case.client.calls and not case.journal.list_status()
        assert case.model_calls==0
    finally:case.doCleanups()


def test_run_without_runtime_guard_cannot_borrow_an_entry_during_transport():
    from test_runtime_guard_security import RuntimeGuardSecurityTests
    from blackoutburst_agents import runtime_uninvoked
    from blackoutburst_agents.runner import PersonalRunner
    from blackoutburst_agents.runtime_guard import RuntimeControlError
    case=RuntimeGuardSecurityTests();case.setUp()
    def transport(settings,prompt,workspace,**callbacks):
        turn=case.store.status(case.work['id'])['turns'][0]
        with pytest.raises(RuntimeControlError,match='runtime_uninvoked_proof_invalid'):
            runtime_uninvoked.before(case.guard(),case.store.get_work(case.work['id']),turn['id'])
        assert not case.client.calls and not case.journal.list_status()
        return case.result(callbacks)
    try:
        result=PersonalRunner(case.store,transport).run(case.work['id'],'without-runtime','synthetic')
        assert result['result']['status']=='completed' and case.model_calls==1
        assert not case.client.calls and not case.journal.list_status()
    finally:case.doCleanups()


def test_real_issued_entry_is_consumed_before_transport_even_before_pid_callback():
    from test_runtime_guard_security import RuntimeGuardSecurityTests
    from blackoutburst_agents import runtime_uninvoked
    from blackoutburst_agents.runner import PersonalRunner
    from blackoutburst_agents.runtime_guard import RuntimeControlError
    from blackoutburst_agents.runtime_journal import RuntimeJournal
    case=RuntimeGuardSecurityTests();case.setUp();entries=[]
    original=case.store.begin_turn
    def capture(*args,**kwargs):
        entry=original(*args,**kwargs);entries.append(entry)
        return entry
    case.store.begin_turn=capture
    guard=case.guard()
    def transport(settings,prompt,workspace,**callbacks):
        entry=entries[0];proof=entry['_runtime_entry'];turn=entry['turn']['id']
        assert case.store._turn(turn)['pid'] is None
        assert proof.entered is True
        assert case.execution()['state']=='running'
        replay=RuntimeJournal(case.root/'fresh-runtime-before-on-started')
        original_journal=guard.journal;guard.journal=replay
        calls=len(case.client.calls)
        try:
            with pytest.raises(RuntimeControlError,match='runtime_uninvoked_proof_invalid'):
                runtime_uninvoked.before(guard,case.store.get_work(case.work['id']),turn,proof)
            assert not replay.list_status()
            assert len(case.client.calls)==calls and case.execution()['state']=='running'
        finally:
            guard.journal=original_journal;replay.close()
        return case.result(callbacks)
    try:
        runner=PersonalRunner(case.store,transport,runtime_guard=guard)
        result=runner.run(case.work['id'],'same','synthetic')
        assert result['result']['status']=='completed' and case.model_calls==1
        assert runner.run(case.work['id'],'same','synthetic')['cached'] is True
        assert '_runtime_entry' in entries[0] and '_runtime_entry' not in entries[1]
        assert case.execution()['state']=='finished' and case.execution()['outcome']=='completed'
    finally:case.doCleanups()


@pytest.mark.parametrize('field',['model','workspace','executable','owner_id','agent_id','project_id','ticket_id'])
def test_real_fresh_entry_cannot_substitute_its_immutable_work(field):
    from test_runtime_guard_security import RuntimeGuardSecurityTests
    from blackoutburst_agents import runtime_uninvoked
    from blackoutburst_agents.runtime_guard import RuntimeControlError
    case=RuntimeGuardSecurityTests();case.setUp()
    try:
        guard=case.guard()
        with case.store.work_lock(case.work['id']):
            entry=case.store.begin_turn(case.work['id'],'fresh','synthetic',_runtime_guard=guard)
            work={**case.store.get_work(case.work['id']),field:'substituted'}
            with pytest.raises(RuntimeControlError,match='runtime_uninvoked_proof_invalid'):
                runtime_uninvoked.before(guard,work,entry['turn']['id'],entry['_runtime_entry'])
            assert not case.client.calls and not case.journal.list_status()
        assert case.model_calls==0
    finally:case.doCleanups()
