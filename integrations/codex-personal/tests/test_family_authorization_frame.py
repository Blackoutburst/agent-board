"""Fresh bounded family checks against local Board fixtures, no remote effects."""
from collections import Counter
from copy import deepcopy
import json
from unittest import TestCase
from unittest.mock import patch

import test_family_worker as fixture
from blackoutburst_agents.context import (ContextError,ContextLimits,ContextScope,
    build_context,build_context_from_reads,read_context_pages)
from blackoutburst_agents.development_authorization import DevelopmentAuthorization
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.family_worker import FamilyAuthorizationGuard


class FamilyAuthorizationFrameTests(TestCase):
    make_manifest = fixture.FamilyWorkerTests.make_manifest
    ready = fixture.FamilyWorkerTests.ready
    transport = fixture.FamilyWorkerTests.transport

    def setUp(self):
        fixture.FamilyWorkerTests.setUp(self)
        if self._testMethodName in ('test_parent_and_child_pagination_remains_bounded',
                                    'test_overlap_pages_remain_bounded_to_fourteen_reads'):
            thread=self.board.get_ticket(self.human,self.scope['ticket_id'])['ticket']['thread_id']
            for i in range(45):
                self.board.post_message(self.human,thread,'bounded root '+str(i),idempotency_key='root-page-'+str(i))
        self.ready()
        self.state = self.journal.read()
        self.slot = 'child_0'
        self.client = self.workflows[self.slot].client
        self.parent = self.workflows['parent'].client
        self.child_scope = self.state['payload']['slot_scopes'][self.slot]
        response = self.board.get_ticket(self.client.actor,self.child_scope['ticket_id'])
        self.board.claim_ticket(self.client.actor,self.child_scope['ticket_id'],response['ticket']['version'])
        manifest = json.loads((self.base/'child_0-broker/manifest.json').read_text())
        self.guard = self.worker._worker(self.state,self.slot).delegation_guard
        self.authority = DevelopmentAuthorization(self.client,scope=self.child_scope,manifest=manifest,
            registration=self.registrations[self.slot],clock=lambda:self.now,delegation_guard=self.guard)
        self.clear_calls()

    def clear_calls(self):
        self.client.calls.clear();self.parent.calls.clear()

    def counts(self):
        return Counter(name for client in (self.client,self.parent) for name,_ in client.calls)

    def snapshot(self):
        return self.authority.snapshot(require_claim=True)

    def no_effects(self):
        self.assertFalse(self.inferences)
        self.assertFalse(any(worker.control.calls for worker in self.workflows.values()))
        self.assertFalse(any(backend.calls for backend in self.backend.values()))

    def test_exact_ten_reads_and_no_cache_between_boundaries(self):
        self.assertIs(type(self.guard),FamilyAuthorizationGuard)
        first = self.snapshot()
        self.assertEqual(self.counts(),{'get_work':2,'get_ticket':4,'get_thread':2,'get_delegation_family':2})
        self.authority.seal(first)
        self.clear_calls()
        grant = self.authority()
        self.assertEqual(grant['claim_id'],self.authority.claim_id)
        self.assertEqual(self.counts(),{'get_work':2,'get_ticket':4,'get_thread':2,'get_delegation_family':2})
        self.assertEqual(first['context'],self.snapshot()['context'])
        self.assertEqual(sum(self.counts().values()),20)
        self.no_effects()

    def test_context_matches_independent_existing_composition(self):
        result = self.snapshot()
        scope = ContextScope(**{key:self.child_scope[key] for key in
                                ('owner_id','agent_id','project_id','ticket_id')})
        expected = build_context(self.client.call,scope=scope,registration=self.registrations[self.slot])
        self.assertEqual(result['context'],expected)
        self.no_effects()

    def test_opaque_callback_keeps_two_independent_full_family_checks(self):
        self.authority.delegation_guard = lambda response:self.guard(response)
        self.snapshot()
        self.assertEqual(sum(self.counts().values()),20)
        self.assertEqual(self.counts()['get_delegation_family'],2)
        self.no_effects()

    def test_foreign_scope_manifest_registration_and_client_refused(self):
        for field,value in [('scope',{**self.authority.scope,'ticket_id':'foreign'}),
                            ('manifest',{**self.authority.manifest,'resource':'foreign'}),
                            ('registration',{}),('client',self.parent)]:
            before = getattr(self.authority,field)
            try:
                setattr(self.authority,field,value)
                with self.subTest(field=field),self.assertRaises(DevelopmentError):self.snapshot()
            finally:setattr(self.authority,field,before)
        self.assertEqual(sum(self.counts().values()),0)
        self.no_effects()

    def intercept(self,client,hook):
        original = client.call
        def call(name,args):
            response = original(name,args)
            hook(name,args,response)
            return response
        return patch.object(client,'call',side_effect=call)

    def test_change_each_ticket_project_claim_and_lease_observation(self):
        mutations = [lambda r:r['ticket'].update(version=r['ticket']['version']+1),
            lambda r:r['project'].update(version=r['project']['version']+1),
            lambda r:r['ticket'].update(claimant=None),
            lambda r:r['ticket'].update(lease_expires_at=self.now),
            lambda r:r['project'].update(archived=True)]
        for who in ('parent','child_0'):
            client = self.workflows[who].client
            for ordinal in (1,2):
                for mutate in mutations:
                    calls=0
                    def hook(name,args,response):
                        nonlocal calls
                        if name=='get_ticket':
                            calls+=1
                            if calls==ordinal:mutate(response)
                    with self.subTest(who=who,ordinal=ordinal,mutate=mutate):
                        with self.intercept(client,hook),self.assertRaises((DevelopmentError,ContextError)):
                            self.snapshot()
        self.no_effects()

    def test_family_stop_or_plan_change_on_either_view_refuses(self):
        for ordinal in (1,2):
            for field,value in [('should_stop',True),('plan_sha256','0'*64),('revision',True)]:
                calls=0
                def hook(name,args,response):
                    nonlocal calls
                    if name=='get_delegation_family':
                        calls+=1
                        if calls==ordinal:response[field]=value
                with self.subTest(ordinal=ordinal,field=field):
                    with self.intercept(self.parent,hook),self.assertRaises(DevelopmentError):self.snapshot()
        self.no_effects()

    def test_real_policy_revocation_during_history_is_caught_by_final_family_view(self):
        def hook(name,args,response):
            if name=='get_thread':
                self.board.set_policy(self.human,True,True,True,True,['repo.read_file'],['repo:fixture'])
        with self.intercept(self.client,hook),self.assertRaises(DevelopmentError):self.snapshot()
        self.no_effects()

    def test_real_profile_change_during_history_refuses(self):
        def hook(name,args,response):
            if name=='get_thread':
                profile=self.board.get_agent_profile(self.human,self.child_scope['agent_id'])
                self.board.update_agent_profile(self.human,profile['id'],profile['version'],description='changed')
        with self.intercept(self.client,hook),self.assertRaises((DevelopmentError,ContextError)):self.snapshot()
        self.no_effects()

    def test_sealed_context_change_is_refused(self):
        self.authority.seal(self.snapshot())
        thread=self.board.get_ticket(self.human,self.child_scope['ticket_id'])['ticket']['thread_id']
        self.board.post_message(self.human,thread,'new human context',idempotency_key='new-context')
        with self.assertRaises(DevelopmentError):self.snapshot()
        self.no_effects()

    def test_local_stop_and_deadline_during_collection_refuse(self):
        original=self.now
        def deadline(name,args,response):
            if name=='get_thread':self.now=self.state['configuration']['opening']['limits']['deadline']
        with self.intercept(self.client,deadline),self.assertRaises(DevelopmentError):self.snapshot()
        self.now=original
        def stop(name,args,response):
            if name=='get_thread':self.journal.request_stop()
        with self.intercept(self.client,stop),self.assertRaises(DevelopmentError):self.snapshot()
        self.no_effects()

    def test_prior_result_mutation_does_not_change_next_observation(self):
        first=self.snapshot();first['policy']['actions'].clear()
        first['context']['data']['ticket']['title']='altered'
        second=self.snapshot()
        self.assertTrue(second['policy']['actions'])
        self.assertNotEqual(second['context']['data']['ticket']['title'],'altered')
        self.no_effects()

    def test_parent_and_child_pagination_remains_bounded(self):
        thread=self.board.get_ticket(self.human,self.child_scope['ticket_id'])['ticket']['thread_id']
        for i in range(45):
            self.board.post_message(self.human,thread,'bounded child '+str(i),idempotency_key='child-page-'+str(i))
        self.clear_calls();self.snapshot()
        self.assertEqual(sum(self.counts().values()),12)
        self.assertEqual(self.counts()['get_thread'],4)
        self.no_effects()

    def test_overlap_pages_remain_bounded_to_fourteen_reads(self):
        thread=self.board.get_ticket(self.human,self.child_scope['ticket_id'])['ticket']['thread_id']
        for i in range(45):
            self.board.post_message(self.human,thread,'bounded child '+str(i),idempotency_key='child-overlap-'+str(i))
        def overlapping():
            boundary=None
            def hook(name,args,response):
                nonlocal boundary
                if name!='get_thread':return
                if 'before' not in args:
                    boundary=deepcopy(response['messages'][0])
                elif args['before']==boundary['id'] and args['limit']==20:
                    # The existing composer explicitly accepts the exact
                    # boundary duplicate, then follows its real next cursor.
                    response['messages']=response['messages'][1:]+[boundary]
                    response['next_before']=response['messages'][0]['id']
            return hook
        self.clear_calls()
        with self.intercept(self.client,overlapping()),self.intercept(self.parent,overlapping()):
            result=self.snapshot()
        self.assertEqual(sum(self.counts().values()),14)
        self.assertEqual(self.counts()['get_thread'],6)
        self.assertEqual(result['context']['sources']['history']['selected_count'],40)
        self.no_effects()
