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
import uuid

from .context import ContextScope, _hash
from .development_authorization import DevelopmentAuthorization
from .dev_broker import DevelopmentBroker
from .dev_fence import DevelopmentFence, canonical_anchor
from .dev_journal import DevelopmentJournal
from .dev_manifest import DevelopmentError, canonical, check_content, digest, opaque, sha256
from .dev_planner import DevelopmentPlanner, _agent_id, _directory_pin, _reasoning_configuration
from .dev_runtime import development_work_id
from .dev_planner_journal import PlannerBudgets
from .dev_test_supervisor import SupervisedNativeNamedPythonBackend, _ancestor_identities
from .planner_policy import PlannerPolicy
from .work_journal import JournalError


def execution_factories(profile='darwin'):
    """Closed explicit selection, never inferred from platform or a model."""
    if profile == 'darwin':
        return {'backend_factory': SupervisedNativeNamedPythonBackend, 'policy_factory': PlannerPolicy}
    if profile == 'linux-python-execution-v1':
        from .linux_test_backend import SupervisedLinuxNamedPythonBackend
        from .linux_planner_policy import LinuxPlannerPolicy
        return {'backend_factory': SupervisedLinuxNamedPythonBackend, 'policy_factory': LinuxPlannerPolicy}
    raise DevelopmentError('development_execution_profile_invalid')


