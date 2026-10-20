"""Protected entry wiring, with synthetic accounts and no network or model."""
import io
from contextlib import redirect_stdout
from unittest.mock import patch
import copy

import pytest
import test_isolated_host_entry as fixture
from deploy.isolated_host import owner_entry as entry
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.host_config import digest
from blackoutburst_agents.server_controller import launch_catalog


@pytest.fixture
def case():
    value=fixture.EntryTests('test_configure_creates_disabled_real_journal_without_network_or_model')
    value.setUp()
    try:yield value
    finally:value.tearDown()


def test_managed_prepare_is_offline_and_preserves_disabled_budget(case):
    output=io.StringIO()
    with patch('subprocess.Popen') as popen,patch('blackoutburst_agents.server_control_client.ServerControlClient.call') as rpc,\
            patch.object(entry,'manifest_id',return_value='b'*64),redirect_stdout(output):
        assert entry.execute(case.args('configure'),case.manifest)==0
        assert entry.execute(case.args('managed-prepare',host_id='host-id'),case.manifest)==0
    rpc.assert_not_called();popen.assert_not_called()
    with HostJournal(case.journal) as j:
        assert not j.control()['enabled'] and j.control()['remaining']==0
        assert j.managed_settings()['host_id']=='host-id'
        expected=launch_catalog(j.control()['configuration'],'b'*64)
        assert j.managed_settings()['catalog_sha256']==digest(expected)
    assert 'PRIVATE' not in output.getvalue()


def test_managed_run_uses_only_pinned_owner_connection_release_and_existing_journal(case):
    output=io.StringIO()
    with redirect_stdout(output),patch.object(entry,'manifest_id',return_value='b'*64):
        entry.execute(case.args('configure'),case.manifest)
        entry.execute(case.args('managed-prepare',host_id='host-id'),case.manifest)
        with patch('blackoutburst_agents.server_control_client.ServerControlClient.from_connection_file') as open_client,\
                patch('blackoutburst_agents.server_controller.ServerController') as controller:
            client=open_client.return_value.__enter__.return_value
            client.endpoint=case.config['board_endpoint']
            controller.return_value.run.return_value={'status':'stopped','model_calls_by_control_loop':0}
            assert entry.execute(case.args('managed-run'),case.manifest)==0
            open_client.assert_called_once_with(str(case.connection),session_id='fixed-server-control',timeout=5)
            assert controller.return_value.publish.call_count==1
            assert controller.return_value.run.call_count==1
    assert 'PRIVATE' not in output.getvalue()


def test_managed_run_does_not_create_missing_journal_or_broaden_configuration(case):
    with patch.object(entry,'manifest_id',return_value='b'*64):
        with pytest.raises(entry.PackageError):entry.execute(case.args('managed-run'),case.manifest)
    assert not case.journal.exists()


def prior_tool(case):
    manifest=copy.deepcopy(case.manifest)
    manifest.update(codex_version='codex-cli fixture',files={
        'tools/codex':{'size':4,'sha256':'a'*64},'tools/codex-resources/bwrap':{'size':4,'sha256':'b'*64}})
    previous=case.root/'releases'/('a'*64)
    config=copy.deepcopy(case.config);config['targets'][0]['executable']=str(previous/'tools/codex')
    return manifest,previous,config


def test_update_can_keep_exact_old_root_pinned_cli_without_rewriting_private_config(case):
    manifest,previous,config=prior_tool(case)
    with patch.object(entry,'RELEASES',case.root/'releases'),patch.object(entry,'verify_release',return_value=manifest) as verify:
        entry.check_configuration(config,manifest,case.release,case.username)
    verify.assert_called_once_with(previous)
    assert case.config['targets'][0]['executable']==str(case.codex)


@pytest.mark.parametrize('changed',['owners','python','board_endpoint','codex_version','tool','bwrap'])
def test_previous_executable_is_refused_if_any_owner_runtime_or_tool_pin_changed(case,changed):
    manifest,previous,config=prior_tool(case);old=copy.deepcopy(manifest)
    if changed in ('tool','bwrap'):
        k='tools/codex' if changed=='tool' else 'tools/codex-resources/bwrap';old['files'][k]['sha256']='c'*64
    else:old[changed]='different'
    with patch.object(entry,'RELEASES',case.root/'releases'),patch.object(entry,'verify_release',return_value=old):
        with pytest.raises(entry.PackageError,match='unpinned_host_executable'):
            entry.check_configuration(config,manifest,case.release,case.username)


def test_untrusted_old_release_failure_is_never_suppressed(case):
    manifest,previous,config=prior_tool(case)
    with patch.object(entry,'RELEASES',case.root/'releases'),patch.object(entry,'verify_release',side_effect=entry.PackageError('pin_changed')):
        with pytest.raises(entry.PackageError,match='pin_changed'):
            entry.check_configuration(config,manifest,case.release,case.username)
