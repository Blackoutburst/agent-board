"""Managed control races and budgets; synthetic files, no model/network/process."""
import copy
import sqlite3

import pytest

from blackoutburst_agents.host_config import HostError, validate
from blackoutburst_agents.host_journal import HostJournal
from test_host_journal import configuration


@pytest.fixture
def host(tmp_path):
    cfg=configuration(tmp_path);cfg['dispatch_budget']=3
    second={**cfg['targets'][0],'id':'second','agent_id':'agent-two',
            'board_session_id':'session-two','ticket_id':'ticket-two'}
    cfg['targets'].append(second)
    cfg=validate(cfg,bind_connections=True)
    with HostJournal(tmp_path/'host') as journal:
        journal.configure(cfg)
        journal.test_lock_directory=tmp_path/'locks'
        yield journal


def catalog(host):
    return [{'catalog_key':t['id'],'profile_id':t['agent_id'],'profile_version':t['profile_version'],
             'launchspec_sha256':('a' if t['id']=='primary' else 'b')*64,'mode':'ticket_text'}
            for t in host.control()['configuration']['targets']]


def prepared(host):
    host.prepare_managed('host-identity',catalog(host))
    host.bind_managed_instances({'primary':'instance-one','second':'instance-two'})


def command(target='primary',*,action='start',gen=1,identity='start-1',budget=2):
    return {'id':identity,'target_id':target,'instance_id':'instance-one' if target=='primary' else 'instance-two',
            'generation':gen,'action':action,'launchspec_sha256':('a' if target=='primary' else 'b')*64,
            'budget':0 if action=='stop' else budget}


def locked(host):return host.owner_lock(lock_directory=host.test_lock_directory)


def test_migration_requires_original_owner_lock_and_stopped_clean_host(host):
    with pytest.raises(HostError,match='host_lock_required'):prepared(host)
    with locked(host):
        host.enable(1)
        with pytest.raises(HostError,match='managed_requires_clean_stopped_host'):prepared(host)
        host.stop();prepared(host)
        assert host.managed() and not host.control()['enabled']
        with pytest.raises(HostError,match='managed_target_control_required'):host.enable(host.status()['revision'])


def test_uncertain_dispatch_blocks_management_even_when_disabled(host):
    with locked(host):
        activation=host.enable(1)['activation_id']
        host.reserve_dispatch('primary',{'id':'batch','request_id':'req','subscription_id':'sub'},activation)
        host.stop()
        with pytest.raises(HostError,match='managed_requires_clean_stopped_host'):prepared(host)
        assert not host.managed()


def test_replayed_start_never_renews_global_or_target_budget(host):
    with locked(host):
        prepared(host);c=command();host.queue_managed_command(c);host.apply_managed_command(c['id'])
        token=host.target_token('primary');control=host.status()
        host.queue_managed_command(c);host.apply_managed_command(c['id'])
        assert host.target_token('primary')==token and host.status()==control
        assert host.managed_target('primary')['remaining']==2


def test_same_key_changed_command_is_a_conflict(host):
    with locked(host):
        prepared(host);c=command();host.queue_managed_command(c)
        with pytest.raises(HostError,match='managed_command_conflict'):
            host.queue_managed_command({**c,'budget':1})


def test_second_connection_stop_preempts_old_queued_start(host):
    with locked(host):
        prepared(host);host.queue_managed_command(command())
        with HostJournal(host.directory) as control:
            control.queue_managed_command(command(action='stop',identity='stop-1'))
        old=host.apply_managed_command('start-1')
        assert old['state']=='superseded' and not host.target_selected('primary')
        host.apply_managed_command('stop-1')
        assert not host.control()['enabled']


