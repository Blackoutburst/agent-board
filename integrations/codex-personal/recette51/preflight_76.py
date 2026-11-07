"""RECETTE-51 §7.6 step 2: rollback preflight for the queue-slot pointers.

Standard library only; meant to run under the owner lease, for example:

    sudo -u bb-agent-thescreamer -H flock -n -E 75 "$R/continuations/owner.lock" \
      /usr/bin/python3.10 -I -B preflight-76.py --root "$R" [--apply]

Without --apply it is read-only. It walks, without following any symbolic
link, every R/slots/<slot>/admissions/queue-<key>/tick.json and every
R/admissions/queue-<key>/tick.json. Each directory on those paths must be a
real directory owned by the effective UID with mode 0700; each tick.json a
regular, single-link 0600 file owned by the effective UID. Any symlink,
unexpected entry or other type refuses. The single non-slot entry accepted
under R/slots is the inventory that `reconcile_inventory` writes,
R/slots/inventory.json: a private regular single-link 0600 file owned by the
effective UID, at most 512 KiB, whose bytes are exactly the canonical JSON of
{"schema": "linux-cloud-queue-slots-inventory-v1", "slots": {<slot>: {
"agent_id", "catalog_sha256", "approval_sha256", "resources"}}}. Only its
sha256 is printed. It prints only the relative path, `phase` and whether
`active` is null, and exits 0 only if every pointer is `idle` with `active`
null.

With --apply it repeats the same check and, only if it passes, requires
R/config/continuation.pre51.json to be a private regular file with one link,
then atomically replaces R/config/continuation.json with a copy of it (0600
temporary file in the same directory, fsync, rename, directory fsync). It
never deletes the slots document, a slot root or any history. It prints the
sha256 of the old and new continuation.json, nothing else from documents.

Exit codes: 0 all idle (and replaced with --apply); 1 a pointer is not idle;
2 refused (unsafe or unexpected layout, unreadable document, bad argument).
"""
import argparse
import hashlib
import json
import os
import re
import stat
import sys

MAX_DOCUMENT = 512 * 1024
SLOT = re.compile(r'[A-Za-z0-9_.:-]{1,200}')
QUEUE = re.compile(r'queue-[0-9a-f]{64}')
REQUEST = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
PHASE = re.compile(r'[a-z_]{1,32}')
QUEUE_ENTRIES = frozenset({'tick.json', 'tick.lock'})
INVENTORY = 'inventory.json'
INVENTORY_SCHEMA = 'linux-cloud-queue-slots-inventory-v1'
BINDING_FIELDS = frozenset({'agent_id', 'catalog_sha256', 'approval_sha256', 'resources'})
OPAQUE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}')
SHA256 = re.compile(r'[0-9a-f]{64}')
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Refused(Exception):
    pass


def refuse(reason, path):
    raise Refused('refused: %s: %s' % (reason, path))


def private(info, path, kind):
    if info.st_uid != os.geteuid():
        refuse('wrong owner', path)
    if kind == 'directory':
        if not stat.S_ISDIR(info.st_mode):
            refuse('not a directory', path)
        if stat.S_IMODE(info.st_mode) != 0o700:
            refuse('mode is not 0700', path)
    else:
        if not stat.S_ISREG(info.st_mode):
            refuse('not a regular file', path)
        if stat.S_IMODE(info.st_mode) != 0o600:
            refuse('mode is not 0600', path)
        if info.st_nlink != 1:
            refuse('not a single link', path)


def open_directory(name, path, parent=None):
    """A real private directory, never through a symbolic link."""
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        refuse('symbolic link', path)
    private(info, path, 'directory')
    try:
        fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    except OSError:
        refuse('cannot open directory', path)
    opened = os.fstat(fd)
    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        os.close(fd)
        refuse('directory changed', path)
    return fd


def read_private(name, parent, path):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        refuse('missing', path)
    except OSError:
        refuse('symbolic link or unreadable file', path)
    try:
        info = os.fstat(fd)
        private(info, path, 'file')
        if info.st_size > MAX_DOCUMENT:
            refuse('document too large', path)
        data = b''
        while len(data) <= MAX_DOCUMENT:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
        if len(data) > MAX_DOCUMENT:
            refuse('document too large', path)
        return data
    finally:
        os.close(fd)


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate key')
        value[key] = item
    return value


def pointer(name, parent, path):
    data = read_private(name, parent, path)
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
    except (UnicodeError, ValueError, RecursionError):
        refuse('invalid document', path)
    if type(value) is not dict or 'phase' not in value or 'active' not in value:
        refuse('not a tick pointer', path)
    phase = value['phase'] if type(value['phase']) is str and PHASE.fullmatch(value['phase']) else 'invalid'
    return phase, value['active'] is None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def inventory(parent):
    """R/slots/inventory.json exactly as reconcile_inventory writes it; its sha256."""
    path = 'slots/' + INVENTORY
    data = read_private(INVENTORY, parent, path)
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
    except (UnicodeError, ValueError, RecursionError):
        refuse('invalid document', path)
    if (type(value) is not dict or set(value) != {'schema', 'slots'} or value['schema'] != INVENTORY_SCHEMA
            or type(value['slots']) is not dict):
        refuse('not a slots inventory', path)
    for name, binding in value['slots'].items():
        if not SLOT.fullmatch(name) or type(binding) is not dict or set(binding) != BINDING_FIELDS:
            refuse('not a slots inventory', path)
        resources = binding['resources']
        if (type(binding['agent_id']) is not str or not OPAQUE.fullmatch(binding['agent_id'])
                or any(type(binding[key]) is not str or not SHA256.fullmatch(binding[key])
                       for key in ('catalog_sha256', 'approval_sha256'))
                or type(resources) is not list
                or any(type(item) is not str or not item or len(item) > 512 for item in resources)
                or resources != sorted(set(resources))):
            refuse('not a slots inventory', path)
    if canonical(value) != data:
        refuse('not canonical', path)
    return hashlib.sha256(data).hexdigest()


