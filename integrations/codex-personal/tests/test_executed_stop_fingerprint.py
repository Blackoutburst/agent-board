"""Real SQLite/Board Stop metadata before executed disposition; fake transport only."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest
import test_catalog_v2_independent_real_board as real_fixture
from blackoutburst_agents.host_config import HostError,digest
from blackoutburst_agents.catalog_source_transition import CatalogSourceTransition,EXECUTED_SCHEMA


def controls(host):
    return {table:[dict(row) for row in host._db.execute('SELECT * FROM '+table+' ORDER BY id')]
            for table in ('managed_commands','managed_effects')}


class PendingStopFixture(real_fixture.Fixture):
    __test__=False
    variant='accepted'

    def before_disposition(self):
        self.stop=self.human_command('stop','stop-before-executed-disposition-v2')
        i=self.instance();c=self.stop
        args={'host_id':self.remote['id'],'command_id':c['id'],'expected_revision':c['revision'],
              'idempotency_key':'managed-ack-'+c['id']}
        with self.locked():
            self.host.queue_managed_command({'id':c['id'],'target_id':'primary','instance_id':i['id'],
                **{k:c[k] for k in ('generation','action','launchspec_sha256','budget')}})
            if self.variant!='queued':
                effect=self.host.begin_managed_effect('ack-'+c['id'],'ack_instance_command',args)
                result=self.server.call('ack_instance_command',effect['arguments'])
                if self.variant!='lost_ack':self.host.finish_managed_effect(effect['id'],result)
            if self.variant=='pending_report':
                i=self.instance()
                report={'host_id':self.remote['id'],'instance_id':i['id'],
                    'expected_observation_revision':i['observation_revision'],'generation':i['generation'],
                    'launchspec_sha256':i['launchspec_sha256'],'command_id':i['command_id'],
                    'state':'stopped','cleanup_confirmed':True,'reason':'stopped'}
                key='report-'+digest(report);report['idempotency_key']=key
                self.host.begin_managed_effect(key,'report_instance_state',report)
            # Reproduce the actual ordering: queue/remote ACK succeeded, applying
            # Stop cannot settle while this genuine child dispatch needs review.
            with pytest.raises(HostError,match='host_work_pending'):
                self.host.apply_managed_command(c['id'])
            with pytest.raises(HostError,match='message_abandon_unsettled'):
                self.host._abandon_host_fingerprint('primary',self.host.dispatches()[0]['batch_id'])
            self.controls_before=controls(self.host)
            self.runtime_before=self.remote_rows()
            self.local_runtime_before=[tuple(r) for r in self.message_runtime.db.execute('SELECT * FROM executions')]
            self.before_stop_instance=deepcopy(self.instance())
            self.before_stop_control=deepcopy(self.host.control())


@pytest.fixture(params=('queued','accepted','lost_ack','pending_report'))
def pending_stop(request):
    f=PendingStopFixture();f.variant=request.param;f.setUp()
    try:yield f
    finally:f.doCleanups()


def test_disposition_pins_but_does_not_apply_ack_or_attest_pending_stop(pending_stop):
    f=pending_stop
    assert f.closed['phase']=='complete'
    assert controls(f.host)==f.controls_before
    assert f.host.control()==f.before_stop_control
    assert f.remote_rows()==f.runtime_before
    assert [tuple(r) for r in f.message_runtime.db.execute('SELECT * FROM executions')]==f.local_runtime_before
    assert f.instance()==f.before_stop_instance
    assert f.instance()['current_command']['status']==('queued' if f.variant=='queued' else 'accepted')
    assert f.instance()['cleanup_confirmed'] is False
    assert f.host.managed_command(f.stop['id'])['state']=='queued'
    assert not f.host.control()['enabled'] and f.host.control()['remaining']==0
    assert not f.host.managed_target('primary')['enabled']
    assert f.calls==1 and f.server.calls==[]
    with f.locked():
        assert f.admin.status(intent_id=f.closed['intent_id'])==f.closed


def test_catalog_v2_settles_preexisting_real_stop_only_after_child_closure(pending_stop):
    f=pending_stop;h=f.host
    ledger=[tuple(r) for r in h._db.execute('SELECT * FROM host_message_dispositions')]
    children={name:[tuple(r) for r in db.execute('SELECT * FROM '+name)] for db,name in (
        (f.message_runs._db,'works'),(f.message_runs._db,'turns'),(f.message_runtime.db,'executions'),
        (f.messages.db,'operations'),(f.messages.db,'readers'),(f.messages.db,'deliveries'))}
    checks=[]
    def guard():
        assert f.closed['phase']=='complete'
        checks.append(f.instance()['current_command']['status'])
        return f.stopped_children()
    c=CatalogSourceTransition(h,f.server,source_guard=lambda old,new,cfg:
        (old,new,cfg)==('b'*64,'d'*64,h.control()['hash']),stopped_guard=guard,executed_disposition=f.admin)
    with f.locked():
        p=c.plan(old_release_sha256='b'*64,new_release_sha256='d'*64,
                 request_key='real-preexisting-stop-source-transition-v2')
        assert p['schema']==EXECUTED_SCHEMA and p['closures']==[]
        result=c.resolve(p)
        assert result['catalog_version']==2
        assert c.status('source-'+digest(p['request_key']))['phase']=='complete'
        assert h.managed_command(f.stop['id'])['state']=='applied'
        assert h.control()['remaining']==0 and not h.control()['enabled']
        assert h.managed_target('primary')['remaining']==0 and not h.managed_target('primary')['enabled']
        assert [tuple(r) for r in h._db.execute('SELECT * FROM host_message_dispositions')]==ledger
    assert children=={name:[tuple(r) for r in db.execute('SELECT * FROM '+name)] for db,name in (
        (f.message_runs._db,'works'),(f.message_runs._db,'turns'),(f.message_runtime.db,'executions'),
        (f.messages.db,'operations'),(f.messages.db,'readers'),(f.messages.db,'deliveries'))}
    assert checks and f.remote_rows()==f.runtime_before and f.calls==1
    i=f.instance()
    assert i['current_command']['id']==f.stop['id'] and i['current_command']['status']=='complete'
    assert i['desired_state']=='stopped' and i['observed_state']=='unknown' and i['cleanup_confirmed'] is False
    assert {name for name,args in f.server.calls}<={'poll_instance_commands','ack_instance_command','report_instance_state','replace_server_catalog'}


@pytest.fixture
def unclosed():
    import test_message_executed_disposition as fixture
    f=fixture.ExecutedDispositionTests();f.setUp()
    try:yield f
    finally:f.doCleanups()


def queue_local_stop(f,identity='synthetic-disposition-stopped-command'):
    with f.locked():
        target=f.host.managed_target('primary')
        return f.host.queue_managed_command({'id':identity,'target_id':'primary','instance_id':target['instance_id'],
            'generation':target['generation']+1,'action':'stop','launchspec_sha256':target['launchspec_sha256'],'budget':0})


def test_empty_control_fingerprint_preserves_existing_historical_digest(unclosed):
    f=unclosed
    with f.locked():
        batch=f.host.dispatches()[0]['batch_id']
        assert f.host._executed_fingerprint('primary',batch)==f.host._abandon_host_fingerprint('primary',batch)


@pytest.mark.parametrize('phase',('work_disposed','message_disposed','complete'))
def test_fresh_stop_snapshot_survives_cold_resume_without_implicit_control_work(unclosed,phase):
    f=unclosed;queue_local_stop(f)
    original=f.host._executed_advance;before=controls(f.host);remote=f.remote_rows()
    def crash(record,next_phase,**kwargs):
        if next_phase==phase:raise SystemExit('synthetic lost return before Host checkpoint')
        return original(record,next_phase,**kwargs)
    with f.locked():
        p=f.plan()
        with patch.object(f.host,'_executed_advance',side_effect=crash):
            with pytest.raises(SystemExit):f.admin.apply(p)
    f.reopen()
    with f.locked():
        st=f.admin.status(request_key=p['request_key'])
        assert f.admin.resume(st['intent_id'],expected_revision=st['revision'])['phase']=='complete'
        assert controls(f.host)==before
        assert f.host.managed_command('synthetic-disposition-stopped-command')['state']=='queued'
    assert f.remote_rows()==remote and f.calls==1 and len(f.observer.calls)==2


def test_stop_snapshot_change_after_intent_refuses_retry_without_observation(unclosed):
    f=unclosed;command=queue_local_stop(f);f.observer.failure=('before','lookup_execution')
    with f.locked():
        p=f.plan()
        with pytest.raises(HostError):f.admin.apply(p)
        st=f.admin.status(request_key=p['request_key']);calls=deepcopy(f.observer.calls)
        # A second genuine local Stop is not adopted into the old request.
        command2={**json.loads(command['payload']),'id':'synthetic-newer-stop-after-intent',
                  'generation':command['generation']+1}
        f.host.queue_managed_command(command2)
        rows=f.state();remote=f.remote_rows();f.observer.failure=None
        with pytest.raises(HostError):f.admin.resume(st['intent_id'],expected_revision=1,retry_observation=True)
        assert f.state()==rows and f.remote_rows()==remote and f.observer.calls==calls
        assert f.host._executed_record(intent_id=st['intent_id'])['phase']=='intent'


@pytest.mark.parametrize('action',('start','restart'))
def test_any_unsettled_launch_command_stays_refused_without_network(unclosed,action):
    f=unclosed
    with f.locked():
        target=f.host.managed_target('primary')
        f.host.queue_managed_command({'id':'synthetic-unsettled-'+action,'target_id':'primary',
            'instance_id':target['instance_id'],'generation':target['generation']+1,'action':action,
            'launchspec_sha256':target['launchspec_sha256'],'budget':1})
        before=f.state()
        with pytest.raises(HostError):f.plan()
        assert f.state()==before and f.observer.calls==[] and f.calls==1
