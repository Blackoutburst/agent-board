"""Independent bounded controller regressions; fake model/control only."""
from copy import deepcopy
import unittest
import uuid

from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
import test_dev_planner as fixtures


class PlannerIndependentReview(unittest.TestCase):
    create = fixtures.PlannerTests.create
    transport = fixtures.PlannerTests.transport
    action = fixtures.PlannerTests.action

    def setUp(self):
        fixtures.PlannerTests.setUp(self)

    def test_runtime_device_profile_mismatch_is_refused_before_any_model(self):
        self.actions = [self.action('read',path='main.py')]
        original = self.control.call

        def mismatched(name,args):
            result = original(name,args)
            if name == 'reserve_execution':
                result['execution']['profile_version'] += 1
                self.control.rows[result['execution']['id']]['profile_version'] += 1
            return result

        self.control.call = mismatched
        result = self.create().run()
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(self.transports,[])
        self.assertEqual(self.backend.calls,[])
        self.assertEqual((self.root/'main.py').read_text(),'value = 1\n')

    def test_disabled_probe_does_not_block_later_explicit_enabled_operation(self):
        with self.assertRaises(DevelopmentError) as error:
            self.create(enabled=False)
        self.assertEqual(error.exception.code,'planner_disabled')
        self.assertFalse((self.base/'anchor').exists())
        self.assertFalse((self.private/'planner.json').exists())
        self.assertEqual(self.store._db.execute('SELECT COUNT(*) FROM works').fetchone()[0],0)
        self.assertEqual(self.policy.validations,0)
        self.assertEqual(self.control.calls,[])
        self.actions = [self.action('test',name='unit'),self.action('final',summary='Review only')]
        self.assertEqual(self.create().run()['status'],'review_ready')
        self.assertEqual(len(self.transports),2)
        self.assertEqual(len(self.backend.calls),1)

    def test_anchor_size_refusal_preserves_previously_readable_records(self):
        planner = self.create()
        fence = planner.fence
        template = deepcopy(planner.binding)
        template['root_identity'] = [11111111,10000000]
        operation = str(uuid.uuid4())
        first_key = fence.key(template['scope'],template['root_identity'])
        first = {'schema':1,'records':{first_key:{'binding':template,'operation_id':operation}}}
        empty = {'schema':1,'records':{}}
        entry_bytes = len(canonical(first)) - len(canonical(empty))
        maximum = 2 * 1024 * 1024
        count = 1 + (maximum - len(canonical(first))) // (entry_bytes + 1)
        self.assertLess(count,4096)
        records = {}
        for number in range(count):
            binding = deepcopy(template)
            binding['root_identity'][1] += number
            records[fence.key(binding['scope'],binding['root_identity'])] = {
                'binding':binding,'operation_id':operation}
        state = {'schema':1,'records':records}
        encoded = canonical(state)
        self.assertLessEqual(len(encoded),maximum)
        additional = deepcopy(template)
        additional['root_identity'] = [22222222,22222222]
        next_key = fence.key(additional['scope'],additional['root_identity'])
        self.assertGreater(len(canonical({'schema':1,'records':{
            **records,next_key:{'binding':additional,'operation_id':operation}}})),maximum)
        # Seed only bounded, validated synthetic journal metadata, not a set of
        # live checkouts. No loop of filesystem effects or model runs occurs.
        fence.private.replace('fence.json',encoded)
        self.assertEqual(len(fence._read()['records']),count)
        with self.assertRaises(DevelopmentError) as error:
            fence.bind(additional,str(uuid.uuid4()))
        self.assertEqual(error.exception.code,'fence_full')
        self.assertEqual(fence.private.read('fence.json',maximum),encoded)
        self.assertEqual(len(fence._read()['records']),count)
        self.assertEqual(self.transports,[])

    def test_resource_withdrawal_after_read_prevents_next_model_receiving_result(self):
        self.actions = [self.action('read',path='main.py'),self.action('test',name='unit')]

        def withdraw(name,args):
            if name == 'finish_execution':
                row = self.control.rows[args['execution_id']]
                if row['provider'] == 'local_broker':
                    self.grant['resources'] = []

        self.control.after = withdraw
        result = self.create().run()
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(len(self.transports),1)
        self.assertEqual(self.backend.calls,[])

    def test_resource_withdrawal_during_model_is_rejected_before_result_becomes_action(self):
        self.actions = [self.action('read',path='main.py')]

        def withdraw(callbacks):
            self.grant['resources'] = []
            self.assertFalse(callbacks['should_cancel']())

        self.during_model = withdraw
        result = self.create().run()
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(len(self.transports),1)
        self.assertEqual(self.backend.calls,[])
        self.assertEqual(len(self.control.rows),1)

    def test_review_ready_and_cached_reply_are_a_dated_snapshot_after_external_write(self):
        self.actions = [self.action('test',name='unit'),self.action('final',summary='Review only')]

        def change_after_inspection(name,args):
            if name == 'finish_execution':
                row = self.control.rows[args['execution_id']]
                if row['provider'] == 'local_broker' and row['model'] == 'read_file':
                    (self.root/'main.py').write_text('external write after inspection\n')

        self.control.after = change_after_inspection
        planner = self.create()
        result = planner.run()
        self.assertEqual(result['status'],'review_ready')
        review = result['review']
        self.assertEqual(review['evidence_kind'],'inspected_snapshot')
        self.assertEqual(review['inspected_at'],self.clock)
        self.assertEqual(review['snapshot_sha256'],digest(canonical({'main.py':digest(b'value = 1\n')})))
        self.assertNotEqual(digest((self.root/'main.py').read_bytes()),digest(b'value = 1\n'))
        self.assertEqual(review['diff']['files'],[])
        before = len(self.control.calls)
        self.clock += 1
        self.assertEqual(planner.run(),result)
        self.assertEqual(len(self.control.calls),before)
        self.assertEqual(len(self.transports),2)
        self.assertEqual(len(self.backend.calls),1)
