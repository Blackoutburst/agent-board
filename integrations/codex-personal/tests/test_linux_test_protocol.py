"""Pure data codecs use a real local locked snapshot, never any executor."""
from copy import deepcopy
from dataclasses import FrozenInstanceError,replace
import json
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

import test_linux_input_snapshot as snapshot_fixture
from blackoutburst_agents.dev_broker import DevelopmentBroker
from blackoutburst_agents.dev_manifest import DevelopmentError,NamedTest,canonical,digest
from blackoutburst_agents.linux_input_snapshot import capture_input_snapshot, capture_input_snapshot_locked
from blackoutburst_agents.linux_test_protocol import (LinuxTestRequest,MAGIC,
    MAX_HEADER_BYTES,MAX_TOTAL_BYTES,encode_input_packet,decode_input_packet,
    linux_backend_id,validate_linux_request)


class LinuxTestProtocolTests(unittest.TestCase):
    def setUp(self):
        snapshot_fixture.LinuxInputSnapshotTests.setUp(self)
        self.plan_hash=digest(b'synthetic expected plan: no plan or host is qualified')
        manifest=self.broker.manifest
        manifest['tests']=[NamedTest('check',backend_id=linux_backend_id(self.plan_hash),
                                    timeout_seconds=20,max_output_bytes=4096).document()]
        self.broker.close()
        # Only the empty controlled fixture is rebuilt, before any operation.
        # This is not a migration or permission to rewrite a live journal.
        (self.journal/'manifest.json').write_bytes(canonical(manifest))
        state=json.loads((self.journal/'state.json').read_bytes())
        self.assertEqual(state['operations'],{})
        state['manifest_hash']=digest(canonical(manifest))
        (self.journal/'state.json').write_bytes(canonical(state))
        self.broker=DevelopmentBroker(self.journal,authorize_scope=self.authorize,enabled=True)
        self.addCleanup(self.broker.close)
        self.manifest=self.broker.manifest
        self.snapshot,self.contents=self.current_copy()
        self.packet=self.encode(self.contents)
        self.request=LinuxTestRequest(operation_id=digest(b'new synthetic operation'),
            workflow_id=self.snapshot['operation_id'],manifest_sha256=digest(canonical(self.manifest)),
            plan_sha256=self.plan_hash,scope_sha256=digest(canonical(self.manifest['scope'])),
            input_snapshot_sha256=digest(canonical(self.snapshot)),input_packet_sha256=digest(self.packet),
            planner_snapshot_sha256=self.planner['sha256'],root=str(self.root),
            root_identity=tuple(self.manifest['root_identity']),name='check',
            timeout_seconds=20,max_output_bytes=4096)

    def current_copy(self):
        with capture_input_snapshot(self.broker,fence=self.fence,binding=self.binding,
                operation_id=self.operation,planner_snapshot=self.planner) as snapshot:
            return snapshot.document(),{path:snapshot.read(path) for path in self.broker.manifest['files']}

    def encode(self,contents,**changes):
        args=dict(manifest=self.manifest,snapshot=self.snapshot,planner_snapshot=self.planner);args.update(changes)
        return encode_input_packet(contents,**args)

    def decode(self,packet,**changes):
        args=dict(manifest=self.manifest,snapshot=self.snapshot,planner_snapshot=self.planner);args.update(changes)
        return decode_input_packet(packet,**args)

    def validate(self,request,**changes):
        args=dict(manifest=self.manifest,snapshot=self.snapshot,planner_snapshot=self.planner,
                  expected_plan_sha256=self.plan_hash,input_packet=self.packet);args.update(changes)
        return validate_linux_request(request,**args)

    def header(self):
        prefix=len(MAGIC)+8;length=int.from_bytes(self.packet[len(MAGIC):prefix],'big')
        return json.loads(self.packet[prefix:prefix+length]),self.packet[prefix+length:]

    def changed_packet(self,mutate):
        header,body=self.header();mutate(header);raw=canonical(header)
        return MAGIC+len(raw).to_bytes(8,'big')+raw+body

    def test_round_trip_is_deterministic_and_binds_every_byte(self):
        self.assertEqual(self.decode(self.packet),self.contents)
        self.assertEqual(self.encode(dict(reversed(list(self.contents.items())))),self.packet)
        self.assertEqual(self.validate(self.request),self.request.document())
        with self.assertRaises(FrozenInstanceError):self.request.name='another'
        value=self.validate(self.request);value['root_identity'][0]+=1
        self.assertNotEqual(value,self.request.document())

    def test_complete_validations_perform_no_file_process_or_network_operation(self):
        with patch('os.open',side_effect=AssertionError('file open')),\
             patch('os.stat',side_effect=AssertionError('file stat')),\
             patch('subprocess.Popen',side_effect=AssertionError('process')),\
             patch('socket.socket',side_effect=AssertionError('network')):
            self.assertEqual(self.encode(self.contents),self.packet)
            self.assertEqual(self.decode(self.packet),self.contents)
            self.assertEqual(self.validate(self.request),self.request.document())
        self.authorize.assert_not_called()

    def test_request_bindings_cannot_be_exchanged(self):
        changes={'operation_id':True,'workflow_id':str(uuid.uuid4()),'manifest_sha256':'0'*64,
            'plan_sha256':'0'*64,'scope_sha256':'0'*64,'input_snapshot_sha256':'0'*64,
            'input_packet_sha256':'0'*64,'planner_snapshot_sha256':'0'*64,
            'root':'/another','root_identity':(0,1),'name':'another','timeout_seconds':21,
            'max_output_bytes':4097}
        for key,value in changes.items():
            with self.subTest(key=key),self.assertRaises(DevelopmentError):
                self.validate(replace(self.request,**{key:value}))
        # A distinct operation key is allowed as data, but has no launch effect.
        self.assertEqual(self.validate(replace(self.request,operation_id=digest(b'another')))
                         ['operation_id'],digest(b'another'))

    def test_request_exact_schema_types_and_bounds(self):
        original=self.request.document()
        for key,value in [('schema',True),('extra',1),('root_identity',[True,1]),
                          ('timeout_seconds',True),('timeout_seconds',20.0),('timeout_seconds',61),
                          ('max_output_bytes',False),('max_output_bytes',1048577),
                          ('workflow_id',17),('root',str(self.root)+'/../other'),
                          ('root',str(self.root)+'/\ud800')]:
            changed={**original,key:value}
            with self.subTest(key=key,value=value),self.assertRaises(DevelopmentError):self.validate(changed)
        for field in original:
            changed=deepcopy(original);del changed[field]
            with self.subTest(missing=field),self.assertRaises(DevelopmentError):self.validate(changed)

    def test_foreign_plan_manifest_scope_snapshot_and_planner_are_refused(self):
        manifest=deepcopy(self.manifest);manifest['scope']['owner_id']='foreign'
        snapshot=deepcopy(self.snapshot);snapshot['operation_id']=str(uuid.uuid4())
        planner=deepcopy(self.planner);planner['files']['right.txt']='0'*64
        planner['sha256']=digest(canonical(planner['files']))
        for change in [dict(expected_plan_sha256='0'*64),dict(manifest=manifest),
                       dict(snapshot=snapshot),dict(planner_snapshot=planner)]:
            with self.subTest(change=list(change)),self.assertRaises(DevelopmentError):self.validate(self.request,**change)

    def test_contents_must_be_exact_immutable_allowlisted_bytes(self):
        for contents in [{}, {**self.contents,'foreign':b''},
                         {**self.contents,'right.txt':bytearray(b'22\n')},
                         {**self.contents,'right.txt':b'23\n'},
                         {**self.contents,'right.txt':b'x'*(MAX_TOTAL_BYTES+1)}]:
            with self.subTest(keys=list(contents)),self.assertRaises(DevelopmentError):self.encode(contents)

    def test_packet_framing_truncation_and_trailing_bytes_are_refused(self):
        alternatives=[b'',self.packet[:len(MAGIC)+7],self.packet[:-1],self.packet+b'extra',
            b'WRONG'+self.packet[5:],bytearray(self.packet),
            MAGIC+(MAX_HEADER_BYTES+1).to_bytes(8,'big')+self.packet[len(MAGIC)+8:],
            self.packet+b'x'*(MAX_TOTAL_BYTES+MAX_HEADER_BYTES)]
        for packet in alternatives:
            with self.subTest(size=len(packet)),self.assertRaises(DevelopmentError):self.decode(packet)

    def test_header_offsets_types_paths_order_hashes_and_keys_are_strict(self):
        changes=[lambda h:h.update(schema=True),lambda h:h.update(snapshot_sha256='0'*64),
            lambda h:h.update(extra=1),lambda h:h['files'].reverse(),
            lambda h:h['files'].pop(),lambda h:h['files'].append(deepcopy(h['files'][0])),
            lambda h:h['files'][0].update(offset=True),lambda h:h['files'][1].update(offset=0),
            lambda h:h['files'][0].update(bytes=True),lambda h:h['files'][0].update(bytes=3.0),
            lambda h:h['files'][0].update(bytes=4*1024*1024),
            lambda h:h['files'][0].update(sha256='0'*64),
            lambda h:h['files'][0].update(path='../../outside'),
            lambda h:h['files'][0].update(path='.ssh/key'),lambda h:h['files'][0].update(extra=1)]
        for change in changes:
            with self.subTest(change=change),self.assertRaises(DevelopmentError):self.decode(self.changed_packet(change))

    def test_body_change_and_recomputed_header_cannot_cross_snapshot_binding(self):
        changed=self.packet[:-1]+bytes([self.packet[-1]^1])
        with self.assertRaises(DevelopmentError):self.decode(changed)
        header,body=self.header();body=b'21\n'+body[3:]
        header['files'][0]['sha256']=digest(body[:3]);raw=canonical(header)
        with self.assertRaises(DevelopmentError):self.decode(MAGIC+len(raw).to_bytes(8,'big')+raw+body)

    def test_noncanonical_duplicate_and_nonfinite_header_json_are_refused(self):
        header,body=self.header()
        values=[json.dumps(header,indent=2).encode(),
                canonical(header).replace(b'{',b'{"schema":"duplicate",',1),
                canonical(header).replace(b'"offset":0',b'"offset":NaN',1)]
        for raw in values:
            with self.subTest(raw=raw[:80]),self.assertRaises(DevelopmentError):
                self.decode(MAGIC+len(raw).to_bytes(8,'big')+raw+body)

    def test_packet_repeated_validation_has_no_admission_or_receipt_side_effect(self):
        before={str(path.relative_to(self.base)):path.read_bytes() for path in self.base.rglob('*') if path.is_file()}
        for _ in range(3):self.validate(self.request);self.decode(self.packet)
        after={str(path.relative_to(self.base)):path.read_bytes() for path in self.base.rglob('*') if path.is_file()}
        self.assertEqual(before,after)

    def test_entry_inode_budget_is_independent_of_content_budget(self):
        from blackoutburst_agents.linux_test_protocol import MAX_INPUT_ENTRIES
        with patch('blackoutburst_agents.linux_test_protocol.MAX_INPUT_ENTRIES',3):
            # Two files + one relative directory + root need four entries.
            with self.assertRaises(DevelopmentError):self.encode(self.contents)
        self.assertGreater(MAX_INPUT_ENTRIES,3)

    def test_packet_bounds_apply_to_both_encoding_and_decoding(self):
        for constant,maximum in [('MAX_FILES',1),('MAX_FILE_BYTES',2),
                                 ('MAX_TOTAL_BYTES',5),('MAX_HEADER_BYTES',20)]:
            with self.subTest(constant=constant),patch(
                    'blackoutburst_agents.linux_test_protocol.'+constant,maximum):
                with self.assertRaises(DevelopmentError):self.encode(self.contents)
                with self.assertRaises(DevelopmentError):self.decode(self.packet)
        with patch('blackoutburst_agents.linux_test_protocol.MAX_REQUEST_BYTES',20):
            with self.assertRaises(DevelopmentError):self.validate(self.request)

    def synthetic_context(self,contents,*,max_read_bytes=None):
        # Pure documents for codec tests: these are never live snapshot proofs.
        manifest=deepcopy(self.manifest);snapshot=deepcopy(self.snapshot)
        planner={'files':{path:digest(data) for path,data in contents.items()}}
        planner['sha256']=digest(canonical(planner['files']))
        for path,data in contents.items():
            manifest['files'][path]={'sha256':digest(data),'bytes':len(data)}
            snapshot['files'][path].update(sha256=digest(data),bytes=len(data))
            snapshot['files'][path]['signature'][2]=len(data)
        if max_read_bytes is not None:
            manifest['budgets']['max_read_bytes']=max_read_bytes
            snapshot['limits']['max_total_bytes']=max_read_bytes
        snapshot['planner_snapshot']=planner
        snapshot['manifest_hash']=digest(canonical(manifest))
        return dict(manifest=manifest,snapshot=snapshot,planner_snapshot=planner)

    def test_manifest_lower_total_read_bound_is_not_replaced_by_global_bound(self):
        context=self.synthetic_context(self.contents,max_read_bytes=5)
        with self.assertRaises(DevelopmentError):self.encode(self.contents,**context)
        with self.assertRaises(DevelopmentError):self.decode(self.packet,**context)

    def test_even_hash_consistent_non_text_or_secret_inputs_are_refused(self):
        for data in [b'\xff',b'a\x00b',b'-----BEGIN PRIVATE KEY-----',
                     b'{"client_secret":"synthetic-secret-for-test"}']:
            contents={**self.contents,'right.txt':data}
            context=self.synthetic_context(contents)
            with self.subTest(data=data),self.assertRaises(DevelopmentError):
                self.encode(contents,**context)

    def test_zero_length_input_remains_an_exact_inventory_entry(self):
        contents={**self.contents,'right.txt':b''}
        context=self.synthetic_context(contents)
        packet=self.encode(contents,**context)
        self.assertEqual(self.decode(packet,**context),contents)
        with self.assertRaises(DevelopmentError):self.decode(packet+b'\x00',**context)

    def test_darwin_or_unqualified_suite_identifier_is_not_linux_authority(self):
        for backend_id in ['unqualified','python-v1-'+self.plan_hash[:40],
                           linux_backend_id('0'*64)]:
            manifest=deepcopy(self.manifest);manifest['tests'][0]['backend_id']=backend_id
            snapshot=deepcopy(self.snapshot);snapshot['manifest_hash']=digest(canonical(manifest))
            request=replace(self.request,manifest_sha256=digest(canonical(manifest)),
                            input_snapshot_sha256=digest(canonical(snapshot)))
            with self.subTest(backend_id=backend_id),self.assertRaises(DevelopmentError):
                self.validate(request,manifest=manifest,snapshot=snapshot)

    def test_codec_roundtrip_under_existing_lease_never_reacquires_broker_lock(self):
        journal=self.broker.journal
        with journal.lock() as state:
            before=canonical(state)
            lease=journal.held_lock_lease()
            with capture_input_snapshot_locked(self.broker,lock_lease=lease,fence=self.fence,
                    binding=self.binding,operation_id=self.operation,planner_snapshot=self.planner) as held:
                document=held.document()
                contents={path:held.read(path) for path in self.manifest['files']}
                packet=encode_input_packet(contents,manifest=self.manifest,snapshot=document,planner_snapshot=self.planner)
                self.assertEqual(document,self.snapshot)
                self.assertEqual(packet,self.packet)
                self.assertEqual(validate_linux_request(self.request,manifest=self.manifest,snapshot=document,
                    planner_snapshot=self.planner,expected_plan_sha256=self.plan_hash,input_packet=packet),
                    self.request.document())
                self.assertTrue(journal.require_lock_lease(lease))
            self.assertTrue(journal.require_lock_lease(lease))
            self.assertEqual(canonical(state),before)
        self.authorize.assert_not_called()
        self.assertFalse(lease._active)

    def test_valid_packet_does_not_grant_authority_to_replay_an_expired_lease(self):
        journal=self.broker.journal
        with journal.lock():
            lease=journal.held_lock_lease()
            with capture_input_snapshot_locked(self.broker,lock_lease=lease,fence=self.fence,
                    binding=self.binding,operation_id=self.operation,planner_snapshot=self.planner) as held:
                document=held.document()
                contents={path:held.read(path) for path in self.manifest['files']}
                packet=encode_input_packet(contents,manifest=self.manifest,snapshot=document,planner_snapshot=self.planner)
        # Pure codecs keep validating immutable data; that is deliberately not
        # a live lock, Board grant, backend receipt or permission to run a test.
        self.assertEqual(decode_input_packet(packet,manifest=self.manifest,snapshot=document,
            planner_snapshot=self.planner),contents)
        self.assertEqual(validate_linux_request(self.request,manifest=self.manifest,snapshot=document,
            planner_snapshot=self.planner,expected_plan_sha256=self.plan_hash,input_packet=packet),self.request.document())
        with journal.lock():
            with self.assertRaises(DevelopmentError) as refused:
                with capture_input_snapshot_locked(self.broker,lock_lease=lease,fence=self.fence,
                        binding=self.binding,operation_id=self.operation,planner_snapshot=self.planner):
                    self.fail('Expired lease adopted from a valid packet')
            self.assertEqual(refused.exception.code,'journal_lock_lease_invalid')
        self.authorize.assert_not_called()
