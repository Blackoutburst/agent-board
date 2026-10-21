"""Bounded local source compatibility pins; no OS qualification claim."""
from pathlib import Path
import os
from .dev_manifest import DevelopmentError,absolute_directory,digest
from .dev_planner import _directory_pin
from .dev_test_supervisor import _ancestor_identities

MODULES=('__init__.py', 'board_reader.py', 'client_lifecycle.py', 'codex_turn.py', 'context.py', 'context_cache.py', 'decision_memory.py', 'dev_broker.py', 'dev_fence.py', 'dev_journal.py', 'dev_manifest.py', 'dev_planner.py', 'dev_planner_journal.py', 'dev_runtime.py', 'dev_test_backend.py', 'dev_test_supervisor.py', 'development_authorization.py', 'development_cleanup.py', 'development_cli.py', 'development_client.py', 'development_frame.py', 'development_worker.py', 'event_journal.py', 'family_audit.py', 'family_cli.py', 'family_client.py', 'family_journal.py', 'family_plan.py', 'family_sources.py', 'family_worker.py', 'host_config.py', 'linux_execution_qualification.py', 'linux_execution_state.py', 'linux_input_admission.py', 'linux_input_plan.py', 'linux_input_snapshot.py', 'linux_input_state.py', 'linux_planner_policy.py', 'linux_test_backend.py', 'linux_test_namespace.py', 'linux_test_protocol.py', 'linux_test_supervisor.py', 'message_config.py', 'message_journal.py', 'message_scope.py', 'model_policy.py', 'persistent_transport.py', 'planner_policy.py', 'run_store.py', 'runner.py', 'runtime_client.py', 'runtime_guard.py', 'runtime_journal.py', 'subscription_probe.py', 'work_journal.py', 'worker_client.py')


def controller_sources(roots=None):
    from .linux_planner_policy import _public_source_at
    from .linux_input_snapshot import _chain
    directory=Path(__file__).resolve().parent
    if roots is not None:
        ancestry=_ancestor_identities(directory)
        for pin in roots.values():
            selected=_ancestor_identities(pin['path'])
            if tuple(pin['identity']) in ancestry or ancestry[0] in selected:
                raise DevelopmentError('family_controller_overlap')
    chain=_chain(str(directory));fd=absolute_directory(directory)
    try:
        info=os.fstat(fd)
        if ([info.st_dev,info.st_ino]!=chain[-1]['identity'] or info.st_uid not in (0,os.geteuid())
                or info.st_mode&0o022):
            raise DevelopmentError('family_controller_sources_changed')
        files={};used=0
        for name in MODULES:
            data=_public_source_at(fd,name,min(256*1024,2*1024*1024-used));used+=len(data)
            if used>2*1024*1024:raise DevelopmentError('family_controller_sources_too_large')
            files[name]={'sha256':digest(data),'bytes':len(data)}
        # The parent FD stays held for the whole bounded collection. Recheck
        # its full linkage once after reading, as in linux_source_pins; no grant
        # or file observation is cached between calls.
        if _chain(str(directory))!=chain:raise DevelopmentError('family_controller_sources_changed')
        return {'package':_directory_pin(directory),'files':files}
    finally:os.close(fd)