class DevelopmentBoardWorker:
    def __init__(self, journal, store, runtime_journal, client, control_client, *,
                 backend_factory=SupervisedNativeNamedPythonBackend, policy_factory=PlannerPolicy,
                 planner_factory=DevelopmentPlanner, _transport=None, _test_anchor=None,
                 _allow_fixture_backend=False, clock=time.time, controller_scope=None, controller_guard=None, delegation_guard=None, fence_intent=None,
                 controller_local_guard=None):
        self.journal, self.store, self.runtime = journal, store, runtime_journal
        self.client, self.control = client, control_client
        self.backend_factory, self.policy_factory, self.planner_factory = backend_factory, policy_factory, planner_factory
        self.transport, self.test_anchor, self.allow_fixture, self.clock = _transport, _test_anchor, _allow_fixture_backend, clock
        if controller_scope is not None:
            if (type(controller_scope) is not dict or set(controller_scope) !=
                    {'kind','operation_id','family_id','slot','plan_sha256','configuration_sha256','journal'}
                    or controller_scope['kind'] != 'delegation_family'
                    or controller_scope['slot'] not in ('child_0','child_1','parent')
                    or not callable(controller_guard) or not callable(controller_local_guard)
                    or not callable(delegation_guard) or not callable(fence_intent)):
                raise DevelopmentError('invalid_development_controller')
            development_work_id(controller_scope['operation_id'])
            opaque(controller_scope['family_id']);sha256(controller_scope['plan_sha256'])
            sha256(controller_scope['configuration_sha256'])
        elif (controller_guard is not None or controller_local_guard is not None
                or delegation_guard is not None or fence_intent is not None):
            raise DevelopmentError('invalid_development_controller')
        self.controller_scope = deepcopy(controller_scope)
        self.controller_guard, self.delegation_guard, self.fence_intent = controller_guard, delegation_guard, fence_intent
        self.controller_local_guard = controller_local_guard

    def _local_control(self, operation_id, *, inspection=False):
        """Local operation/family stop checks, without the remote controller guard."""
        if getattr(self,'_checkpoint_execution',None) is not None:
            from .development_cycle_driver import check_worker_cycle
            check_worker_cycle(self,operation_id)
        if not inspection and self._stopped(operation_id):raise DevelopmentError('development_stop_requested')
        if self.controller_scope is not None and self.controller_local_guard(inspection=inspection) is not True:
            raise DevelopmentError('development_controller_stopped')
        return True

    def _controller_cancelled(self, operation_id=None, *, inspection=False):
        if getattr(self,'_checkpoint_execution',None) is not None:
            try:
                from .development_cycle_driver import check_worker_cycle
                check_worker_cycle(self,operation_id)
            except Exception:return True
        if not inspection and operation_id is not None and self._stopped(operation_id):
            return True
        if self.controller_scope is not None:
            try:
                return self.controller_guard() is not True
            except Exception:
                return True
        return False

    def _local_cancelled(self, operation_id=None, *, inspection=False):
        """Local registration bracket inside a fresh concrete family frame.

        Full controller guards at other boundaries remain in
        _controller_cancelled; Runtime cancellation remains in the planner.
        """
        try:
            if operation_id is not None:
                return self._local_control(operation_id,inspection=inspection) is not True
            if self.controller_scope is not None:
                return self.controller_local_guard(inspection=inspection) is not True
            return False
        except Exception:
            return True

    def _pin_controller(self, op):
        if (self.controller_scope is not None and op['id']!=self._controller_operation_id()):
            raise DevelopmentError('development_controller_required')
        if op['configuration'].get('controller_scope') != self.controller_scope:
            raise DevelopmentError('development_controller_required')

    def _controller_operation_id(self):
        return str(uuid.uuid5(uuid.UUID(self.controller_scope['operation_id']),self.controller_scope['slot']))

    def _family_reservation(self, manifest, scope):
        anchor=canonical_anchor() if self.test_anchor is None else Path(self.test_anchor).absolute()
        try:anchor.lstat()
        except FileNotFoundError:
            if self.controller_scope is not None:raise DevelopmentError('development_family_reservation_missing')
            return
        fence=DevelopmentFence(_test_anchor=anchor)
        try:
            found=fence.inspect_checkout(manifest['root_identity'])
            family=None if found is None else found.get('family')
            if family is None:
                if self.controller_scope is not None:raise DevelopmentError('development_family_reservation_missing')
                return
            descriptor=self.controller_scope
            if descriptor is None:raise DevelopmentError('development_family_reservation_conflict')
            from .dev_journal import PrivateDirectory,decode
            with PrivateDirectory(descriptor['journal']['path']) as private:
                if _directory_pin(private.path)!=descriptor['journal']:
                    raise DevelopmentError('development_family_reservation_conflict')
                state=decode(private.read('family.json',2*1024*1024))
            expected=state['payload']['fence_descriptor']
            if (state['operation_id']!=descriptor['operation_id']
                    or state['configuration_sha256']!=descriptor['configuration_sha256']
                    or digest(canonical(state['configuration']))!=descriptor['configuration_sha256']
                    or state['configuration']['server_plan_sha256']!=descriptor['plan_sha256']
                    or digest(canonical(state['configuration']['server_plan']))!=descriptor['plan_sha256']
                    or state['payload']['family_id']!=descriptor['family_id']
                    or state['payload']['slot_scopes'][descriptor['slot']]!=scope
                    or family['descriptor']!=expected or family['slot']!=descriptor['slot']
                    or expected['roots'][descriptor['slot']]['identity']!=manifest['root_identity']
                    or state['workflow_ids'][descriptor['slot']]!=self._controller_operation_id()
                    or state['configuration']['child_stores'][descriptor['slot']]!={
                        'runs':_directory_pin(self.store.state_dir),'runtime':_directory_pin(self.runtime.directory)}):
                raise DevelopmentError('development_family_reservation_conflict')
            fence.verify_family(state['payload']['fence_reservations'],expected,state['payload'].get('fence_targets',{}))
        finally:fence.close()

    def _verify_family_binding(self, binding, stage):
        """The all-slot prefix check alone does not admit a planner effect."""
        self._family_reservation({'root_identity':binding['root_identity']},binding['scope'])
        fence=DevelopmentFence(_test_anchor=self.test_anchor)
        try:
            found=fence.inspect_checkout(binding['root_identity'])
            if (found is None or found['operation_id']!=self._controller_operation_id()
                    or found['binding']!=binding or found.get('family',{}).get('stage')!=stage):
                raise DevelopmentError('development_family_target_incomplete')
        finally:fence.close()

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
            if manifest._linux_execution_profile:
                from .linux_test_backend import SupervisedLinuxNamedPythonBackend
                from .linux_planner_policy import LinuxPlannerPolicy
                if (type(backend) is not SupervisedLinuxNamedPythonBackend or type(policy) is not LinuxPlannerPolicy
                        or backend.environment != policy.environment):
                    raise DevelopmentError('development_linux_pair_mismatch')
            elif getattr(backend, 'kind', None) == 'linux-python-execution-v1':
                raise DevelopmentError('development_linux_journal_required')
            elif not self.allow_fixture and (type(backend) is not SupervisedNativeNamedPythonBackend
                    or type(policy) is not PlannerPolicy):
                raise DevelopmentError('development_backend_policy_mismatch')
            if (not manifest.manifest['tests'] or any(not backend.qualified_for(manifest.manifest_hash,suite)
                    for suite in manifest.manifest['tests'])):
                raise DevelopmentError('development_backend_unqualified')
            yield manifest, policy, backend

    @staticmethod
    def _backend_pin(backend):
        value={'backend_id':backend.backend_id, 'plan_hash':backend.document['plan_hash'],
               'supervision':backend.supervision}
        if getattr(backend,'kind',None)=='linux-python-execution-v1':
            value['kind']='linux-python-execution-v1'
        return digest(canonical(value))

    def prepare(self, *, scope, request_id, instruction, broker_directory, backend_directory,
                planner_policy_directory, model, budgets=None, operation_id=None, registration=None,
                reasoning_effort=None):
        """Pure read/plan phase; no claim, Runtime reservation, model or test."""
        reasoning = _reasoning_configuration(reasoning_effort)
        if (type(instruction) is not str or not instruction.strip()
                or len(instruction.encode('utf-8','surrogatepass'))>16384):
            raise DevelopmentError('invalid_planner_instruction')
        check_content(instruction.encode('utf-8','strict'))
        opaque(model, 'invalid_development_model', 128)
        limits=(budgets or PlannerBudgets()).document()
        if self.controller_scope is not None and operation_id!=self._controller_operation_id():
            raise DevelopmentError('development_controller_required')
        with self.journal.lock(scope):
            prior=self.journal.find(scope,request_id)
            with closing(DevelopmentJournal(broker_directory)) as reserved_manifest:
                self._family_reservation(reserved_manifest.manifest,scope)
            if registration is not None and self.controller_scope is None:
                raise DevelopmentError('invalid_development_controller')
            if prior is not None:
                self.client.adopt_registration()
                if registration is not None and registration!=prior['payload']['registration']:
                    raise DevelopmentError('development_registration_mismatch')
                registration=prior['payload']['registration']
            elif registration is not None:
                self.client.adopt_registration()
                registration=deepcopy(registration)
            else:
                registration=self.client.register_initial()
            # Grants must be checked before any claim/Runtime/model or canonical
            # reservation. An empty policy cannot bootstrap its own rights.
            with closing(DevelopmentJournal(broker_directory)) as manifest:
                self._family_reservation(manifest.manifest,scope)
                authorization=DevelopmentAuthorization(self.client,scope=scope,manifest=manifest.manifest,
                                                        registration=registration,clock=self.clock,
                    stop_requested=lambda:self._controller_cancelled(prior['id'] if prior else None),
                    local_stop_requested=lambda:self._local_cancelled(prior['id'] if prior else None),
                    delegation_guard=self.delegation_guard)
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
                config.update(reasoning)
                if manifest._linux_execution_profile:
                    config.update(execution_profile='linux-python-execution-v1',
                                  linux_execution_plan_sha256=backend.document['plan_hash'])
                if self.controller_scope is not None:
                    config['controller_scope']=deepcopy(self.controller_scope)
                if prior is not None and prior['configuration']!=config:
                    raise DevelopmentError('development_prepare_conflict')
                op=self.journal.begin(scope,request_id,config,operation_id=operation_id)
                if prior is not None:
                    expected=op['payload'].get('initial_context')
                    if op['stage']=='new' and (expected is None or expected['fingerprint']!=initial['context']['fingerprint']):
                        raise DevelopmentError('development_context_changed')
                    return self._public(op)
                op=self._save(op,registration=registration,initial_context=initial['context'],
                    initial_selected_ticket=initial['selected_ticket'], initial_selected_project=initial['selected_project'])
                return self._public(op)

    def _pin_configuration(self, op, manifest, policy, backend):
        self._pin_controller(op)
        config=op['configuration']
        if (config.get('schema')!=1 or config.get('kind')!='development'
                or config['manifest_hash']!=manifest.manifest_hash or config['policy_hash']!=policy.fingerprint
                or config['backend_hash']!=self._backend_pin(backend)):
            raise DevelopmentError('development_configuration_changed')
        if manifest._linux_execution_profile:
            if (config.get('execution_profile')!='linux-python-execution-v1'
                    or config.get('linux_execution_plan_sha256')!=backend.document['plan_hash']):
                raise DevelopmentError('development_linux_configuration_changed')
        elif 'execution_profile' in config:
            raise DevelopmentError('development_execution_profile_changed')
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
        work=self.store.create_work(owner_id=op['scope']['owner_id'], agent_id=_agent_id(op['scope'], op['id']),
            project_id=op['scope']['project_id'],ticket_id=op['scope']['ticket_id'],
            workspace=policy.workspace,executable=policy.executable,model=op['configuration']['model'],
            work_id=development_work_id(op['id']))
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
                if self._controller_cancelled(op['id']):
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
                if found is not None and found.get('family') is not None:
                    if self.controller_scope is None:raise DevelopmentError('development_controller_required')
                    return  # _family_reservation already validated every exact private prefix.
                if found is not None and (found['operation_id']!=op['id'] or any(
                        found['binding'].get(key)!=parent[key] for key in
                        ('scope','root_identity','journal','child_scope','parent_scope'))):
                    raise DevelopmentError('canonical_parent_conflict')
            finally:policy.close()

    def _has_execution_receipt(self, op):
        if op['stage'] not in ('running','needs_review'):
            return False
        from .dev_journal import PrivateDirectory, decode
        with PrivateDirectory(op['configuration']['pins']['broker']['path']) as private:
            try:state=decode(private.read('planner.json',2*1024*1024))
            except DevelopmentError as error:
                if error.code!='private_read_failed':raise
                return False
        if type(state) is not dict or state.get('operation_id')!=op['id']:
            raise DevelopmentError('development_planner_scope_mismatch')
        return state.get('step') is not None or state.get('phase')=='review_ready'

    def run(self, operation_id, *, reconcile=False):
        return self._run(operation_id,reconcile=reconcile)

    def run_checkpoint(self, operation_id, *, request, checkpoint_controller, parent_checkpoint_id=None,provider_session=None):
        """Explicit ready-boundary capture, with no partial review promotion."""
        from .development_cycle_driver import run_worker_checkpoint
        return run_worker_checkpoint(self,operation_id,request=request,
            checkpoint_controller=checkpoint_controller,parent_checkpoint_id=parent_checkpoint_id,provider_session=provider_session)

    def _run(self, operation_id, *, reconcile=False, checkpoint=None):
        op=self.journal.get(operation_id)
        with self.journal.lock(op['scope']):
            op=self.journal.get(operation_id)
            if reconcile and op['stage'] not in ('result_ready','complete','abandoned') and not self._has_execution_receipt(op):
                return {**self._public(op),'status':'pending','reason':'development_no_execution_receipt'}
            self._pin_controller(op)
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
                with closing(DevelopmentJournal(pins['broker']['path'])) as reserved_manifest:
                    self._family_reservation(reserved_manifest.manifest,op['scope'])
                self._existing_fence_check(op)
                if not reconcile and self._controller_cancelled(op['id']):
                    raise DevelopmentError('development_stop_requested')
                with closing(DevelopmentJournal(pins['broker']['path'])) as manifest:
                    authorization=DevelopmentAuthorization(self.client,scope=op['scope'],manifest=manifest.manifest,
                        registration=op['payload']['registration'],clock=self.clock,
                        stop_requested=lambda:self._controller_cancelled(op['id'],inspection=reconcile),
                        local_stop_requested=lambda:self._local_cancelled(op['id'],inspection=reconcile),
                        delegation_guard=self.delegation_guard)
                if 'context' in op['payload']:
                    authorization.expected_context=deepcopy(op['payload']['context'])
                    authorization.claim_version=op['payload']['claim_version']
                    authorization.claim_id=op['payload']['claim_id']
                # Qualification is an offline check. Before claim, the callback
                # verifies explicit grants without asserting a nonexistent lease.
                allowed=authorization.allowed
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
                            if self.controller_scope is None:
                                fence.bind(parent_binding,op['id'])
                            else:
                                proof=self.fence_intent('parent',parent_binding)
                                fence.transfer_family_slot(proof)
                            op=self._save(op,parent_binding=parent_binding)
                        elif existing_parent!=parent_binding:
                            raise DevelopmentError('development_parent_changed')
                        elif 'context' not in op['payload']:
                            if self.controller_scope is None:fence.verify(parent_binding,op['id'])
                            else:self._verify_family_binding(parent_binding,'parent')
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
                    if checkpoint is not None:
                        from .development_cycle_driver import validate_worker_checkpoint_caps
                        validate_worker_checkpoint_caps(op,manifest,backend,checkpoint[1])
                    broker=DevelopmentBroker(pins['broker']['path'],authorize_scope=allowed,enabled=True,test_backend=backend)
                    try:
                        planner=self.planner_factory(broker=broker,store=self.store,runtime_journal=self.runtime,
                            control_client=self.control,board_scope=op['scope'],planner_policy=policy,
                            authorization=authorization,claim_id=op['payload']['claim_id'],
                            instruction=config['instruction'],model=config['model'],
                            **_reasoning_configuration(config.get('reasoning_effort')),
                            budgets=PlannerBudgets(**op['payload']['planner_budgets']),enabled=True,
                            context=op['payload']['context'],
                            parent_operation={'binding':parent_binding,'operation_id':op['id']},
                            fence_intent=None if self.fence_intent is None else
                                lambda target:self.fence_intent('planner',target),
                            fence_verify=None if self.fence_intent is None else
                                lambda target:self._verify_family_binding(target,'planner'),
                            local_guard=lambda **options:self._local_control(op['id'],**options),
                            _transport=self.transport,_test_anchor=self.test_anchor,
                            _allow_fixture_backend=self.allow_fixture,_clock=self.clock)
                        try:
                            if checkpoint is None:
                                receipt=planner.reconcile() if reconcile else planner.run()
                            else:
                                from .development_checkpoint_proof import DevelopmentCheckpointReady
                                from .development_provider_continuation import DevelopmentProviderCheckpointReady
                                from .private_development_checkpoint import checkpoint_id_for
                                request,controller,parent_id=checkpoint[:3]
                                options={} if len(checkpoint)==3 else {'provider_session':checkpoint[3]}
                                frame=planner.run(checkpoint_request=request,**options)
                                if type(frame) in (DevelopmentCheckpointReady,DevelopmentProviderCheckpointReady):
                                    # run() has released its Planner lock. The
                                    # concrete proof is still live until these
                                    # normal resources close in the finally.
                                    sealed=controller.capture(checkpoint_id=checkpoint_id_for(op['id']),
                                        proof=frame.proof,parent_checkpoint_id=parent_id)
                                    op=self._save(op,checkpoint_capture={'status':frame.status,'checkpoint':sealed,
                                        'observation':deepcopy(frame.proof.metadata),
                                        'previous_revision':op['revision'],'previous_updated_at':op['updated_at']})
                                    return {**self._public(op),'status':frame.status,
                                        'checkpoint':sealed,'historical':False,'launchable':False}
                                receipt=frame
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
            if type(state) is not dict or state.get('operation_id')!=op['id']:
                raise DevelopmentError('development_planner_scope_mismatch')
            planner=PlannerJournal(directory,state['configuration'],op['id'])
            try:planner.request_stop()
            finally:planner.close()
        return {**self._public(op,reason='development_stop_requested'),'stop_requested':True}

    @staticmethod
    def _audit_text(op, delivery=None):
        review=op['payload']['planner_receipt']['review']
        text=('Development snapshot '+op['id']+'\n'
                'Inspection timestamp: '+str(review['inspected_at'])+'\n'
                'Base: '+review['diff']['base_sha']+'\n'
                'Source snapshot SHA256: '+review['snapshot_sha256']+'\n'
                'Passed named suites: '+', '.join(sorted(review['tests']))+'\n'
                'Dated audit only; verify current hashes before accepting or applying changes.')
        if delivery is not None:
            text+='\nDelivery commit: '+delivery['delivery_sha']+'\nDraft pull request: '+delivery['pull_request']['url']
        return text

    def publish(self, operation_id, *, audit_outcome=False, delivery_publication=None):
        """Explicit publication of dated evidence in review, never done."""
        if type(audit_outcome) is not bool:
            raise DevelopmentError('invalid_development_publication')
        op=self.journal.get(operation_id)
        with self.journal.lock(op['scope']):
            op=self.journal.get(operation_id)
            self._pin_controller(op)
            if delivery_publication is not None:
                from .development_delivery import DevelopmentPublishedDelivery
                if (type(delivery_publication) is not DevelopmentPublishedDelivery
                        or delivery_publication.proof.worker is not self
                        or delivery_publication.proof.operation_id != operation_id):
                    raise DevelopmentError('invalid_development_publication')
            retained=op['payload'].get('publication')
            if retained is not None and (retained.get('delivery') is None) != (delivery_publication is None):
                raise DevelopmentError('development_publication_conflict')
            if op['stage']=='complete':
                if delivery_publication is not None and delivery_publication.verify(self,operation_id,op['scope']) != retained['delivery']:
                    raise DevelopmentError('development_publication_conflict')
                return self._public(op)
            if op['stage'] not in ('result_ready','publishing'):
                raise DevelopmentError('development_not_ready_for_publication')
            self.client.adopt_registration()
            try:
                delivery=None if delivery_publication is None else delivery_publication.verify(self,operation_id,op['scope'])
                if op['stage']=='result_ready':
                    with closing(DevelopmentJournal(op['configuration']['pins']['broker']['path'])) as manifest:
                        authorization=DevelopmentAuthorization(self.client,scope=op['scope'],manifest=manifest.manifest,
                            registration=op['payload']['registration'],clock=self.clock,
                            stop_requested=lambda:self._controller_cancelled(op['id']),
                            local_stop_requested=lambda:self._local_cancelled(op['id']),delegation_guard=self.delegation_guard)
                    authorization.expected_context=deepcopy(op['payload']['context'])
                    authorization.claim_version=op['payload']['claim_version']
                    authorization.claim_id=op['payload']['claim_id']
                    authorization()
                    ticket=authorization.snapshot(require_claim=True)['ticket']
                    text=self._audit_text(op,delivery)
                    evidence=ticket['evidence']+('\n\n' if ticket['evidence'] else '')+text
                    outcome=('Development audit ready for review; source snapshot SHA256: '+
                        op['payload']['planner_receipt']['review']['snapshot_sha256']) if audit_outcome else ticket['outcome']
                    changes={'status':'review','outcome':outcome,'evidence':evidence,'release_claim':True}
                    op=self._save(op,'publishing',publication={'expected_version':ticket['version'],'changes':changes,
                        'audit_outcome':audit_outcome,'delivery':delivery})
                intent=op['payload']['publication']
                if intent.get('audit_outcome',False) is not audit_outcome or intent.get('delivery') != delivery:
                    raise DevelopmentError('development_publication_conflict')
                response=self.client.call('get_ticket',{'ticket_id':op['scope']['ticket_id']})
                ticket=response['ticket']
                if ticket['version']==intent['expected_version'] and ticket['claimant'] is not None:
                    if delivery_publication is not None and delivery_publication.verify(self,operation_id,op['scope']) != intent['delivery']:
                        raise DevelopmentError('development_publication_conflict')
                    # Revalidate full live authority/context even when retrying
                    # an exact optimistic publication intent.
                    with closing(DevelopmentJournal(op['configuration']['pins']['broker']['path'])) as manifest:
                        authorization=DevelopmentAuthorization(self.client,scope=op['scope'],manifest=manifest.manifest,
                            registration=op['payload']['registration'],clock=self.clock,
                            stop_requested=lambda:self._controller_cancelled(op['id']),
                            local_stop_requested=lambda:self._local_cancelled(op['id']),delegation_guard=self.delegation_guard)
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

    def reconcile_cleanup(self, operation_id):
        from .development_cleanup import reconcile_cleanup
        return reconcile_cleanup(self,operation_id)

    def status(self, operation_id):
        return {**self._public(self.journal.get(operation_id)),'stop_requested':self._stopped(operation_id)}
