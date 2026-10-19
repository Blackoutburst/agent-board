"""Private pinned scripts for INPUT preparation only; never an executor plan."""
from __future__ import annotations
import os
from pathlib import Path
import threading
import unicodedata
from .dev_journal import decode
from .dev_manifest import DevelopmentError, RepositoryFiles, canonical, check_content, digest, opaque, relative_path
from .linux_input_snapshot import _chain, _path, _relative_parents

SCHEMA = 'blackoutburst-linux-input-plan-v1'
MAX_SCRIPTS = 16
MAX_SCRIPT_BYTES = 64 * 1024
MAX_PLAN_BYTES = 128 * 1024
_FACTORY = object()


def _fail():
    raise DevelopmentError('linux_input_plan_invalid')


def _capture(root, scripts_root, scripts):
    root_chain, scripts_chain = _chain(root), _chain(scripts_root)
    if (root_chain[-1]['identity'] in [pin['identity'] for pin in scripts_chain]
            or scripts_chain[-1]['identity'] in [pin['identity'] for pin in root_chain]): _fail()
    entries, aliases, inodes = {}, set(), set()
    files = RepositoryFiles(scripts_root, scripts_chain[-1]['identity'])
    try:
        for name, specification in sorted(scripts.items()):
            opaque(name, maximum=64)
            if (type(specification) is not dict or set(specification) != {'path', 'timeout_seconds', 'max_output_bytes'}
                    or type(specification['timeout_seconds']) is not int or not 1 <= specification['timeout_seconds'] <= 60
                    or type(specification['max_output_bytes']) is not int or not 0 <= specification['max_output_bytes'] <= 1048576): _fail()
            path = specification['path']; relative_path(path)
            alias = unicodedata.normalize('NFC', path).casefold()
            if alias in aliases or not path.endswith('.py'): _fail()
            aliases.add(alias)
            parent, leaf, parents = _relative_parents(files, path)
            try:
                data, signature = files.read_at(parent, leaf, MAX_SCRIPT_BYTES)
                check_content(data)
                if signature[:2] in inodes: _fail()
                inodes.add(signature[:2])
                again, _, current = _relative_parents(files, path); os.close(again)
                if current != parents: _fail()
                entries[name] = {**specification, 'sha256': digest(data), 'bytes': len(data),
                                 'signature': list(signature), 'parents': parents}
            finally: os.close(parent)
    finally: files.close()
    if root_chain != _chain(root) or scripts_chain != _chain(scripts_root): _fail()
    result = {'schema': SCHEMA, 'purpose': 'input_admission_only', 'execution_supported': False,
              'root': root, 'root_chain': root_chain, 'scripts_root': scripts_root,
              'scripts_chain': scripts_chain, 'scripts': entries}
    if len(canonical(result)) > MAX_PLAN_BYTES: _fail()
    return result


class _LinuxInputPlan:
    def __init__(self, factory, root, scripts_root, scripts):
        if factory is not _FACTORY: _fail()
        self._pid, self._thread, self._active = os.getpid(), threading.current_thread(), True
        self._scripts = decode(canonical(scripts))
        self._document = _capture(root, scripts_root, self._scripts)
        self._bytes = canonical(self._document)
        self.sha256 = digest(self._bytes)

    def __reduce_ex__(self, protocol): raise TypeError('Private plans cannot be serialized')
    def __copy__(self): raise TypeError('Private plans cannot be copied')
    def __deepcopy__(self, memo): raise TypeError('Private plans cannot be copied')

    def verify(self):
        try:
            if (not self._active or self._pid != os.getpid() or self._thread is not threading.current_thread()
                    or digest(self._bytes) != self.sha256
                    or canonical(_capture(self._document['root'], self._document['scripts_root'], self._scripts)) != self._bytes): _fail()
            return True
        except BaseException:
            self._active = False
            raise

    def document(self):
        self.verify()
        return decode(self._bytes)


def pin_linux_input_plan(*, root, scripts_root, scripts):
    """Explicit controller inventory of bounded approved scripts, never imports them.

    This handle pins inputs only, carries no grant or runtime inventory, and
    cannot be substituted for a future qualified Linux execution plan.
    """
    root, scripts_root = _path(os.fspath(root)), _path(os.fspath(scripts_root))
    # Deny private path components before opening any script or directory.
    for path in (root, scripts_root):
        for component in Path(path).parts[1:]: relative_path(component)
    if type(scripts) is not dict or not 1 <= len(scripts) <= MAX_SCRIPTS: _fail()
    return _LinuxInputPlan(_FACTORY, root, scripts_root, scripts)
