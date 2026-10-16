"""Real local Board metadata and strict authorization, no native/model traffic."""
from copy import deepcopy
import unittest

import test_family_worker as fixture
from test_development_worker import FixtureBackend
from blackoutburst_agents.dev_manifest import DevelopmentError,authorize
from blackoutburst_agents.family_audit import manifest_document
from blackoutburst_agents.family_journal import SLOTS
from blackoutburst_agents.family_plan import build_configuration


class StrictInspectionBackend(FixtureBackend):
    """Match the native qualified_for authorization contract, without processes."""
    def __init__(self):
        super().__init__();self.inspections=[];self.before_inspection=None

    def qualified_for(self,manifest_hash,suite):
        self.inspections.append((manifest_hash,suite))
        if self.before_inspection:self.before_inspection()
        try:authorize(self.authorize_scope,self.manifest,'repo.run_test')
        except DevelopmentError:return False
        return super().qualified_for(manifest_hash,suite)

    def run(self,request):
        authorize(self.authorize_scope,self.manifest,'repo.run_test')
        return super().run(request)

    def qualify(self):
        authorize(self.authorize_scope,self.manifest,'repo.run_test')
        raise AssertionError('Fixture qualification must never execute')


@unittest.skipIf(fixture.Board is None,'Board sources not bundled')
class FamilyNativeAuthorizationTests(unittest.TestCase):
    make_manifest=fixture.FamilyWorkerTests.make_manifest
    ready=fixture.FamilyWorkerTests.ready
    transport=fixture.FamilyWorkerTests.transport
    ticket=fixture.FamilyWorkerTests.ticket

    def setUp(self):
        fixture.FamilyWorkerTests.setUp(self)
        for slot in SLOTS:
            self.backend[slot]=StrictInspectionBackend()
            def factory(directory,*,manifest,authorize_scope,chosen=slot):
                backend=self.backend[chosen]
                backend.manifest=deepcopy(manifest);backend.authorize_scope=authorize_scope
                return backend
            self.workflows[slot].backend_factory=factory

    def assert_no_execution(self):
        self.assertFalse(self.inferences)
        self.assertFalse(any(backend.calls for backend in self.backend.values()))
        self.assertFalse(any(worker.control.calls for worker in self.workflows.values()))

    def test_prepare_authorizes_only_qualification_and_opens_no_family(self):
        result=self.worker.prepare(self.request)
        self.assertEqual(result['status'],'prepared')
        self.assertTrue(self.backend['parent'].inspections)
        self.assertFalse(any(self.backend[slot].inspections for slot in SLOTS[:2]))
        self.assert_no_execution()
        self.assertFalse(any(name=='open_delegation_family' for name,_ in self.workflows['parent'].client.calls))

    def test_bind_inspects_strict_children_without_claiming_or_executing_them(self):
        self.worker.prepare(self.request);self.worker.open();state=self.journal.read()
        for slot in SLOTS[:2]:
            self.make_manifest(slot,state['payload']['slot_scopes'][slot],self.request['children'][SLOTS.index(slot)]['files'])
        result=self.worker.bind();self.assertEqual(result['status'],'bound')
        for slot in SLOTS[:2]:
            self.assertTrue(self.backend[slot].inspections)
            ticket=self.board.get_ticket(self.human,state['payload']['slot_scopes'][slot]['ticket_id'])['ticket']
            self.assertIsNone(ticket['claimant'])
            self.assertFalse(any(name in ('claim_ticket','renew_ticket') for name,_ in self.workflows[slot].client.calls))
        self.assert_no_execution()

    def test_strict_inspections_allow_three_workflows_with_existing_runtime_guards(self):
        self.ready();before={slot:len(self.backend[slot].inspections) for slot in SLOTS}
        result=self.worker.run();self.assertEqual(result['status'],'review_ready',result)
        self.assertEqual(result['children_accepted'],2)
        for slot in SLOTS:
            self.assertGreater(len(self.backend[slot].inspections),before[slot])
            self.assertEqual(len(self.backend[slot].calls),1)
            self.assertTrue(self.workflows[slot].control.calls)
        self.assertIsNotNone(self.ticket()['claimant'])

    def test_grants_withdrawn_at_prepare_inspection_refuse_before_fence_or_open(self):
        self.backend['parent'].before_inspection=lambda:self.board.set_policy(self.human,True,True,True,True,[],[])
        with self.assertRaises(DevelopmentError) as error:self.worker.prepare(self.request)
        self.assertEqual(error.exception.code,'development_backend_unqualified')
        self.assertFalse((self.base/'family'/'family.json').exists())
        self.assert_no_execution()

    def test_grants_withdrawn_at_bind_inspection_refuse_before_child_claim(self):
        self.worker.prepare(self.request);self.worker.open();state=self.journal.read()
        for slot in SLOTS[:2]:
            self.make_manifest(slot,state['payload']['slot_scopes'][slot],self.request['children'][SLOTS.index(slot)]['files'])
        self.backend['child_0'].before_inspection=lambda:self.board.set_policy(self.human,True,True,True,True,[],[])
        with self.assertRaises(DevelopmentError) as error:self.worker.bind()
        self.assertEqual(error.exception.code,'development_backend_unqualified')
        self.assertNotIn('bindings',self.journal.read()['payload'])
        self.assert_no_execution()

    def test_callback_refuses_other_action_resource_scope_and_child_before_open(self):
        config=build_configuration(self.request,self.workflows,self.registrations,clock=lambda:self.now)
        callback=self.worker._qualification_authorizer(config,'parent')
        manifest,_=manifest_document(self.items['parent']['broker_directory'])
        self.assertTrue(callback(manifest['scope'],'repo.run_test',manifest['resource']))
        for action in ('repo.read_file','repo.patch_file','shell'):
            self.assertFalse(callback(manifest['scope'],action,manifest['resource']))
        self.assertFalse(callback(manifest['scope'],'repo.run_test','repo:other'))
        self.assertFalse(callback({**manifest['scope'],'session_id':'other'},'repo.run_test',manifest['resource']))
        with self.assertRaises(DevelopmentError):self.worker._qualification_authorizer(config,'child_0')
        self.assert_no_execution()

    def test_inspected_metadata_stays_readable_but_callback_is_revoked_before_yield(self):
        config=build_configuration(self.request,self.workflows,self.registrations,clock=lambda:self.now)
        with self.worker._qualification_resources(config,'parent') as resources:
            manifest,policy,backend=resources
            self.assertTrue(backend.inspections)
            self.assertEqual(manifest.manifest_hash,config['parent_manifest_sha256'])
            self.assertTrue(policy.fingerprint)
            self.assertEqual(backend.document['plan_hash'],self.backend['parent'].document['plan_hash'])
            self.assertFalse(backend.qualified_for(manifest.manifest_hash,manifest.manifest['tests'][0]))
            with self.assertRaises(DevelopmentError):backend.run(None)
            with self.assertRaises(DevelopmentError):backend.qualify()
            self.assertFalse(backend.authorize_scope(manifest.manifest['scope'],'repo.run_test',manifest.manifest['resource']))
        self.assertFalse(backend.authorize_scope(manifest.manifest['scope'],'repo.run_test',manifest.manifest['resource']))
        self.assert_no_execution()

    def test_inspection_callback_is_revoked_when_resource_entry_raises(self):
        config=build_configuration(self.request,self.workflows,self.registrations,clock=lambda:self.now)
        backend=self.backend['parent']
        def fail():raise RuntimeError('fixture entry interrupted')
        backend.before_inspection=fail
        with self.assertRaises(RuntimeError):
            with self.worker._qualification_resources(config,'parent'):
                self.fail('Interrupted resources must not yield')
        self.assertFalse(backend.authorize_scope(backend.manifest['scope'],'repo.run_test',backend.manifest['resource']))
        self.assert_no_execution()

    def test_grants_withdrawn_at_workflow_inspection_refuse_before_child_claim(self):
        self.ready()
        self.backend['child_0'].before_inspection=lambda:self.board.set_policy(self.human,True,True,True,True,[],[])
        result=self.worker.run()
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(result['reason'],'development_backend_unqualified')
        for slot in SLOTS[:2]:
            self.assertFalse(any(name in ('claim_ticket','renew_ticket') for name,_ in self.workflows[slot].client.calls))
        self.assert_no_execution()
