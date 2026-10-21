"""Protected owner factory wiring with synthetic paths and account checks."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from unittest.mock import patch

import pytest

from deploy.isolated_host import owner_entry as entry
from blackoutburst_agents.host_config import HostError
from blackoutburst_agents.host_journal import HostJournal
from test_managed_owner_entry import case


def configure_messages(case):
    case.config['schema_version']=2
    target=case.config['targets'][0]
    for key in ('project_id','ticket_id','after'):del target[key]
    target.update(thread_ids=['fixed-thread'],start_at='now')
    case.config_path.write_text(json.dumps(case.config))
    case.manifest['files']={'source/blackoutburst_agents/'+name+'.py':{} for name in
        ('message_append_coordinator','message_append_slots','message_append_journal',
         'message_append_children','message_append_protocol')}
    with redirect_stdout(io.StringIO()),patch.object(entry,'manifest_id',return_value='b'*64):
        entry.execute(case.args('configure'),case.manifest)
        entry.execute(case.args('managed-prepare',host_id='host-id'),case.manifest)


def test_managed_run_passes_protected_factory_without_calling_it_in_control_thread(case):
    configure_messages(case)
    with redirect_stdout(io.StringIO()),patch.object(entry,'manifest_id',return_value='b'*64),\
            patch('blackoutburst_agents.server_control_client.ServerControlClient.from_connection_file') as opened,\
            patch('blackoutburst_agents.server_controller.ServerController') as controller,\
            patch('blackoutburst_agents.message_append_coordinator.MessagesAppendCoordinator') as coordinator:
        opened.return_value.__enter__.return_value.endpoint=case.config['board_endpoint']
        controller.return_value.run.return_value={'status':'stopped','model_calls_by_control_loop':0}
        assert entry.execute(case.args('managed-run'),case.manifest)==0
        factory=controller.call_args.kwargs['append_factory']
        assert callable(factory)
        coordinator.assert_not_called()
        opened.assert_called_once_with(str(case.connection),session_id='fixed-server-control',timeout=5)


@pytest.mark.parametrize('fault',['owner','release','private-configuration'])
def test_factory_requires_real_acquisition_and_rechecks_source_owner_and_private_config(case,fault):
    configure_messages(case)
    with patch.object(entry,'manifest_id',return_value='b'*64),\
            patch.object(entry,'owner_context',return_value=case.owner) as owner,\
            patch.object(entry,'verify_release',return_value=case.manifest) as verify,\
            patch('blackoutburst_agents.message_append_coordinator.MessagesAppendCoordinator') as coordinator,\
            patch('blackoutburst_agents.message_append_slots.PreparedMessageSlots') as slots,\
            HostJournal(case.journal) as worker:
        factory=entry._managed_append_factory(case.args('managed-run'),case.manifest,object())
        with pytest.raises(HostError,match='host_lock_required'):factory(worker)
        coordinator.assert_not_called()
        with worker.owner_lock():
            factory(worker)
            slots.assert_called_once()
            assert slots.call_args.args==(case.home,)
            source_guard=coordinator.call_args.kwargs['source_guard']
            assert source_guard() is None
            owner.assert_called_once_with(case.manifest,case.username)
            verify.assert_called_once_with(case.release)
            assert coordinator.call_args.args[0] is worker
            assert coordinator.call_args.kwargs['candidate_factory'] is slots.return_value.candidate
            if fault=='owner':owner.side_effect=entry.PackageError('unix_owner_mismatch')
            elif fault=='release':verify.return_value={**deepcopy(case.manifest),'architecture':'changed'}
            else:
                case.connection.write_text(case.connection.read_text()+' ')
            with pytest.raises(HostError,match='messages_append_source_changed'):source_guard()
