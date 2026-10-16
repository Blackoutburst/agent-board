"""Explicit Board-backed development preparation and execution.

Preparation reads/seals existing controller manifests and qualified policies.
Only run claims/renews the selected ticket, then seals its actual final context.
No queue, listener, implicit registration, model fallback or automatic completion.
"""
from contextlib import ExitStack, contextmanager, closing
from copy import deepcopy
from dataclasses import replace
import math
import os
from pathlib import Path
import time

from .context import ContextScope, _hash
from .development_authorization import DevelopmentAuthorization
from .dev_broker import DevelopmentBroker
from .dev_fence import DevelopmentFence, canonical_anchor
from .dev_journal import DevelopmentJournal
from .dev_manifest import DevelopmentError, canonical, check_content, digest, opaque
from .dev_planner import DevelopmentPlanner, _agent_id, _directory_pin
from .dev_planner_journal import PlannerBudgets
from .dev_test_supervisor import SupervisedNativeNamedPythonBackend, _ancestor_identities
from .planner_policy import PlannerPolicy
from .work_journal import JournalError


class DevelopmentBoardWorker:
    def __init__(self, journal, store, runtime_journal, client, control_client, *,
                 backend_factory=SupervisedNativeNamedPythonBackend, policy_factory=PlannerPolicy,
                 planner_factory=DevelopmentPlanner, _transport=None, _test_anchor=None,
                 _allow_fixture_backend=False, clock=time.time):
        self.journal, self.store, self.runtime = journal, store, runtime_journal
        self.client, self.control = client, control_client
        self.backend_factory, self.policy_factory, self.planner_factory = backend_factory, policy_factory, planner_factory
        self.transport, self.test_anchor, self.allow_fixture, self.clock = _transport, _test_anchor, _allow_fixture_backend, clock

    @staticmethod
    def _public(op, reason='', planner=None):
        return {'operation_id':op['id'], 'stage':op['stage'],
            'status':'complete' if op['stage']=='complete' else 'review_ready' if op['stage']=='result_ready' else
                     'needs_review' if op['stage']=='needs_review' or
                         op['payload'].get('planner_receipt',{}).get('status')=='needs_review' else 'prepared' if op['stage']=='new' else 'pending',
            'reason':reason or op['payload'].get('reason',''),
            'planner':planner if planner is not None else op['payload'].get('planner_receipt')}

    def _save(self, op, stage=None, **values):
        return self.journal.update(op['id'],op['revision'], stage=stage or op['stage'],
                                   payload={**op['payload'], **values})

    @contextmanager
    def _resources(self, broker_path, backend_path, policy_path, *, authorize):
        with ExitStack() as stack:
            manifest = stack.enter_context(closing(DevelopmentJournal(broker_path)))
            policy = self.policy_factory(policy_path)
            stack.callback(policy.close)
            policy.validate_for(policy.executable, policy.workspace)  # Offline pins only.
            backend = self.backend_factory(backend_path, manifest=manifest.manifest, authorize_scope=authorize)
            stack.callback(backend.close)
            if (not manifest.manifest['tests'] or any(not backend.qualified_for(manifest.manifest_hash,suite)
                    for suite in manifest.manifest['tests'])):
                raise DevelopmentError('development_backend_unqualified')
            yield manifest, policy, backend

    @staticmethod
    def _backend_pin(backend):
        return digest(canonical({'backend_id':backend.backend_id, 'plan_hash':backend.document['plan_hash'],
                                  'supervision':backend.supervision}))

    def prepare(self, *, scope, request_id, instruction, broker_directory, backend_directory,
                planner_policy_directory, model, budgets=None):
        """Pure read/plan phase; no claim, Runtime reservation, model or test."""
        if (type(instruction) is not str or not instruction.strip()
                or len(instruction.encode('utf-8','surrogatepass'))>16384):
            raise DevelopmentError('invalid_planner_instruction')
        check_content(instruction.encode('utf-8','strict'))
        opaque(model, 'invalid_development_model', 128)
        limits=(budgets or PlannerBudgets()).document()
        with self.journal.lock(scope):
            prior=self.journal.find(scope,request_id)
            if prior is not None:
                self.client.adopt_registration()
                registration=prior['payload']['registration']
            else:
                registration=self.client.register_initial()
            # Grants must be checked before any claim/Runtime/model or canonical
            # reservation. An empty policy cannot bootstrap its own rights.
            with closing(DevelopmentJournal(broker_directory)) as manifest:
                authorization=DevelopmentAuthorization(self.client,scope=scope,manifest=manifest.manifest,
                                                        registration=registration,clock=self.clock,stop_requested=lambda:self._stopped(prior['id']) if prior else False)
                initial=authorization.snapshot()
            def preflight(given,action,resource):
                try:
                    authorization._registration()
                    policy=authorization._policy()
                    return (given==authorization.manifest['scope'] and action in policy['actions']
                            and resource==authorization.manifest['resource'] and resource in policy['resources'])
                except Exception:
                    return False
            with self._resources(broker_directory,backend_directory,planner_policy_directory,authorize=preflight) as resources:
                manifest,policy,backend=resources
                pins={key:_directory_pin(path) for key,path in {
                    'broker':broker_directory,'backend':backend_directory,'planner_policy':planner_policy_directory,
                    'runs':self.store.state_dir,'runtime':self.runtime.directory,'journal':self.journal.directory,
                    'planner_workspace':policy.workspace}.items()}
                connection=getattr(self.client,'connection_path',None)
                if connection is not None:
                    connection_ancestors=_ancestor_identities(Path(connection).parent)
                    if (tuple(manifest.manifest['root_identity']) in connection_ancestors
                            or tuple(policy.workspace_identity) in connection_ancestors):
                        raise DevelopmentError('development_connection_overlap')
                root_identity=tuple(manifest.manifest['root_identity'])
                root_ancestry=_ancestor_identities(manifest.manifest['root'])
                for pin in pins.values():
                    ancestry=_ancestor_identities(pin['path'])
                    if root_identity in ancestry or tuple(pin['identity']) in root_ancestry:
                        raise DevelopmentError('development_private_overlap')
                config={'schema':1,'kind':'development','instruction':instruction,'model':model,'budgets':limits,
                    'pins':pins,'manifest_hash':manifest.manifest_hash,'policy_hash':policy.fingerprint,
                    'backend_hash':self._backend_pin(backend)}
                if prior is not None and prior['configuration']!=config:
                    raise DevelopmentError('development_prepare_conflict')
                op=self.journal.begin(scope,request_id,config)
                if prior is not None:
                    expected=op['payload'].get('initial_context')
                    if op['stage']=='new' and (expected is None or expected['fingerprint']!=initial['context']['fingerprint']):
                        raise DevelopmentError('development_context_changed')
                    return self._public(op)
                op=self._save(op,registration=registration,initial_context=initial['context'],
                    initial_selected_ticket=initial['selected_ticket'], initial_selected_project=initial['selected_project'])
                return self._public(op)

    def _pin_configuration(self, op, manifest, policy, backend):
        config=op['configuration']
        if (config.get('schema')!=1 or config.get('kind')!='development'
                or config['manifest_hash']!=manifest.manifest_hash or config['policy_hash']!=policy.fingerprint
                or config['backend_hash']!=self._backend_pin(backend)):
            raise DevelopmentError('development_configuration_changed')
        for pin in config['pins'].values():
            if _directory_pin(pin['path'])!=pin:
                raise DevelopmentError('development_directory_changed')
        if (config['pins']['runs']['path']!=str(self.store.state_dir)
                or config['pins']['runtime']['path']!=str(self.runtime.directory)
                or config['pins']['journal']['path']!=str(self.journal.directory)):
            raise DevelopmentError('development_child_scope_changed')

    def _parent_binding(self, op, manifest, policy):
        # Local work identity is allocated before MCP claim. No model turn is
        # begun by create_work, and every later constructor reuses this UUID.
        work=self.store.create_work(owner_id=op['scope']['owner_id'], agent_id=_agent_id(op['scope']),
            project_id=op['scope']['project_id'],ticket_id=op['scope']['ticket_id'],
            workspace=policy.workspace,executable=policy.executable,model=op['configuration']['model'])
        config=op['configuration']
        parent={'operation_id':op['id'],'journal':config['pins']['journal'],
            'configuration_hash':digest(canonical({'configuration':config,
                'initial_context_hash':op['payload']['initial_context']['fingerprint']}))}
        return {'scope':op['scope'],'root_identity':manifest.manifest['root_identity'],
            'journal':config['pins']['broker'],'configuration_hash':parent['configuration_hash'],
            'child_scope':{'work_id':work['id'],'runs':config['pins']['runs'],'runtime':config['pins']['runtime']},
            'parent_scope':parent}

    @staticmethod
    def _transition_context(op, current):
        """Accept only the known claim/renew status+revision change."""
        initial=op['payload']['initial_context']
        expected_ticket=deepcopy(op['payload']['initial_selected_ticket'])
        expected_ticket['version']+=1
        expected_ticket['status']='in_progress'
        sources=current['context']['sources']
        if (current['ticket']['version']!=expected_ticket['version']
                or sources['ticket']['sha256']!=_hash(expected_ticket)
                or any(sources[key]!=initial['sources'][key] for key in ('agent','project','thread','history','messages'))):
            raise DevelopmentError('development_context_changed_during_claim')

    def _claim(self, op, authorization):
        scope=op['scope']
        if op['stage']=='new':
            initial=authorization.snapshot()
            if initial['context']['fingerprint']!=op['payload']['initial_context']['fingerprint']:
                raise DevelopmentError('development_context_changed')
            ticket=initial['ticket']
            if ticket['claimant'] is not None and not authorization.own_claim(ticket):
                raise DevelopmentError('development_foreign_or_expired_claim')
            action='renew_ticket' if authorization.own_claim(ticket) else 'claim_ticket'
            op=self._save(op,'claiming',claim_intent={'action':action,'expected_version':ticket['version'],
                'previous_lease':ticket['lease_expires_at']})
        if op['stage']=='claiming':
            intent=op['payload']['claim_intent']
            current=authorization.snapshot()
            ticket=current['ticket']
            if ticket['version']==intent['expected_version']:
                if current['context']['fingerprint']!=op['payload']['initial_context']['fingerprint']:
                    raise DevelopmentError('development_context_changed')
                if intent['action']=='renew_ticket' and not authorization.own_claim(ticket):
                    raise DevelopmentError('development_claim_lost')
                if intent['action']=='claim_ticket' and ticket['claimant'] is not None:
                    raise DevelopmentError('development_foreign_or_expired_claim')
                if self._stopped(op['id']):
                    raise DevelopmentError('development_stop_requested')
                try:
                    self.client.call(intent['action'],{'ticket_id':scope['ticket_id'],
                        'expected_version':intent['expected_version']})
                except Exception:
                    # Read exact optimistic revision/claim; never take another
                    # key or invoke a recovery of an expired/foreign claim.
                    pass
                current=authorization.snapshot(require_claim=True,remaining=10)
                ticket=current['ticket']
            if not authorization.own_claim(ticket,remaining=10):
                raise DevelopmentError('development_claim_lost')
            self._transition_context(op,current)
            if (intent['action']=='renew_ticket' and (type(intent['previous_lease']) not in (int,float)
                    or ticket['lease_expires_at']<=intent['previous_lease'])):
                raise DevelopmentError('development_renewal_unconfirmed')
            op=self._save(op,'claimed',claimed_context=current['context'],claimed_ticket=ticket)
        if op['stage']=='claimed':
            current=authorization.snapshot(require_claim=True,remaining=10)
            if current['context']['fingerprint']!=op['payload']['claimed_context']['fingerprint']:
                raise DevelopmentError('development_context_changed')
            # The context is captured AFTER our known transition, then frozen.
            claim_id=authorization.seal(current)
            available=int(current['ticket']['lease_expires_at']-self.clock()-10)
            if available<1:
                raise DevelopmentError('development_claim_too_short')
            limits=PlannerBudgets(**op['configuration']['budgets'])
            bounded=replace(limits,max_wall_seconds=min(limits.max_wall_seconds,available)).document()
            op=self._save(op,'prepared',context=current['context'],claim_id=claim_id,
                claim_version=current['ticket']['version'],planner_budgets=bounded)
        return op

    def _existing_fence_check(self, op):
        anchor=canonical_anchor() if self.test_anchor is None else Path(self.test_anchor).absolute()
        if not anchor.exists():
            return
        pins=op['configuration']['pins']
        with closing(DevelopmentJournal(pins['broker']['path'])) as manifest:
            policy=self.policy_factory(pins['planner_policy']['path'])
            try:
                policy.validate_for(policy.executable,policy.workspace)
                parent=self._parent_binding(op,manifest,policy)
                ancestors=_ancestor_identities(anchor)
                for path in (manifest.manifest['root'],policy.workspace):
                    selected=_ancestor_identities(path)
                    if selected[0] in ancestors or ancestors[0] in selected:
                        raise DevelopmentError('development_fence_overlap')
                fence=DevelopmentFence(_test_anchor=self.test_anchor)
                try:found=fence.inspect_checkout(manifest.manifest['root_identity'])
                finally:fence.close()
                if found is not None and (found['operation_id']!=op['id'] or any(
                        found['binding'].get(key)!=parent[key] for key in
                        ('scope','root_identity','journal','child_scope','parent_scope'))):
                    raise DevelopmentError('canonical_parent_conflict')
            finally:policy.close()

    def run(self, operation_id, *, reconcile=False):
        op=self.journal.get(operation_id)
        with self.journal.lock(op['scope']):
            op=self.journal.get(operation_id)
            self.client.adopt_registration()
            connection=getattr(self.client,'connection_path',None)
            if connection is not None:
                config=op['configuration'];broker_pin=config['pins']['broker']
                with closing(DevelopmentJournal(broker_pin['path'])) as manifest:
                    connection_ancestors=_ancestor_identities(Path(connection).parent)
                    if tuple(manifest.manifest['root_identity']) in connection_ancestors:
                        raise DevelopmentError('development_connection_overlap')
            if op['stage'] in ('result_ready','complete','abandoned'):
                return self._public(op)
            if op['stage']=='needs_review' and not reconcile:
                return self._public(op)
            config=op['configuration'];pins=config['pins']
            try:
                self._existing_fence_check(op)
                if self._stopped(op['id']):
                    raise DevelopmentError('development_stop_requested')
                with closing(DevelopmentJournal(pins['broker']['path'])) as manifest:
                    authorization=DevelopmentAuthorization(self.client,scope=op['scope'],manifest=manifest.manifest,
                        registration=op['payload']['registration'],clock=self.clock,stop_requested=lambda:self._stopped(op['id']))
                if 'context' in op['payload']:
                    authorization.expected_context=deepcopy(op['payload']['context'])
                    authorization.claim_version=op['payload']['claim_version']
                    authorization.claim_id=op['payload']['claim_id']
                # Qualification is an offline check. Before claim, the callback
                # verifies explicit grants without asserting a nonexistent lease.
                def allowed(scope,action,resource):
                    try:
                        if 'context' in op['payload']:
                            grant=authorization()
                            return (scope==grant['scope'] and action in grant['actions'] and resource in grant['resources'])
                        policy=authorization._policy()
                        return (scope==authorization.manifest['scope'] and action in policy['actions']
                            and resource==authorization.manifest['resource'] and resource in policy['resources'])
                    except Exception:
                        return False
                with self._resources(pins['broker']['path'],pins['backend']['path'],pins['planner_policy']['path'],
                                     authorize=allowed) as resources:
                    manifest,policy,backend=resources
                    self._pin_configuration(op,manifest,policy,backend)
                    authorization.snapshot(require_claim='context' in op['payload'])
                    parent_binding=self._parent_binding(op,manifest,policy)
                    anchor=canonical_anchor() if self.test_anchor is None else Path(self.test_anchor).absolute()
                    ancestors=_ancestor_identities(anchor,allow_missing=True)
                    for path in (manifest.manifest['root'],policy.workspace):
                        selected=_ancestor_identities(path)
                        if selected[0] in ancestors or (anchor.exists() and ancestors[0] in selected):
                            raise DevelopmentError('development_fence_overlap')
                    fence=DevelopmentFence(_test_anchor=self.test_anchor)
                    try:
                        # This precedes the first claim/renew MCP effect and
                        # fences alternate journals even before a planner exists.
                        existing_parent=op['payload'].get('parent_binding')
                        if existing_parent is None:
                            fence.bind(parent_binding,op['id'])
                            op=self._save(op,parent_binding=parent_binding)
                        elif existing_parent!=parent_binding:
                            raise DevelopmentError('development_parent_changed')
                        elif 'context' not in op['payload']:
                            fence.verify(parent_binding,op['id'])
                    finally:
                        fence.close()
                    if op['stage'] in ('new','claiming','claimed'):
                        op=self._claim(op,authorization)
                    if op['stage']=='prepared':
                        authorization.expected_context=deepcopy(op['payload']['context'])
                        authorization.claim_version=op['payload']['claim_version']
                        authorization.claim_id=op['payload']['claim_id']
                        authorization()
                        op=self._save(op,'running')
                    if op['stage'] not in ('running','needs_review'):
                        raise DevelopmentError('development_stage_requires_review')
                    broker=DevelopmentBroker(pins['broker']['path'],authorize_scope=allowed,enabled=True,test_backend=backend)
                    try:
                        planner=self.planner_factory(broker=broker,store=self.store,runtime_journal=self.runtime,
                            control_client=self.control,board_scope=op['scope'],planner_policy=policy,
                            authorization=authorization,claim_id=op['payload']['claim_id'],
                            instruction=config['instruction'],model=config['model'],
                            budgets=PlannerBudgets(**op['payload']['planner_budgets']),enabled=True,
                            context=op['payload']['context'],
                            parent_operation={'binding':parent_binding,'operation_id':op['id']},
                            _transport=self.transport,_test_anchor=self.test_anchor,
                            _allow_fixture_backend=self.allow_fixture,_clock=self.clock)
                        try:
                            receipt=planner.reconcile() if reconcile else planner.run()
                        finally:
                            planner.close()
                    finally:
                        broker.close()
                    if receipt['status']=='review_ready':
                        if op['stage']=='needs_review':
                            # WorkJournal intentionally has no automatic escape
                            # from review. Keep the recovered evidence for a human.
                            op=self._save(op,planner_receipt=receipt,reason='development_recovered_review')
                        else:
                            op=self._save(op,'result_ready',planner_receipt=receipt)
                    elif receipt['status']=='needs_review':
                        # Keep the parent running fence while exact local
                        # receipt reconciliation remains possible. No new
                        # inference occurs unless explicitly resumed later.
                        op=self._save(op,planner_receipt=receipt,reason=receipt['reason'])
                    else:
                        op=self._save(op,planner_receipt=receipt)
                    return self._public(op,planner=receipt)
            except Exception as error:
                reason=error.code if isinstance(error,DevelopmentError) else 'development_control_requires_review'
                op=self.journal.get(operation_id)
                if op['stage'] not in ('needs_review','complete','abandoned'):
                    op=self._save(op,'needs_review',reason=reason)
                return self._public(op,reason)

    def _stopped(self, operation_id):
        from .dev_journal import PrivateDirectory, decode
        name='development-stop-'+operation_id+'.json'
        with PrivateDirectory(self.journal.directory) as private:
            try:raw=private.read(name,512)
            except DevelopmentError as error:
                if error.code!='private_read_failed':raise
                try:os.stat(name,dir_fd=private.fd,follow_symlinks=False)
                except FileNotFoundError:return False
                raise
            if decode(raw)!={'schema':1,'operation_id':operation_id,'stop':True}:
                raise DevelopmentError('development_stop_corrupt')
            return True

    def stop(self, operation_id):
        # Independent immutable marker: another process can stop while run
        # holds its WorkJournal lock and a model/test is still active.
        op=self.journal.get(operation_id)
        from .dev_journal import PrivateDirectory, decode
        from .dev_planner_journal import PlannerJournal
        with PrivateDirectory(self.journal.directory) as private:
            if not self._stopped(operation_id):
                try:private.create('development-stop-'+operation_id+'.json',canonical(
                    {'schema':1,'operation_id':operation_id,'stop':True}))
                except DevelopmentError as error:
                    if error.code!='private_file_exists' or not self._stopped(operation_id):raise
        directory=op['configuration']['pins']['broker']['path']
        with PrivateDirectory(directory) as private:
            try:state=decode(private.read('planner.json',2*1024*1024))
            except DevelopmentError as error:
                if error.code!='private_read_failed':raise
                state=None
        if state is not None:
            if state.get('operation_id')!=op['id']:
                raise DevelopmentError('development_planner_scope_mismatch')
            planner=PlannerJournal(directory,state['configuration'],op['id'])
            try:planner.request_stop()
            finally:planner.close()
        return {**self._public(op,reason='development_stop_requested'),'stop_requested':True}

    @staticmethod
    def _audit_text(op):
        review=op['payload']['planner_receipt']['review']
        return ('Development snapshot '+op['id']+'\n'
                'Inspection timestamp: '+str(review['inspected_at'])+'\n'
                'Base: '+review['diff']['base_sha']+'\n'
                'Source snapshot SHA256: '+review['snapshot_sha256']+'\n'
                'Passed named suites: '+', '.join(sorted(review['tests']))+'\n'
                'Dated audit only; verify current hashes before accepting or applying changes.')

    def publish(self, operation_id):
        """Explicit publication of dated evidence in review, never done."""
        op=self.journal.get(operation_id)
        with self.journal.lock(op['scope']):
            op=self.journal.get(operation_id)
            if op['stage']=='complete':
                return self._public(op)
            if op['stage'] not in ('result_ready','publishing'):
                raise DevelopmentError('development_not_ready_for_publication')
            self.client.adopt_registration()
            try:
                if op['stage']=='result_ready':
                    from .dev_journal import DevelopmentJournal
                    with closing(DevelopmentJournal(op['configuration']['pins']['broker']['path'])) as manifest:
                        authorization=DevelopmentAuthorization(self.client,scope=op['scope'],manifest=manifest.manifest,
                            registration=op['payload']['registration'],clock=self.clock,stop_requested=lambda:self._stopped(op['id']))
                    authorization.expected_context=deepcopy(op['payload']['context'])
                    authorization.claim_version=op['payload']['claim_version']
                    authorization.claim_id=op['payload']['claim_id']
                    authorization()
                    ticket=authorization.snapshot(require_claim=True)['ticket']
                    text=self._audit_text(op)
                    evidence=ticket['evidence']+('\n\n' if ticket['evidence'] else '')+text
                    changes={'status':'review','outcome':ticket['outcome'],'evidence':evidence,'release_claim':True}
                    op=self._save(op,'publishing',publication={'expected_version':ticket['version'],'changes':changes})
                intent=op['payload']['publication']
                response=self.client.call('get_ticket',{'ticket_id':op['scope']['ticket_id']})
                ticket=response['ticket']
                if ticket['version']==intent['expected_version'] and ticket['claimant'] is not None:
                    # Revalidate full live authority/context even when retrying
                    # an exact optimistic publication intent.
                    with closing(DevelopmentJournal(op['configuration']['pins']['broker']['path'])) as manifest:
                        authorization=DevelopmentAuthorization(self.client,scope=op['scope'],manifest=manifest.manifest,
                            registration=op['payload']['registration'],clock=self.clock,stop_requested=lambda:self._stopped(op['id']))
                    authorization.expected_context=deepcopy(op['payload']['context'])
                    authorization.claim_version=op['payload']['claim_version'];authorization.claim_id=op['payload']['claim_id']
                    authorization()
                    try:
                        self.client.call('update_ticket',{'ticket_id':op['scope']['ticket_id'],
                            'expected_version':intent['expected_version'],'changes':intent['changes']})
                    except Exception:
                        pass
                    response=self.client.call('get_ticket',{'ticket_id':op['scope']['ticket_id']});ticket=response['ticket']
                changes=intent['changes']
                if (response['project']['id']!=op['scope']['project_id'] or ticket['id']!=op['scope']['ticket_id']
                        or ticket['version']!=intent['expected_version']+1 or ticket['claimant'] is not None
                        or any(ticket.get(key)!=changes[key] for key in ('status','outcome','evidence'))):
                    raise DevelopmentError('development_publication_unconfirmed')
                op=self._save(op,'complete',publication_confirmed=True)
                return self._public(op)
            except Exception as error:
                reason=error.code if isinstance(error,DevelopmentError) else 'development_publication_requires_review'
                # Keep exact publishing intent for a receipt-only retry. A
                # mismatched state cannot silently create a new publication.
                op=self._save(op,reason=reason)
                return self._public(op,reason)

    def status(self, operation_id):
        return {**self._public(self.journal.get(operation_id)),'stop_requested':self._stopped(operation_id)}