def entries(fd, path):
    try:
        return sorted(os.listdir(fd))
    except OSError:
        refuse('cannot list', path)


def admissions(fd, label, rows):
    """R/.../admissions: queue-<key> pointer directories and request directories."""
    admission = open_directory('admissions', label + 'admissions', fd)
    if admission is None:
        return
    try:
        for name in entries(admission, label + 'admissions'):
            path = label + 'admissions/' + name
            if QUEUE.fullmatch(name):
                queue = open_directory(name, path, admission)
                try:
                    names = set(entries(queue, path))
                    if not names <= QUEUE_ENTRIES:
                        refuse('unexpected entry', path + '/' + sorted(names - QUEUE_ENTRIES)[0])
                    if 'tick.lock' in names:
                        read_private('tick.lock', queue, path + '/tick.lock')
                    phase, idle_active = pointer('tick.json', queue, path + '/tick.json')
                    rows.append((path + '/tick.json', phase, idle_active))
                finally:
                    os.close(queue)
            elif REQUEST.fullmatch(name):
                os.close(open_directory(name, path, admission))
            else:
                refuse('unexpected entry', path)
    finally:
        os.close(admission)


def root_fd(root):
    """The owner root itself must be a private real directory."""
    if not os.path.isabs(root):
        refuse('root must be absolute', root)
    parent, name = os.path.split(os.path.normpath(root))
    try:
        parent_fd = os.open(parent, DIRECTORY_FLAGS)
    except OSError:
        refuse('cannot open parent', parent)
    try:
        fd = open_directory(name, root, parent_fd)
    finally:
        os.close(parent_fd)
    if fd is None:
        refuse('missing', root)
    return fd


def scan(root, found=None):
    """Every pointer row as (relative path, phase, active is None)."""
    fd = root_fd(root)
    rows = []
    found = {} if found is None else found
    try:
        admissions(fd, '', rows)
        slots = open_directory('slots', 'slots', fd)
        if slots is not None:
            try:
                for slot in entries(slots, 'slots'):
                    if slot == INVENTORY:
                        found['inventory'] = inventory(slots)
                        continue
                    if not SLOT.fullmatch(slot):
                        refuse('unexpected entry', 'slots/' + slot)
                    slot_fd = open_directory(slot, 'slots/' + slot, slots)
                    try:
                        admissions(slot_fd, 'slots/' + slot + '/', rows)
                    finally:
                        os.close(slot_fd)
            finally:
                os.close(slots)
    finally:
        os.close(fd)
    return rows


def digest(data):
    return 'absent' if data is None else hashlib.sha256(data).hexdigest()


def apply(root):
    fd = root_fd(root)
    try:
        config = open_directory('config', 'config', fd)
    finally:
        os.close(fd)
    if config is None:
        refuse('missing', 'config')
    try:
        source = read_private('continuation.pre51.json', config, 'config/continuation.pre51.json')
        try:
            old = read_private('continuation.json', config, 'config/continuation.json')
        except Refused as error:
            if 'missing' not in str(error):
                raise
            old = None
        temporary = '.continuation.json.preflight-%d' % os.getpid()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                     dir_fd=config)
        try:
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(source)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, 'continuation.json', src_dir_fd=config, dst_dir_fd=config)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=config)
            except FileNotFoundError:
                pass
            raise
        os.fsync(config)
        new = read_private('continuation.json', config, 'config/continuation.json')
        if new != source:
            refuse('replacement does not match continuation.pre51.json', 'config/continuation.json')
        return digest(old), digest(new)
    finally:
        os.close(config)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--root', required=True)
    parser.add_argument('--apply', action='store_true')
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as error:
        return 2 if error.code else 0
    try:
        found = {}
        rows = scan(arguments.root, found)
        if 'inventory' in found:
            print('slots/%s sha256=%s' % (INVENTORY, found['inventory']))
        for path, phase, active_none in rows:
            print('%s phase=%s active_is_none=%s' % (path, phase, active_none))
        ready = all(phase == 'idle' and active_none for _, phase, active_none in rows)
        print('pointers=%d all_idle=%s' % (len(rows), ready))
        if not ready:
            return 1
        if arguments.apply:
            old, new = apply(arguments.root)
            print('continuation.json old_sha256=%s new_sha256=%s' % (old, new))
        return 0
    except Refused as error:
        print(error, file=sys.stderr)
        return 2
    except OSError as error:
        print('refused: %s' % (error.strerror or 'os error'), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
