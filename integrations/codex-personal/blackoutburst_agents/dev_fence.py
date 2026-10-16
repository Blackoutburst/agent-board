"""One local, non-configurable anchor for development operations.

This is a same-user, same-machine durability fence, not protection against an
administrator deleting its state or a coordination service for multiple hosts.
The underscored anchor override exists solely for isolated fixture tests.

Format 3 adds an atomic three-root family reservation. An older format reader
must refuse it; stop old coordinators before sharing a migrated anchor. There
is no automatic release, expiry, repair or rollback detection against an OS
administrator. Private family targets precede each explicit transfer, and
retries accept only those exact reserved/parent/planner prefixes. Missing or
partial active rows require review. No Board permission follows from a fence.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import os
from pathlib import Path
import pwd
import stat
import threading
import urllib.parse
import uuid

from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, absolute_directory, canonical, digest, identity, sha256


FAMILY_SLOTS = ('child_0', 'child_1', 'parent')
MAX_FENCE_BYTES = 2 * 1024 * 1024


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:
        raise ValueError
    return value


def _pin(value):
    if (type(value) is not dict or set(value) != {'path', 'identity'}
            or type(value['path']) is not str or not value['path'].startswith('/')
            or any(part in ('.', '..') for part in value['path'].split('/'))
            or '\x00' in value['path'] or type(value['identity']) is not list
            or len(value['identity']) != 2
            or any(type(item) is not int or item < 0 for item in value['identity'])):
        raise ValueError


def _descriptor(value):
    if (type(value) is not dict or set(value) != {'schema', 'operation_id',
            'configuration_sha256', 'journal', 'roots', 'sessions'}
            or type(value['schema']) is not int or value['schema'] != 1
            or type(value['roots']) is not dict or set(value['roots']) != set(FAMILY_SLOTS)
            or type(value['sessions']) is not dict or set(value['sessions']) != set(FAMILY_SLOTS)):
        raise ValueError
    _uuid(value['operation_id']); sha256(value['configuration_sha256']); _pin(value['journal'])
    for slot in FAMILY_SLOTS:
        _pin(value['roots'][slot])
        session = value['sessions'][slot]
        if type(session) is not str or not session.strip() or len(session) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in session):
            raise ValueError
    if len({tuple(pin['identity']) for pin in value['roots'].values()}) != 3:
        raise ValueError


def _live_pin(pin, *, private=False):
    """Current effects check paths; historical fence validation never does."""
    if private:
        with PrivateDirectory(pin['path']) as directory:
            if list(directory.identity) != pin['identity']:
                raise DevelopmentError('canonical_family_changed')
    else:
        fd = absolute_directory(pin['path'])
        try:
            if list(identity(os.fstat(fd))) != pin['identity']:
                raise DevelopmentError('canonical_family_changed')
        finally:
            os.close(fd)


def normalized_origin(endpoint):
    try:
        if type(endpoint) is not str:
            raise ValueError
        value = urllib.parse.urlsplit(endpoint)
        if ( not value.hostname or value.username is not None
                or value.password is not None or value.query or value.fragment
                or (value.scheme != 'https' and not (value.scheme == 'http' and
                    value.hostname in ('127.0.0.1', '::1', 'localhost')))):
            raise ValueError
        port = value.port
        host = value.hostname.encode('idna').decode('ascii').lower()
        if ':' in host:
            host = '[' + host + ']'
        if port is not None and port != (443 if value.scheme == 'https' else 80):
            host += ':' + str(port)
        return value.scheme + '://' + host
    except (ValueError, AttributeError, UnicodeError, TypeError):
        raise DevelopmentError('invalid_board_origin') from None


def canonical_anchor():
    # HOME and CODEX_HOME deliberately do not select the fence.
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/blackoutburst-agents/development'


def _create_chain(path):
    """Create components relative to open nofollow directories, never a symlink."""
    path = Path(path).absolute()
    if '..' in path.parts:
        raise DevelopmentError('invalid_fence_directory')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
                os.fsync(fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if (info.st_uid not in (0, os.geteuid()) or (info.st_mode & 0o022 and
                    not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
                raise DevelopmentError('insecure_fence_ancestor')
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise DevelopmentError('insecure_fence_directory')
    except OSError:
        raise DevelopmentError('fence_directory_unavailable') from None
    finally:
        os.close(fd)
    return path


class DevelopmentFence:
    def __init__(self, *, _test_anchor=None):
        self.private = PrivateDirectory(_create_chain(canonical_anchor() if _test_anchor is None else _test_anchor))
        self._thread_lock = threading.Lock()
        self.fd = None
        try:
            self.fd = os.open('fence.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                              0o600, dir_fd=self.private.fd)
            self.private.validate_file(self.fd)
            with self.lock():
                try:
                    self.private.read('fence.json', 2 * 1024 * 1024)
                except DevelopmentError as error:
                    if error.code != 'private_read_failed':
                        raise
                    try:
                        os.stat('fence.json', dir_fd=self.private.fd, follow_symlinks=False)
                    except FileNotFoundError:
                        self.private.create('fence.json', canonical({'schema': 1, 'records': {}}))
                    else:
                        raise error
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.private.close()

    @contextmanager
    def lock(self):
        if not self._thread_lock.acquire(blocking=False):
            raise DevelopmentError('fence_busy')
        locked = False
        try:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                self.private.verify()
                current = os.stat('fence.lock', dir_fd=self.private.fd, follow_symlinks=False)
                if identity(current) != identity(os.fstat(self.fd)):
                    raise DevelopmentError('fence_lock_changed')
                self.private.validate_file(self.fd)
            except (OSError, TypeError):
                raise DevelopmentError('fence_busy') from None
            yield
        finally:
            if locked:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            self._thread_lock.release()

    @staticmethod
    def key(scope, root_identity):
        return digest(canonical({'origin': normalized_origin(scope['board_endpoint']),
            **{key: scope[key] for key in ('owner_id', 'agent_id', 'project_id', 'ticket_id')},
            'root_identity': list(root_identity)}))

    def _read(self):
        state = decode(self.private.read('fence.json', 2 * 1024 * 1024))
        return self._validate(state)

    def _validate(self, state):
        if (type(state) is not dict or set(state) != {'schema', 'records'} or state['schema'] not in (1, 2, 3)
                or type(state['schema']) is not int or type(state['records']) is not dict):
            raise DevelopmentError('fence_corrupt')
        try:
            from .work_journal import _scope
            for key, value in state['records'].items():
                binding = value['binding']
                _scope(binding['scope'])
                sha256(binding['configuration_hash'])
                if (type(value) is not dict or set(value) not in ({'binding', 'operation_id'}, {'binding', 'operation_id', 'family'})
                        or type(binding) is not dict or set(binding) not in ({'scope', 'root_identity',
                            'journal', 'configuration_hash', 'child_scope'}, {'scope', 'root_identity',
                            'journal', 'configuration_hash', 'child_scope', 'parent_scope'})
                        or type(binding['root_identity']) is not list or len(binding['root_identity']) != 2
                        or any(type(item) is not int or item < 0 for item in binding['root_identity'])
                        or str(uuid.UUID(value['operation_id'])) != value['operation_id']
                        or key != self.key(binding['scope'], binding['root_identity'])):
                    raise ValueError
                if 'parent_scope' in binding:
                    parent = binding['parent_scope']
                    if (state['schema'] < 2 or type(parent) is not dict or set(parent) !=
                            {'operation_id', 'journal', 'configuration_hash'}
                            or str(uuid.UUID(parent['operation_id'])) != parent['operation_id']
                            or parent['operation_id'] != value['operation_id']):
                        raise ValueError
                    sha256(parent['configuration_hash'])
                children = binding['child_scope']
                if (type(children) is not dict or set(children) != {'work_id', 'runs', 'runtime'}
                        or str(uuid.UUID(children['work_id'])) != children['work_id']):
                    raise ValueError
                for pin in ([binding['journal'], children['runs'], children['runtime']] +
                            ([binding['parent_scope']['journal']] if 'parent_scope' in binding else [])):
                    _pin(pin)
                if 'family' in value:
                    family = value['family']
                    if (state['schema'] != 3 or type(family) is not dict or set(family) !=
                            {'descriptor', 'slot', 'stage', 'server_family_id'}
                            or family['slot'] not in FAMILY_SLOTS
                            or family['stage'] not in ('reserved', 'parent', 'planner')):
                        raise ValueError
                    descriptor, slot = family['descriptor'], family['slot']
                    _descriptor(descriptor)
                    workflow = str(uuid.uuid5(uuid.UUID(descriptor['operation_id']), slot))
                    from .dev_runtime import development_work_id
                    if (value['operation_id'] != workflow
                            or binding['root_identity'] != descriptor['roots'][slot]['identity']
                            or binding['scope']['board_session_id'] != descriptor['sessions'][slot]
                            or children['work_id'] != development_work_id(workflow)):
                        raise ValueError
                    if family['stage'] == 'reserved':
                        if (family['server_family_id'] is not None or 'parent_scope' in binding
                                or binding['journal'] != descriptor['journal']
                                or binding['configuration_hash'] != descriptor['configuration_sha256']):
                            raise ValueError
                    else:
                        identifier = family['server_family_id']
                        if (type(identifier) is not str or not identifier.strip() or len(identifier) > 256
                                or any(ord(c) < 32 or ord(c) == 127 for c in identifier)
                                or 'parent_scope' not in binding
                                or (family['stage'] == 'parent' and binding['configuration_hash'] !=
                                    binding['parent_scope']['configuration_hash'])):
                            raise ValueError
            roots=[tuple(value['binding']['root_identity']) for value in state['records'].values()]
            if len(set(roots))!=len(roots):raise ValueError
        except Exception:
            raise DevelopmentError('fence_corrupt') from None
        return state

    def _save(self, state, current):
        self._validate(state)
        encoded = canonical(state)
        if len(encoded) > MAX_FENCE_BYTES:
            raise DevelopmentError('fence_full')
        current()  # Recheck the held private family document before mutation.
        self.private.replace('fence.json', encoded)

    @staticmethod
    def _family_record(binding, descriptor, slot, stage, server_family_id=None):
        return {'binding': deepcopy(binding),
            'operation_id': str(uuid.uuid5(uuid.UUID(descriptor['operation_id']), slot)),
            'family': {'descriptor': deepcopy(descriptor), 'slot': slot,
                'stage': stage, 'server_family_id': server_family_id}}

    @contextmanager
    def _family_snapshot(self, reservations, descriptor):
        """Read an exact private intent, keeping its nofollow directory open.

        The document is evidence for identity only, never Board authorization.
        Historical records are not opened here: only this explicit family and
        its current roots/directories can participate in an effect.
        """
        directory = None
        try:
            from .work_journal import _scope
            from .dev_runtime import development_work_id
            _descriptor(descriptor)
            directory = PrivateDirectory(descriptor['journal']['path'])
            if list(directory.identity) != descriptor['journal']['identity']:
                raise ValueError
            raw = directory.read('family.json', MAX_FENCE_BYTES)
            document = decode(raw)
            if (type(document) is not dict or type(document.get('schema')) is not int
                    or document['schema'] != 1 or document.get('operation_id') != descriptor['operation_id']
                    or type(document.get('configuration')) is not dict
                    or document.get('configuration_sha256') != descriptor['configuration_sha256']
                    or type(document.get('payload')) is not dict
                    or type(document.get('fence_ready')) is not bool
                    or type(document.get('phase')) is not str):
                raise ValueError
            config, payload = document['configuration'], document['payload']
            if digest(canonical(config)) != descriptor['configuration_sha256']:
                raise ValueError
            _scope(config['scope'])
            if (config['roots'] != descriptor['roots'] or set(config['provisional_scopes']) != set(FAMILY_SLOTS)
                    or set(config['child_stores']) != set(FAMILY_SLOTS)
                    or set(config['pins']) != set(FAMILY_SLOTS)):
                raise ValueError
            expected = {}
            workflows = {slot: str(uuid.uuid5(uuid.UUID(descriptor['operation_id']), slot)) for slot in FAMILY_SLOTS}
            if document.get('workflow_ids') != workflows:
                raise ValueError
            for slot in FAMILY_SLOTS:
                scope = config['provisional_scopes'][slot]
                _scope(scope)
                if (scope != {**config['scope'], 'board_session_id': descriptor['sessions'][slot]}
                        or (slot == 'parent' and scope != config['scope'])
                        or type(config['child_stores'][slot]) is not dict
                        or set(config['child_stores'][slot]) != {'runs', 'runtime'}):
                    raise ValueError
                for pin in config['child_stores'][slot].values():
                    _pin(pin)
                pins = config['pins'][slot]
                if (type(pins) is not dict or not {'journal', 'broker_directory'} <= set(pins)
                        or not set(pins) <= {'journal', 'broker_directory', 'backend_directory', 'planner_policy_directory'}):
                    raise ValueError
                for pin in pins.values():
                    _pin(pin)
                expected[slot] = {'scope': scope, 'root_identity': descriptor['roots'][slot]['identity'],
                    'journal': descriptor['journal'], 'configuration_hash': descriptor['configuration_sha256'],
                    'child_scope': {'work_id': development_work_id(workflows[slot]), **config['child_stores'][slot]}}
            if len(set(descriptor['sessions'].values())) != 3:
                raise ValueError
            if (reservations != expected or payload.get('fence_descriptor') != descriptor
                    or payload.get('fence_reservations') != reservations):
                raise ValueError
            targets = payload.get('fence_targets')
            if type(targets) is not dict or not set(targets) <= set(FAMILY_SLOTS):
                raise ValueError
            records = {slot: [self._family_record(expected[slot], descriptor, slot, 'reserved')] for slot in FAMILY_SLOTS}
            for slot, stages in targets.items():
                if (type(stages) is not dict or not stages or not set(stages) <= {'parent', 'planner'}
                        or ('planner' in stages and 'parent' not in stages)):
                    raise ValueError
                for stage in ('parent', 'planner'):
                    if stage not in stages:
                        continue
                    proof = stages[stage]
                    if (type(proof) is not dict or set(proof) != {'descriptor', 'slot', 'workflow_id',
                            'server_family_id', 'stage', 'binding'} or proof['descriptor'] != descriptor
                            or proof['slot'] != slot or proof['stage'] != stage
                            or proof['workflow_id'] != workflows[slot]
                            or proof['server_family_id'] != payload.get('family_id')):
                        raise ValueError
                    binding = proof['binding']
                    scope = payload['slot_scopes'][slot]
                    _scope(scope)
                    if (any(scope[name] != expected[slot]['scope'][name] for name in
                            ('owner_id', 'agent_id', 'project_id', 'board_session_id', 'board_endpoint'))
                            or (slot == 'parent' and scope != config['scope'])
                            or binding['scope'] != scope
                            or binding['root_identity'] != expected[slot]['root_identity']
                            or binding['journal'] != config['pins'][slot]['broker_directory']
                            or binding['child_scope'] != expected[slot]['child_scope']
                            or binding['parent_scope']['journal'] != config['pins'][slot]['journal']
                            or binding['parent_scope']['operation_id'] != workflows[slot]):
                        raise ValueError
                    if stage == 'parent':
                        if binding['configuration_hash'] != binding['parent_scope']['configuration_hash']:
                            raise ValueError
                    elif (binding['parent_scope'] != stages['parent']['binding']['parent_scope']
                            or any(binding[name] != stages['parent']['binding'][name] for name in
                                ('scope', 'root_identity', 'journal', 'child_scope'))):
                        raise ValueError
                    record = self._family_record(binding, descriptor, slot, stage, proof['server_family_id'])
                    self._validate({'schema': 3, 'records': {self.key(binding['scope'], binding['root_identity']): record}})
                    records[slot].append(record)
            # All three roots and the directories used by their next effects
            # must still name the declared inodes. No history-wide filesystem scan.
            from .dev_test_supervisor import _ancestor_identities
            roots = descriptor['roots']
            private_pins = [descriptor['journal'], {'path': str(self.private.path), 'identity': list(self.private.identity)}]
            for slot in FAMILY_SLOTS:
                private_pins.extend(config['child_stores'][slot].values())
                private_pins.extend(config['pins'][slot].values())
            for slot, pin in roots.items():
                _live_pin(pin)
                ancestors = _ancestor_identities(pin['path'])
                if any(tuple(other['identity']) in ancestors for name, other in roots.items() if name != slot):
                    raise ValueError
                for private_pin in private_pins:
                    private_ancestors = _ancestor_identities(private_pin['path'])
                    if tuple(pin['identity']) in private_ancestors or tuple(private_pin['identity']) in ancestors:
                        raise ValueError
            for slot in FAMILY_SLOTS:
                for pin in config['child_stores'][slot].values():
                    _live_pin(pin, private=True)
                for pin in config['pins'][slot].values():
                    _live_pin(pin, private=True)
            def current():
                directory.verify()
                if directory.read('family.json', MAX_FENCE_BYTES) != raw:
                    raise DevelopmentError('canonical_family_changed')
                for pin in roots.values():
                    _live_pin(pin)
                for pin in private_pins:
                    _live_pin(pin, private=True)
            yield document, records, current
        except (KeyError, TypeError, ValueError, AttributeError, OSError):
            raise DevelopmentError('canonical_family_invalid') from None
        finally:
            if directory is not None:
                directory.close()

    @staticmethod
    def _root_record(state, root_identity):
        values = [(key, value) for key, value in state['records'].items()
            if value['binding']['root_identity'] == root_identity]
        return values[0] if values else (None, None)

    def _verify_family_records(self, state, records):
        for allowed in records.values():
            _, found = self._root_record(state, allowed[0]['binding']['root_identity'])
            if found not in allowed:
                raise DevelopmentError('canonical_family_changed')

    def reserve_family(self, reservations_by_slot, descriptor):
        """Reserve all three exact roots with one lock and atomic replacement."""
        with self.lock():
            state = self._read()
            with self._family_snapshot(reservations_by_slot, descriptor) as (document, records, current):
                found = [self._root_record(state, reservations_by_slot[slot]['root_identity'])[1] for slot in FAMILY_SLOTS]
                if any(value is not None for value in found):
                    if any(value is None for value in found):
                        raise DevelopmentError('canonical_checkout_busy')
                    self._verify_family_records(state, records)
                    current()
                    return descriptor['operation_id'], False
                if (document['fence_ready'] or document['phase'] != 'prepared'
                        or document['payload']['fence_targets']
                        or 'family_id' in document['payload']
                        or 'slot_scopes' in document['payload']):
                    # An absent active anchor is an anomaly, never permission
                    # to reconstruct an already-open or transferred family.
                    raise DevelopmentError('canonical_family_changed')
                if len(state['records']) + 3 > 4096:
                    raise DevelopmentError('fence_full')
                for slot in FAMILY_SLOTS:
                    binding = reservations_by_slot[slot]
                    state['records'][self.key(binding['scope'], binding['root_identity'])] = records[slot][0]
                state['schema'] = 3
                self._save(state, current)
                return descriptor['operation_id'], True

    def verify_family(self, reservations, descriptor, targets):
        """Verify known private prefixes; never adopt targets from fence rows."""
        with self.lock():
            state = self._read()
            with self._family_snapshot(reservations, descriptor) as (document, records, current):
                if document['payload']['fence_targets'] != targets:
                    raise DevelopmentError('canonical_family_changed')
                self._verify_family_records(state, records)
                current()

    def transfer_family_slot(self, proof):
        """Explicit reserved-to-parent transfer, durable private target first."""
        return self._change_family(proof, 'parent')

    def _attach_family(self, binding, operation_id, parent_binding, proof):
        if (type(proof) is not dict or proof.get('binding') != binding
                or proof.get('workflow_id') != operation_id):
            raise DevelopmentError('canonical_parent_conflict')
        return self._change_family(proof, 'planner', parent_binding=parent_binding)

    def _change_family(self, proof, stage, parent_binding=None):
        try:
            descriptor = proof['descriptor']
            _descriptor(descriptor)
            slot = proof['slot']
            if slot not in FAMILY_SLOTS or proof['stage'] != stage:
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise DevelopmentError('canonical_family_invalid') from None
        with self.lock():
            state = self._read()
            # The reservation is reconstructed from the private configuration,
            # never discovered/adopted through the current fence record.
            with PrivateDirectory(descriptor['journal']['path']) as directory:
                if list(directory.identity) != descriptor['journal']['identity']:
                    raise DevelopmentError('canonical_family_changed')
                snapshot = decode(directory.read('family.json', MAX_FENCE_BYTES))
                try:
                    reservations = snapshot['payload']['fence_reservations']
                except (KeyError, TypeError):
                    raise DevelopmentError('canonical_family_invalid') from None
            with self._family_snapshot(reservations, descriptor) as (document, records, current):
                if document['payload']['fence_targets'].get(slot, {}).get(stage) != proof:
                    raise DevelopmentError('canonical_family_changed')
                self._verify_family_records(state, records)
                key, found = self._root_record(state, reservations[slot]['root_identity'])
                desired = next((record for record in records[slot] if record['family']['stage'] == stage), None)
                if desired is None:
                    raise DevelopmentError('canonical_family_changed')
                if stage == 'planner':
                    parent = next((record for record in records[slot] if record['family']['stage'] == 'parent'), None)
                    if parent is None or parent['binding'] != parent_binding:
                        raise DevelopmentError('canonical_parent_conflict')
                if found == desired:
                    current()
                    return proof['workflow_id'], False
                previous = 'reserved' if stage == 'parent' else 'parent'
                if found['family']['stage'] != previous:
                    raise DevelopmentError('canonical_parent_conflict')
                binding = desired['binding']
                target_key = self.key(binding['scope'], binding['root_identity'])
                if target_key != key and target_key in state['records']:
                    raise DevelopmentError('canonical_parent_conflict')
                del state['records'][key]
                state['records'][target_key] = desired
                state['schema'] = 3
                self._save(state, current)
                return proof['workflow_id'], True

    def bind(self, binding, operation_id):
        """Reserve before preparing children; uncertainty never frees the row."""
        with self.lock():
            state = self._read()
            key = self.key(binding['scope'], binding['root_identity'])
            existing = state['records'].get(key)
            if existing is not None:
                if 'family' in existing:
                    raise DevelopmentError('canonical_family_required')
                if existing['binding'] != binding:
                    raise DevelopmentError('canonical_operation_conflict')
                return existing['operation_id'], False
            # Exclusive checkout remains fenced even under another ticket or
            # MCP-origin spelling. There is no implicit expiry/reset.
            if any(value['binding']['root_identity'] == binding['root_identity']
                   for value in state['records'].values()):
                raise DevelopmentError('canonical_checkout_busy')
            if len(state['records']) >= 4096:
                raise DevelopmentError('fence_full')
            state['records'][key] = {'binding': binding, 'operation_id': operation_id}
            if 'parent_scope' in binding:
                state['schema'] = max(state['schema'], 2)
            self._validate(state)
            encoded = canonical(state)
            if len(encoded) > 2 * 1024 * 1024:
                raise DevelopmentError('fence_full')
            self.private.replace('fence.json', encoded)
            return operation_id, True

    def attach(self, binding, operation_id, parent_binding, family_proof=None):
        """One exact parent-to-planner transition, never release/rekey a root."""
        if family_proof is not None:
            return self._attach_family(binding, operation_id, parent_binding, family_proof)
        with self.lock():
            state = self._read()
            key = self.key(binding['scope'], binding['root_identity'])
            found = state['records'].get(key)
            if found is not None and 'family' in found:
                raise DevelopmentError('canonical_family_required')
            if found == {'binding': binding, 'operation_id': operation_id}:
                return operation_id, False
            if (found != {'binding': parent_binding, 'operation_id': operation_id}
                    or 'parent_scope' not in binding
                    or binding['parent_scope'] != parent_binding.get('parent_scope')
                    or any(binding[k] != parent_binding.get(k) for k in
                        ('scope', 'root_identity', 'journal', 'child_scope'))
                    or binding['parent_scope']['operation_id'] != operation_id
                    or parent_binding['configuration_hash'] != binding['parent_scope']['configuration_hash']):
                raise DevelopmentError('canonical_parent_conflict')
            state['schema'] = max(state['schema'], 2)
            state['records'][key] = {'binding': binding, 'operation_id': operation_id}
            self._validate(state)
            encoded = canonical(state)
            if len(encoded) > 2 * 1024 * 1024:
                raise DevelopmentError('fence_full')
            self.private.replace('fence.json', encoded)
            return operation_id, True

    def inspect_checkout(self, root_identity):
        with self.lock():
            values=[value for value in self._read()['records'].values()
                    if value['binding']['root_identity']==list(root_identity)]
            return None if not values else decode(canonical(values[0]))

    def inspect_binding(self, scope, root_identity):
        """Local metadata only, for refusing a conflicting parent before MCP."""
        with self.lock():
            found=self._read()['records'].get(self.key(scope,root_identity))
            return None if found is None else decode(canonical(found))

    def verify(self, binding, operation_id):
        with self.lock():
            found = self._read()['records'].get(self.key(binding['scope'], binding['root_identity']))
            if found is not None and 'family' in found:
                raise DevelopmentError('canonical_family_required')
            if found != {'binding': binding, 'operation_id': operation_id}:
                raise DevelopmentError('canonical_fence_changed')