def test_stop_one_target_does_not_stop_other_target(host):
    with locked(host):
        prepared(host)
        for target in ('primary','second'):
            c=command(target,identity='start-'+target);host.queue_managed_command(c);host.apply_managed_command(c['id'])
        token=host.target_token('primary');other=host.target_token('second')
        with HostJournal(host.directory) as control:
            control.queue_managed_command(command(action='stop',identity='stop-primary'))
        assert host.target_cancelled('primary',token)
        assert not host.target_cancelled('second',other)
        assert host.control()['enabled'] and host.target_selected('second')


def test_target_and_global_budget_charged_once_for_new_dispatch(host):
    with locked(host):
        prepared(host);c=command();host.queue_managed_command(c);host.apply_managed_command(c['id'])
        active=host.control()['activation'];batch={'id':'batch','request_id':'req','subscription_id':'sub'}
        first=host.reserve_dispatch('primary',batch,active)
        assert host.reserve_dispatch('primary',batch,active)==first
        assert host.managed_target('primary')['remaining']==1
        assert host.control()['remaining']==2


def test_stopped_target_cannot_reserve_a_new_dispatch_even_if_host_remains_enabled(host):
    with locked(host):
        prepared(host)
        for target in ('primary','second'):
            c=command(target,identity='start-'+target);host.queue_managed_command(c);host.apply_managed_command(c['id'])
        host.stop_target('primary')
        with pytest.raises(HostError,match='host_target_stopped'):
            host.reserve_dispatch('primary',{'id':'batch','request_id':'req','subscription_id':'sub'},host.control()['activation'])
        assert host.control()['remaining']==3


def test_global_stop_never_implicitly_reawakens_old_target(host):
    with locked(host):
        prepared(host)
        for target in ('primary','second'):
            c=command(target,identity='start-'+target);host.queue_managed_command(c);host.apply_managed_command(c['id'])
        host.stop()
        next_command=command(gen=2,identity='start-new',budget=1)
        host.queue_managed_command(next_command);host.apply_managed_command(next_command['id'])
        assert host.target_selected('primary') and not host.target_selected('second')
        assert host.managed_target('primary')['remaining']==1


def test_restart_invalidates_current_activation_but_cannot_advance_past_uncertain_dispatch(host):
    with locked(host):
        prepared(host);c=command();host.queue_managed_command(c);host.apply_managed_command(c['id'])
        token=host.target_token('primary')
        host.reserve_dispatch('primary',{'id':'batch','request_id':'req','subscription_id':'sub'},host.control()['activation'])
        restart=command(action='restart',gen=2,identity='restart-1',budget=1)
        with HostJournal(host.directory) as control:control.queue_managed_command(restart)
        assert host.target_cancelled('primary',token)
        with pytest.raises(HostError,match='host_work_pending'):host.apply_managed_command(restart['id'])
        assert not host.control()['enabled']
        assert host.managed_target('primary')['remaining']==1


def test_cold_reopen_preserves_commands_and_immutable_owner_binding(host):
    with locked(host):
        prepared(host);host.queue_managed_command(command())
    with HostJournal(host.directory) as reopened:
        with reopened.owner_lock(lock_directory=host.test_lock_directory):
            assert reopened.managed()
            reopened.prepare_managed('host-identity',catalog(host))
            reopened.bind_managed_instances({'primary':'instance-one','second':'instance-two'})
            reopened.apply_managed_command('start-1')
            with pytest.raises(HostError,match='managed_catalog_conflict'):
                reopened.prepare_managed('different-host',catalog(host))
            assert reopened.target_selected('primary')


@pytest.mark.parametrize('changes',[
    {'generation':True},{'generation':-1},{'budget':True},{'budget':0},{'budget':21},
    {'action':'shell'},{'target_id':'foreign'},{'instance_id':'foreign'},
    {'launchspec_sha256':'c'*64},{'extra':'PRIVATE_COMMAND'},
])
def test_foreign_or_malformed_controls_never_change_activation(host,changes):
    with locked(host):
        prepared(host);before=host.status()
        with pytest.raises(HostError):host.queue_managed_command({**command(),**changes})
        assert host.status()==before and not host.target_selected('primary')
