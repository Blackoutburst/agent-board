"""Independent counterexample: a new local journal is not proof of a new remote turn."""
import uuid


def test_recreated_runtime_journal_cannot_close_a_running_transport():
    from test_runtime_guard_security import RuntimeGuardSecurityTests
    from blackoutburst_agents.runtime_guard import RuntimeGuard,RuntimeControlError
    from blackoutburst_agents.runtime_journal import RuntimeJournal
    from blackoutburst_agents import runtime_uninvoked
    case=RuntimeGuardSecurityTests();case.setUp();observed={}
    def transport(settings,prompt,workspace,**callbacks):
        callbacks['on_started'](999999999)
        session=str(uuid.uuid4());callbacks['on_session'](session)
        case.model_calls+=1
        turn=case.store.status(case.work['id'])['turns'][0]
        observed['before']=case.execution()['state']
        # The original runner remains inside its transport, with its work lock.
        # A caller opens a distinct empty RuntimeJournal using the same scope.
        replay=RuntimeJournal(case.root/'fresh-runtime-replay')
        try:
            guard=RuntimeGuard(replay,case.client,case.scope)
            try:runtime_uninvoked.before(guard,case.store.get_work(case.work['id']),turn['id'])
            except (RuntimeControlError,TypeError):pass
            observed['during_transport']=case.execution()['state']
            rows=replay.list_status()
            observed['local_phase']=rows[0]['phase'] if rows else None
        finally:replay.close()
        return {'status':'completed','session_id':session,'text':'synthetic',
            'verification':{'cleanup_confirmed':True,'tool_free':True}}
    try:
        try:case.run_turn(transport=transport)
        except RuntimeControlError:observed['finish_refused']=True
        assert observed['before']=='running' and case.model_calls==1
        assert observed['during_transport']=='running',observed
        assert observed.get('finish_refused') is not True,observed
    finally:case.doCleanups()
