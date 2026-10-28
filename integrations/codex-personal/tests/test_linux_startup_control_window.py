"""Linux startup lease timing with real codecs/pipes and synthetic children."""
import os
from unittest.mock import Mock,patch

import pytest

from blackoutburst_agents.dev_journal import PrivateDirectory
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical
from blackoutburst_agents.dev_test_supervisor import _ControlPipe,_send_control_window,CONTROL_TTL
import test_linux_execution_supervision as supervision_fixture


@pytest.fixture
def startup_case():
    case=supervision_fixture.LinuxSupervisionTests();case.setUp()
    try:yield case
    finally:case.doCleanups()


def test_prelaunch_local_validation_does_not_starve_an_allowed_first_renewal(startup_case):
    case=startup_case;calls=[];reader=[]
    def pins():
        # Each validation still executes. Scheduling one slow redundant local
        # preflight after the first window used to consume its renewal margin.
        calls.append(case.clock)
        case.clock+={2:.2,3:.8,4:.6}.get(len(calls),0)
    case.backend._pins=Mock(side_effect=pins)
    def launch(*_args,**_kwargs):
        job,state=case.header();case.helper=supervision_fixture.Helper(lambda:case.publish(False) if not case.helper.exited else None)
        case.addCleanup(case.helper.stdin.close)
        reader.append(_ControlPipe(case.helper.stdin.read,nonce=job['nonce'],initial=job['control'],clock=lambda:case.clock))
        state['phase']='namespace_launch_intent'
        with PrivateDirectory(case.directory) as private:private.replace('state.json',canonical(state))
        return case.helper
    def send(descriptor,window,**kwargs):
        _send_control_window(descriptor,window,clock=lambda:case.clock,**kwargs)
        case.sequence.append(window)
        assert (reader[0].wait_initial() if len(case.sequence)==1 else reader[0].authorized()) is True
        if len(case.sequence)==2:case.publish(False)
    with patch('blackoutburst_agents.linux_test_backend.subprocess.Popen',side_effect=launch), \
         patch('blackoutburst_agents.linux_test_backend._send_control_window',side_effect=send):
        case.run_job()
    assert len(calls)==4 and len(case.sequence)==2
    assert case.sequence[0]['started_at']==pytest.approx(100.2)
    assert case.sequence[0]['deadline']==pytest.approx(101.7)
    assert case.clock==pytest.approx(101.6)
    assert CONTROL_TTL==1.5


@pytest.mark.parametrize('stage',['preflight','collection'])
def test_failed_preflight_or_expired_first_collection_never_launches(startup_case,stage):
    case=startup_case;calls=[]
    def pins():
        calls.append(True)
        if stage=='preflight' and len(calls)==2:raise DevelopmentError('linux_execution_source_changed')
    case.backend._pins=Mock(side_effect=pins)
    if stage=='collection':
        def authorize():
            if case.context.authorize.call_count==2:case.clock+=1.5
            return True
        case.context.authorize.side_effect=authorize
    with patch('blackoutburst_agents.linux_test_backend.subprocess.Popen') as spawn,pytest.raises(DevelopmentError):
        case.run_job()
    spawn.assert_not_called()


def test_durable_job_write_still_consumes_initial_window_without_refresh(startup_case):
    case=startup_case;create=PrivateDirectory.create
    def delayed(private,name,data):
        result=create(private,name,data)
        if name=='state.json':case.clock+=1.5
        return result
    def launch(*_args,**_kwargs):
        case.helper=supervision_fixture.Helper(lambda:case.publish(False) if not case.helper.exited else None);case.addCleanup(case.helper.stdin.close)
        return case.helper
    with patch.object(PrivateDirectory,'create',delayed), \
         patch('blackoutburst_agents.linux_test_backend.subprocess.Popen',side_effect=launch), \
         patch('blackoutburst_agents.linux_test_backend._send_control_window',wraps=_send_control_window) as send:
        result=case.run_job()
    assert result['source_released'] is False and send.call_count==1
    assert case.context.before_source.call_count==0
    assert CONTROL_TTL==1.5
