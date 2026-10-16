"""Bounded private family intent and a canonical same-user local fence.

No transport, model, checkout write, adoption or implicit root release lives here.
The marker used by stop is independent of the long-running operation lock.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import math
import os
from pathlib import Path
import threading
import time
import uuid

from .dev_fence import DevelopmentFence, canonical_anchor
from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, canonical, digest, identity, sha256
from .dev_planner import _directory_pin
from .dev_runtime import development_work_id
from .dev_test_supervisor import _ancestor_identities
from .work_journal import _scope

MAXIMUM = 2 * 1024 * 1024
SLOTS = ('child_0', 'child_1', 'parent')


class FamilyJournal:
    def __init__(self, directory, *, _test_anchor=None, clock=time.time):
        self.private = PrivateDirectory(directory, create=True)
        self.directory, self.clock = self.private.path, clock
        self.anchor = canonical_anchor() if _test_anchor is None else Path(_test_anchor).absolute()
        self._mutex, self._locked, self.fd = threading.Lock(), False, None
        self.fence = None
        try:
            self.fd = os.open('family.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self.private.fd)
            self.private.validate_file(self.fd)
            self.fence = DevelopmentFence(_test_anchor=self.anchor)
        except OSError:
            self.close()
            raise DevelopmentError('family_lock_unavailable') from None
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fence is not None:
            self.fence.close();self.fence=None
        if self.fd is not None:
            os.close(self.fd);self.fd=None
        self.private.close()

    def __enter__(self):return self
    def __exit__(self,*_):self.close()

    @contextmanager
    def lock(self):
        if not self._mutex.acquire(False):raise DevelopmentError('family_busy')
        locked=False
        try:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB);locked=True
                self.private.verify();self.private.validate_file(self.fd)
                if identity(os.stat('family.lock',dir_fd=self.private.fd,follow_symlinks=False)) != identity(os.fstat(self.fd)):
                    raise DevelopmentError('family_lock_changed')
            except OSError:
                raise DevelopmentError('family_busy') from None
            self._locked=True
            yield
        finally:
            self._locked=False
            if locked:fcntl.flock(self.fd,fcntl.LOCK_UN)
            self._mutex.release()

    @staticmethod
    def _uuid(value):
        try:
            if str(uuid.UUID(value)) != value:raise ValueError
        except (ValueError,TypeError,AttributeError):
            raise DevelopmentError('family_corrupt') from None
        return value

    def _validate(self, value):
        if (type(value) is not dict or set(value) != {'schema','operation_id','workflow_ids',
                'configuration','configuration_sha256','revision','started_at','phase','payload','fence_ready'}
                or type(value['schema']) is not int or value['schema'] != 1
                or type(value['revision']) is not int or value['revision'] < 1
                or type(value['configuration']) is not dict or type(value['payload']) is not dict
                or type(value['fence_ready']) is not bool or type(value['phase']) is not str
                or value['phase'] not in ('prepared','opening','opened','bound','running','integrating',
                                          'finalizing','review_ready','needs_review','stopped')
                or type(value['started_at']) not in (int,float) or not math.isfinite(value['started_at'])
                or type(value['workflow_ids']) is not dict or set(value['workflow_ids']) != set(SLOTS)):
            raise DevelopmentError('family_corrupt')
        operation=self._uuid(value['operation_id'])
        if any(value['workflow_ids'][slot] != str(uuid.uuid5(uuid.UUID(operation),slot)) for slot in SLOTS):
            raise DevelopmentError('family_corrupt')
        sha256(value['configuration_sha256'])
        if digest(canonical(value['configuration'])) != value['configuration_sha256']:
            raise DevelopmentError('family_corrupt')
        config=value['configuration']
        try:
            _scope(config['scope'])
            if (set(config['roots']) != set(SLOTS) or set(config['child_stores']) != set(SLOTS)
                    or set(config['provisional_scopes']) != set(SLOTS)):raise ValueError
            roots=[]
            for slot in SLOTS:
                pin=config['roots'][slot]
                if type(pin) is not dict or set(pin) != {'path','identity'}:raise ValueError
                if (type(pin['identity']) is not list or len(pin['identity']) != 2
                        or any(type(v) is not int or v < 0 for v in pin['identity'])
                        or type(pin['path']) is not str or not pin['path'].startswith('/')):raise ValueError
                _scope(config['provisional_scopes'][slot])
                roots.append(tuple(pin['identity']))
                if set(config['child_stores'][slot]) != {'runs','runtime'}:raise ValueError
            if len(set(roots)) != 3:raise ValueError
        except (KeyError,TypeError,ValueError):
            raise DevelopmentError('family_corrupt') from None
        return value

    def _bindings(self, state):
        config=state['configuration'];journal=_directory_pin(self.directory)
        return {slot:{'scope':config['provisional_scopes'][slot],'root_identity':config['roots'][slot]['identity'],
            'journal':journal,'configuration_hash':state['configuration_sha256'],
            'child_scope':{'work_id':development_work_id(state['workflow_ids'][slot]),
                **config['child_stores'][slot]}} for slot in SLOTS}

    def _descriptor(self,state):
        config=state['configuration']
        return {'schema':1,'operation_id':state['operation_id'],
            'configuration_sha256':state['configuration_sha256'],'journal':_directory_pin(self.directory),
            'roots':deepcopy(config['roots']),
            'sessions':{slot:config['provisional_scopes'][slot]['board_session_id'] for slot in SLOTS}}

    def _boundaries(self, configuration):
        roots=configuration['roots']
        for slot,pin in roots.items():
            if _directory_pin(pin['path']) != pin:raise DevelopmentError('family_root_changed')
            ancestry=_ancestor_identities(pin['path'])
            for other,selected in roots.items():
                if other != slot and tuple(selected['identity']) in ancestry:
                    raise DevelopmentError('family_root_overlap')
            for path in (self.directory,self.anchor):
                private_ancestors=_ancestor_identities(path,allow_missing=True)
                if tuple(pin['identity']) in private_ancestors or private_ancestors[0] in ancestry:
                    raise DevelopmentError('family_private_overlap')
            for stores in configuration['child_stores'].values():
                for child in stores.values():
                    if _directory_pin(child['path']) != child:raise DevelopmentError('family_directory_changed')
                    child_ancestors=_ancestor_identities(child['path'])
                    if tuple(pin['identity']) in child_ancestors or tuple(child['identity']) in ancestry:
                        raise DevelopmentError('family_private_overlap')

    def prepare(self, configuration):
        with self.lock():
            try:
                os.stat('family.json',dir_fd=self.private.fd,follow_symlinks=False)
                state=self.read()
                if state['configuration'] != configuration:raise DevelopmentError('family_configuration_conflict')
            except FileNotFoundError:
                now=self.clock();operation=str(uuid.uuid4())
                state={'schema':1,'operation_id':operation,'workflow_ids':{slot:str(uuid.uuid5(uuid.UUID(operation),slot)) for slot in SLOTS},
                    'configuration':deepcopy(configuration),'configuration_sha256':digest(canonical(configuration)),
                    'revision':1,'started_at':now,'phase':'prepared','payload':{},'fence_ready':False}
                self._validate(state);self._boundaries(configuration)
                state['payload']={'fence_descriptor':self._descriptor(state),
                    'fence_reservations':self._bindings(state),'fence_targets':{}}
                encoded=canonical(state)
                if len(encoded)>MAXIMUM:raise DevelopmentError('family_document_too_large')
                self.private.create('family.json',encoded)  # UUID precedes every canonical binding/effect.
            self._boundaries(configuration)
            if (state['payload'].get('fence_descriptor')!=self._descriptor(state)
                    or state['payload'].get('fence_reservations')!=self._bindings(state)):
                raise DevelopmentError('family_configuration_changed')
            self.fence.reserve_family(state['payload']['fence_reservations'],state['payload']['fence_descriptor'])
            if not state['fence_ready']:
                state['fence_ready']=True;self.save(state)
            return self.read()

    def read(self):
        return self._validate(decode(self.private.read('family.json',MAXIMUM)))

    def verify(self,state):
        current=self.read()
        if any(current[key] != state[key] for key in ('operation_id','workflow_ids','configuration','configuration_sha256')):
            raise DevelopmentError('family_configuration_changed')
        if not current['fence_ready']:raise DevelopmentError('family_fence_incomplete')
        self._boundaries(current['configuration'])
        if (current['payload'].get('fence_descriptor')!=self._descriptor(current)
                or current['payload'].get('fence_reservations')!=self._bindings(current)):
            raise DevelopmentError('family_configuration_changed')
        self.fence.verify_family(current['payload']['fence_reservations'],
            current['payload']['fence_descriptor'],current['payload'].get('fence_targets',{}))

    def save(self,state):
        if not self._locked:raise DevelopmentError('family_lock_required')
        self._validate(state);current=self.read()
        if state['revision'] != current['revision']:raise DevelopmentError('family_revision_conflict')
        if any(state[key] != current[key] for key in ('operation_id','workflow_ids','configuration','configuration_sha256','started_at')):
            raise DevelopmentError('family_configuration_changed')
        for key in ('fence_descriptor','fence_reservations','family_id','slot_scopes','opening_intent','bindings','finalize_intent'):
            if key in current['payload'] and current['payload'][key]!=state['payload'].get(key):
                raise DevelopmentError('family_configuration_changed')
        for field in ('workflow_intents','accept_intents','accepted','audits'):
            for key,value in current['payload'].get(field,{}).items():
                if state['payload'].get(field,{}).get(key)!=value:
                    raise DevelopmentError('family_intent_conflict')
        for slot,targets in current['payload'].get('fence_targets',{}).items():
            for stage,proof in targets.items():
                if state['payload'].get('fence_targets',{}).get(slot,{}).get(stage)!=proof:
                    raise DevelopmentError('family_target_conflict')
        result=deepcopy(state);result['revision']+=1
        encoded=canonical(result)
        if len(encoded)>MAXIMUM:raise DevelopmentError('family_document_too_large')
        self.private.replace('family.json',encoded)
        state.clear();state.update(result)
        return deepcopy(result)

    def record_target(self,state,slot,stage,binding):
        if not self._locked:raise DevelopmentError('family_lock_required')
        if slot not in SLOTS or stage not in ('parent','planner'):
            raise DevelopmentError('family_target_conflict')
        proof={'descriptor':state['payload']['fence_descriptor'],'slot':slot,
            'workflow_id':state['workflow_ids'][slot],'server_family_id':state['payload']['family_id'],
            'stage':stage,'binding':deepcopy(binding)}
        prior=state['payload'].setdefault('fence_targets',{}).setdefault(slot,{}).get(stage)
        if prior is not None and prior!=proof:raise DevelopmentError('family_target_conflict')
        if prior is None:
            state['payload']['fence_targets'][slot][stage]=proof
            self.save(state)  # This exact target is durable before the global fence transfer.
        return deepcopy(proof)

    def request_stop(self):
        state=self.read();document={'schema':1,'operation_id':state['operation_id'],'stop':True}
        try:self.private.create('stop.json',canonical(document))
        except DevelopmentError as error:
            if error.code != 'private_file_exists':raise
            if decode(self.private.read('stop.json',512)) != document:raise DevelopmentError('family_stop_corrupt')
        return True

    def stopped(self):
        self.private.verify()
        try:os.stat('stop.json',dir_fd=self.private.fd,follow_symlinks=False)
        except FileNotFoundError:return False
        state=self.read()
        if decode(self.private.read('stop.json',512)) != {'schema':1,'operation_id':state['operation_id'],'stop':True}:
            raise DevelopmentError('family_stop_corrupt')
        return True
