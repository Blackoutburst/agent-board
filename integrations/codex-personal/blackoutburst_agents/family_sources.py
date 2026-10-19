"""Bounded local source compatibility pins; no OS qualification claim."""
from pathlib import Path
import os
from .dev_manifest import DevelopmentError,RepositoryFiles,absolute_directory,digest
from .dev_planner import _directory_pin
from .dev_test_supervisor import _ancestor_identities

MODULES=('family_journal.py','family_client.py','family_audit.py','family_plan.py','family_worker.py',
         'family_cli.py','family_sources.py','context.py','development_worker.py','development_authorization.py',
         'development_cleanup.py','dev_planner.py','dev_fence.py')


def controller_sources(roots=None):
    directory=Path(__file__).resolve().parent
    if roots is not None:
        ancestry=_ancestor_identities(directory)
        for pin in roots.values():
            selected=_ancestor_identities(pin['path'])
            if tuple(pin['identity']) in ancestry or ancestry[0] in selected:
                raise DevelopmentError('family_controller_overlap')
    fd=absolute_directory(directory)
    try:
        files={};used=0
        for name in MODULES:
            data,_=RepositoryFiles.read_at(fd,name,256*1024);used+=len(data)
            if used>2*1024*1024:raise DevelopmentError('family_controller_sources_too_large')
            files[name]={'sha256':digest(data),'bytes':len(data)}
        return {'package':_directory_pin(directory),'files':files}
    finally:os.close(fd)
