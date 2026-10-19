"""Explicit local two-child development family, with simulated transports in tests.

The controller audits scoped sources directly; these bounded read-only inspections
are not model tools or Runtime executions. All actual model/broker effects belong
to the existing DevelopmentBoardWorker. There is no queue, qualification, account
fallback, repository publication, merge or automatic parent completion here.
"""
from contextlib import closing,contextmanager
from copy import deepcopy
import math
import time

from .development_worker import DevelopmentBoardWorker
from .development_authorization import DevelopmentAuthorization
from .dev_manifest import DevelopmentError,canonical,digest,opaque
from .dev_journal import decode
from .dev_planner import _directory_pin
from .dev_planner_journal import PlannerBudgets
from .family_audit import audit_workflow,manifest_document,MAX_AUDIT_BYTES,MAX_CANDIDATE_BYTES
from .family_journal import SLOTS
from .family_plan import build_configuration,root_sources,validate_root_start,validate_root_reads
from .context import ContextScope,read_context_pages,build_context_from_reads
from .development_frame import LEGACY, FRAME, parse_frame, read_protocol
from .family_sources import controller_sources


class FamilyAuthorizationGuard:
    """One concrete family controller, not an arbitrary permission callback.

    A snapshot groups the existing parent/child observations into a single
    bounded read window. It is discarded after validation; there is no grant
    cache, server transaction, registration refresh or effect in this class.
    """
    def __init__(self, family, state, slot, *, terminal=False):
        self.family, self.state, self.slot, self.terminal = family, state, slot, terminal

    def _matches(self, response):
        metadata = response['ticket'].get('delegation')
        expected = self.state['payload']['slot_scopes'][self.slot]
        return (type(metadata) is dict and metadata.get('family_id') == self.state['payload']['family_id']
                and metadata.get('parent_ticket_id') == self.state['configuration']['scope']['ticket_id']
                and metadata.get('owner_id') == expected['owner_id']
                and metadata.get('agent_id') == expected['agent_id']
                and response['ticket']['id'] == expected['ticket_id'])

    def __call__(self, response):
        # Opaque callers still receive a full fresh family check.
        self.family._live(self.state, terminal=self.terminal)
        return self._matches(response)

    def snapshot(self, authority, *, require_claim, remaining):
        family, state, slot = self.family, self.state, self.slot
        config = state['configuration']
        if (authority.client is not family.workflows[slot].client
                or authority.scope != state['payload']['slot_scopes'][slot]
                or authority.registration != config['registrations'][slot]
                or digest(canonical(authority.manifest)) != state['payload']['bindings'][slot]['manifest_sha256']):
            raise DevelopmentError('family_authorization_scope_changed')
        # All local pins/stop/deadline checks bracket the complete remote read
        # window, including slow reads and the pure context composition below.
        family._local_live(state, terminal=self.terminal)
        authority._registration()
        parent = DevelopmentAuthorization(family.client, scope=config['scope'],
            manifest=config['parent_manifest'], registration=config['registrations']['parent'],
            clock=family.clock)
        if read_protocol(config.get('read_protocol',LEGACY)) == FRAME:
            return self._frame_snapshot(authority,parent,require_claim=require_claim,remaining=remaining)
        policy = authority._policy()
        parent_policy = parent._policy()
        first = deepcopy(authority.client.call('get_ticket', {'ticket_id': authority.scope['ticket_id']}))
        authority._ticket_fields(first)
        if not self._matches(first):
            raise DevelopmentError('development_ticket_unavailable')
        if require_claim and not authority.own_claim(first['ticket'], remaining=remaining):
            raise DevelopmentError('development_claim_lost')
        root_first = deepcopy(family.client.call('get_ticket', {'ticket_id': config['scope']['ticket_id']}))
        children = [scope['ticket_id'] for name, scope in state['payload']['slot_scopes'].items() if name != 'parent']
        validate_root_start(parent, root_first, config['scope'], config['parent_manifest'], children=children)
        family._view(state, terminal=self.terminal)
        selected = ContextScope(**{key: authority.scope[key] for key in
                                  ('owner_id', 'agent_id', 'project_id', 'ticket_id')})
        root_scope = ContextScope(**{key: config['scope'][key] for key in
                                    ('owner_id', 'agent_id', 'project_id', 'ticket_id')})
        pages = read_context_pages(authority.client.call, scope=selected, first_response=first)
        root_pages = read_context_pages(family.client.call, scope=root_scope, first_response=root_first)
        last = deepcopy(authority.client.call('get_ticket', {'ticket_id': authority.scope['ticket_id']}))
        root_last = deepcopy(family.client.call('get_ticket', {'ticket_id': config['scope']['ticket_id']}))
        # The family view recomputes current owner/profile/grants/resource,
        # parent claim and member state server-side through should_stop.
        family._view(state, terminal=self.terminal)
        if self.terminal:
            # review_ready is already a stop reason server-side, so that
            # terminal view does not recompute current action/resource grants.
            # Keep the former second callback's fresh policy checks explicit.
            policy = authority._policy()
            parent_policy = parent._policy()
        authority._ticket_fields(last)
        if not self._matches(last):
            raise DevelopmentError('development_ticket_unavailable')
        context = build_context_from_reads(scope=selected, registration=authority.registration,
            first_response=first, pages=pages, final_response=last)
        root_context = build_context_from_reads(scope=root_scope, registration=config['registrations']['parent'],
            first_response=root_first, pages=root_pages, final_response=root_last)
        validate_root_reads(parent, root_first, root_last, root_context, parent_policy,
            config['scope'], config['parent_manifest'], children=children, baseline=config['root_frame'])
        result = authority._snapshot_result(policy, first, last, context,
            require_claim=require_claim, remaining=remaining)
        authority._registration()
        family._local_live(state, terminal=self.terminal)
        return result

    def _frame_snapshot(self, authority, parent, *, require_claim, remaining):
        family,state=self.family,self.state
        config=state['configuration']
        observed=authority.client.get_development_frame(ticket_id=authority.scope['ticket_id'],
            family_id=state['payload']['family_id'],terminal=self.terminal)
        # A stop/deadline/pin change while the network was blocked is checked
        # before any observation is composed, as well as after composition.
        authority._registration();parent._registration()
        family._local_live(state,terminal=self.terminal)
        frame=parse_frame(observed,scope=authority.scope,manifest=authority.manifest,
            parent_scope=config['scope'],parent_manifest=config['parent_manifest'],
            family_id=state['payload']['family_id'],terminal=self.terminal)
        child,root=frame['selected'],frame['parent']
        authority._policy_response(child['policy_first'])
        parent._policy_response(root['policy_first'])
        first,last=child['first'],child['last']
        root_first,root_last=root['first'],root['last']
        authority._ticket_fields(first)
        if not self._matches(first):raise DevelopmentError('development_ticket_unavailable')
        if require_claim and not authority.own_claim(first['ticket'],remaining=remaining):
            raise DevelopmentError('development_claim_lost')
        children=[scope['ticket_id'] for name,scope in state['payload']['slot_scopes'].items() if name!='parent']
        validate_root_start(parent,root_first,config['scope'],config['parent_manifest'],children=children)
        family._validate_view(state,frame['family']['first'],terminal=self.terminal)
        selected=ContextScope(**{key:authority.scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')})
        root_scope=ContextScope(**{key:config['scope'][key] for key in ('owner_id','agent_id','project_id','ticket_id')})
        context=build_context_from_reads(scope=selected,registration=authority.registration,
            first_response=first,pages=child['pages'],final_response=last)
        root_context=build_context_from_reads(scope=root_scope,registration=config['registrations']['parent'],
            first_response=root_first,pages=root['pages'],final_response=root_last)
        family._validate_view(state,frame['family']['last'],terminal=self.terminal)
        policy=authority._policy_response(child['policy_last'])
        parent_policy=parent._policy_response(root['policy_last'])
        authority._ticket_fields(last)
        if not self._matches(last):raise DevelopmentError('development_ticket_unavailable')
        validate_root_reads(parent,root_first,root_last,root_context,parent_policy,
            config['scope'],config['parent_manifest'],children=children,baseline=config['root_frame'])
        result=authority._snapshot_result(policy,first,last,context,require_claim=require_claim,remaining=remaining)
        authority._registration();parent._registration()
        family._local_live(state,terminal=self.terminal)
        return result


class DevelopmentFamilyWorker:
    def __init__(self,journal,workflows,*,registrations=None,clock=time.time):
        if type(workflows) is not dict or set(workflows)!=set(SLOTS):
            raise DevelopmentError('invalid_family_workflows')
        if any(not isinstance(worker,DevelopmentBoardWorker) or worker.controller_scope is not None
               for worker in workflows.values()):raise DevelopmentError('invalid_family_workflows')
        self.journal,self.workflows,self.registrations,self.clock=journal,workflows,registrations,clock
        self.client=workflows['parent'].client
        self._last_local_time=None

    @staticmethod
    def _public(state):
        return {'operation_id':state['operation_id'],'family_id':state['payload'].get('family_id'),
                'status':state['phase'],'reason':state['payload'].get('reason',''),
                'children_accepted':len(state['payload'].get('accepted',{})),
                'snapshot_sha256':state['payload'].get('finalize_intent',{}).get('snapshot_sha256')}

    def status(self):return {**self._public(self.journal.read()),'stop_requested':self.journal.stopped()}

    def _save(self,state,phase=None,**values):
        if phase is not None:state['phase']=phase
        state['payload'].update(deepcopy(values));self.journal.save(state)

    def _qualification_manifest(self,config,slot,state=None):
        """Validate an inspection scope without borrowing an execution claim."""
        if slot not in SLOTS or state is None and slot!='parent':
            raise DevelopmentError('family_qualification_scope_invalid')
        if state is not None and (state['configuration']!=config or 'family_id' not in state['payload']):
            raise DevelopmentError('family_qualification_scope_invalid')
        selected=config['scope'] if state is None else state['payload']['slot_scopes'][slot]
        expected={key:selected[key] for key in ('owner_id','agent_id','project_id','ticket_id')}
        expected.update(device_id=config['parent_manifest']['scope']['device_id'],
            session_id=selected['board_session_id'],
            profile_version=config['parent_manifest']['scope']['profile_version'])
        paths=sorted(config['parent_manifest']['files']) if slot=='parent' else config['inputs'][slot]['files']
        manifest,hash_value=manifest_document(config['inputs'][slot]['broker_directory'])
        if (manifest['scope']!=expected or manifest['root']!=config['roots'][slot]['path']
                or manifest['root_identity']!=config['roots'][slot]['identity']
                or manifest['base_sha']!=config['parent_manifest']['base_sha']
                or manifest['resource']!=config['parent_manifest']['resource']
                or 'repo.run_test' not in manifest['actions']
                or not set(manifest['actions']).issubset(config['parent_manifest']['actions'])
                or sorted(manifest['files'])!=paths
                or any(manifest['files'][path]!=config['parent_manifest']['files'][path] for path in paths)):
            raise DevelopmentError('family_manifest_caps_changed')
        if slot=='parent' and hash_value!=config['parent_manifest_sha256']:
            raise DevelopmentError('family_manifest_changed')
        if state is not None and slot in state['payload'].get('bindings',{}):
            if hash_value!=state['payload']['bindings'][slot]['manifest_sha256']:
                raise DevelopmentError('family_manifest_changed')
        return manifest,hash_value,selected

    def _qualification_authorizer(self,config,slot,state=None):
        """Read qualified pins under fresh authority; this grants no execution.

        An unclaimed child is inspected through its exact registered session
        and the coordinator's live root claim. Its test/model effects still
        require the child worker's own claim and Runtime admission.
        """
        manifest,pinned_hash,selected=self._qualification_manifest(config,slot,state)
        def allowed(given,action,resource):
            if (type(given) is not dict or set(given)!=set(manifest['scope'])
                    or any(type(given[key]) is not type(value) or given[key]!=value
                           for key,value in manifest['scope'].items())
                    or type(action) is not str or action!='repo.run_test'
                    or type(resource) is not str or resource!=manifest['resource']):return False
            try:
                current,current_hash,_=self._qualification_manifest(config,slot,state)
                if current_hash!=pinned_hash:return False
                if state is None:
                    if self.journal.stopped():return False
                    now=self.clock()
                    if (type(now) not in (int,float) or not math.isfinite(now)
                            or now>=config['opening']['limits']['deadline']):return False
                    if controller_sources(config['roots'])!=config['controller_sources']:return False
                    root_sources(self.client,config['scope'],config['parent_manifest'],
                        config['registrations']['parent'],baseline=config['root_frame'],clock=self.clock)
                else:self._live(state)
                for pin in config['roots'].values():
                    if _directory_pin(pin['path'])!=pin:return False
                client=self.workflows[slot].client;client.adopt_registration()
                authority=DevelopmentAuthorization(client,scope=selected,manifest=current,
                    registration=config['registrations'][slot],clock=self.clock)
                authority._registration();policy=authority._policy()
                return action in policy['actions'] and resource in policy['resources']
            except Exception:
                return False
        return allowed

    @contextmanager
    def _qualification_resources(self,config,slot,state=None):
        """Permit qualification checks only while entering the resource set.

        Backend callbacks are revoked before the caller receives its pinned
        metadata, also when construction or qualification raises. Reusing the
        returned backend cannot turn this inspection into a test execution.
        """
        allowed=self._qualification_authorizer(config,slot,state)
        worker=self.workflows[slot];item=config['inputs'][slot];active=True
        def inspection(given,action,resource):
            return active and allowed(given,action,resource)
        try:
            with worker._resources(item['broker_directory'],item['backend_directory'],item['planner_policy_directory'],
                                   authorize=inspection) as resources:
                active=False
                yield resources
        finally:
            active=False

    def prepare(self,request):
        try:existing=self.journal.read()
        except DevelopmentError as error:
            if error.code!='private_read_failed':raise
            existing=None
        if existing is not None:
            if existing['configuration']['request']!=request:
                raise DevelopmentError('family_configuration_conflict')
            self.journal.verify(existing)
            return self._public(existing)
        if self.registrations is None:raise DevelopmentError('family_registrations_required')
        config=build_configuration(request,self.workflows,self.registrations,clock=self.clock)
        # Parent qualification is supplied, never generated by preparation.
        item=config['inputs']['parent'];root=self.workflows['parent']
        with self._qualification_resources(config,'parent') as resources:
            manifest,policy,backend=resources
            if manifest.manifest_hash!=config['parent_manifest_sha256']:
                raise DevelopmentError('family_manifest_changed')
            config['parent_qualification']={'policy':policy.fingerprint,'backend':root._backend_pin(backend)}
        state=self.journal.prepare(config)
        return self._public(state)

    def _root(self,state):
        config=state['configuration'];self.client.adopt_registration()
        return root_sources(self.client,config['scope'],config['parent_manifest'],config['registrations']['parent'],
            children=[s['ticket_id'] for slot,s in state['payload'].get('slot_scopes',{}).items() if slot!='parent'],
            baseline=config['root_frame'],clock=self.clock)

    def _view(self,state,*,terminal=False):
        value=self.client.call('get_delegation_family',{'family_id':state['payload']['family_id']})
        return self._validate_view(state,value,terminal=terminal)

    def _validate_view(self,state,value,*,terminal=False):
        """Same validation for a live method response or a fresh fixed frame."""
        payload,config=state['payload'],state['configuration']
        if (type(value) is not dict or value.get('id')!=payload['family_id']
                or any(value.get(key)!=config['scope'][key] for key in ('owner_id','agent_id','project_id'))
                or value.get('parent_ticket_id')!=config['scope']['ticket_id']
                or value.get('plan')!=config['server_plan'] or value.get('plan_sha256')!=config['server_plan_sha256']
                or digest(canonical(value['plan']))!=value['plan_sha256']):
            raise DevelopmentError('family_server_plan_changed')
        if (type(value.get('revision')) is not int or value['revision']<1
                or value.get('state') not in ('running','integrating','review_ready')
                or type(value.get('children')) is not list or len(value['children'])!=2):
            raise DevelopmentError('family_server_plan_changed')
        by_role={child['role']:child for child in value['children']}
        if len(by_role)!=2 or set(by_role)!={c['role'] for c in config['server_plan']['children']}:
            raise DevelopmentError('family_child_scope_changed')
        for slot in SLOTS[:2]:
            planned=config['inputs'][slot];member=by_role[planned['role']]
            if (member['model']!=planned['model'] or member['ticket_id']!=payload['slot_scopes'][slot]['ticket_id']):
                raise DevelopmentError('family_child_scope_changed')
            if slot in payload.get('accepted',{}):
                intent=payload['accept_intents'][slot]
                if (member['state']!='accepted' or member['result_sha256']!=payload['accepted'][slot]
                        or member['accepted_version']!=intent['expected_child_version']+1):
                    raise DevelopmentError('family_accept_unconfirmed')
        if value.get('should_stop') is not False:
            if not (terminal and value.get('state')=='review_ready'
                    and value.get('final_snapshot_sha256')==payload.get('finalize_intent',{}).get('snapshot_sha256')):
                raise DevelopmentError('family_stopped')
        return value

    def _local_live(self,state,*,terminal=False,inspection=False):
        """Durable identity/configuration/stop checks; no Board request."""
        self.journal.verify(state)
        protocol=read_protocol(state['configuration'].get('read_protocol',LEGACY))
        if protocol!=read_protocol(state['configuration']['request'].get('read_protocol',LEGACY)):
            raise DevelopmentError('family_read_protocol_changed')
        if controller_sources(state['configuration']['roots'])!=state['configuration']['controller_sources']:
            raise DevelopmentError('family_controller_sources_changed')
        if not inspection:
            now=self.clock()
            if (type(now) not in (int,float) or not math.isfinite(now) or now<state['started_at']
                    or self._last_local_time is not None and now<self._last_local_time):
                raise DevelopmentError('family_clock_invalid')
            if self.journal.stopped():raise DevelopmentError('family_stop_requested')
            if not terminal and now>=state['configuration']['opening']['limits']['deadline']:
                raise DevelopmentError('family_deadline')
            self._last_local_time=now
        return True

    def _live(self,state,*,terminal=False):
        self._local_live(state,terminal=terminal)
        self._root(state)
        return self._view(state,terminal=terminal) if 'family_id' in state['payload'] else None

    def _opening_receipt(self,state,value,*,validate_live=True):
        config=state['configuration']
        if (type(value) is not dict or value.get('plan')!=config['server_plan']
                or value.get('plan_sha256')!=config['server_plan_sha256']
                or value.get('parent_ticket_id')!=config['scope']['ticket_id']
                or any(value.get(key)!=config['scope'][key] for key in ('owner_id','agent_id','project_id'))):
            raise DevelopmentError('family_open_unconfirmed')
        opaque(value['id'])
        if type(value.get('children')) is not list or len(value['children'])!=2:
            raise DevelopmentError('family_open_unconfirmed')
        roles={member['role']:member for member in value['children']}
        if len(roles)!=2 or set(roles)!={child['role'] for child in config['server_plan']['children']}:
            raise DevelopmentError('family_open_unconfirmed')
        scopes={'parent':config['scope']}
        for slot in SLOTS[:2]:
            planned=config['inputs'][slot];member=roles[planned['role']]
            if member['model']!=planned['model']:raise DevelopmentError('family_open_unconfirmed')
            scopes[slot]={**config['scope'],'ticket_id':member['ticket_id'],'board_session_id':planned['session_id']}
        self._save(state,'opened',family_id=value['id'],slot_scopes=scopes,opening_confirmed=True,remote_revision=value['revision'])
        if validate_live:self._live(state)

    def _open(self,state,*,reconcile=False):
        if 'family_id' in state['payload']:return
        if 'opening_intent' not in state['payload']:
            if reconcile:return
            self._live(state)
            intent=deepcopy(state['configuration']['opening'])
            intent['idempotency_key']='dev-family-open-'+state['operation_id']
            self._save(state,'opening',opening_intent=intent)
        intent=state['payload']['opening_intent']
        found=self.client.call('lookup_delegation_family',{'parent_ticket_id':intent['parent_ticket_id'],
                                                       'idempotency_key':intent['idempotency_key']})
        if found.get('found') is True:
            self._opening_receipt(state,found['family'],validate_live=not reconcile);return
        if found!= {'found':False}:raise DevelopmentError('family_open_unconfirmed')
        if reconcile:return
        self._live(state)
        try:self.client.call('open_delegation_family',intent)
        except Exception:pass
        found=self.client.call('lookup_delegation_family',{'parent_ticket_id':intent['parent_ticket_id'],
                                                       'idempotency_key':intent['idempotency_key']})
        if found.get('found') is not True:raise DevelopmentError('family_open_unconfirmed')
        self._opening_receipt(state,found['family'])

    def open(self):
        with self.journal.lock():
            state=self.journal.read();self._open(state)
            return self._public(state)

    def bind(self):
        """Seal the supplied child manifests/qualified plans after IDs exist."""
        with self.journal.lock():
            state=self.journal.read();self._live(state)
            if 'family_id' not in state['payload']:raise DevelopmentError('family_not_open')
            if 'bindings' in state['payload']:return self._public(state)
            config=state['configuration'];bindings={}
            for slot in SLOTS:
                worker=self.workflows[slot];item=config['inputs'][slot]
                with self._qualification_resources(config,slot,state) as resources:
                    document,policy,backend=resources;manifest=document.manifest
                    expected={key:state['payload']['slot_scopes'][slot][key] for key in
                              ('owner_id','agent_id','project_id','ticket_id')}
                    expected.update(device_id=config['parent_manifest']['scope']['device_id'],
                        session_id=state['payload']['slot_scopes'][slot]['board_session_id'],
                        profile_version=config['parent_manifest']['scope']['profile_version'])
                    paths=sorted(config['parent_manifest']['files']) if slot=='parent' else config['inputs'][slot]['files']
                    if (manifest['scope']!=expected or manifest['root']!=config['roots'][slot]['path']
                            or manifest['root_identity']!=config['roots'][slot]['identity']
                            or manifest['base_sha']!=config['parent_manifest']['base_sha']
                            or manifest['resource']!=config['parent_manifest']['resource']
                            or not set(manifest['actions']).issubset(config['parent_manifest']['actions'])
                            or sorted(manifest['files'])!=paths
                            or any(manifest['files'][path]!=config['parent_manifest']['files'][path] for path in paths)):
                        raise DevelopmentError('family_manifest_caps_changed')
                    qualification={'policy':policy.fingerprint,'backend':worker._backend_pin(backend)}
                    if slot=='parent' and (document.manifest_hash!=config['parent_manifest_sha256']
                            or qualification!=config['parent_qualification']):
                        raise DevelopmentError('family_qualification_changed')
                    bindings[slot]={'manifest_sha256':document.manifest_hash,**qualification}
            self._live(state);self._save(state,'bound',bindings=bindings)
            return self._public(state)

    def _worker(self,state,slot,*,terminal=False):
        config=state['configuration'];base=self.workflows[slot]
        descriptor={'kind':'delegation_family','operation_id':state['operation_id'],
            'family_id':state['payload']['family_id'],'slot':slot,'plan_sha256':config['server_plan_sha256'],
            'configuration_sha256':state['configuration_sha256'],'journal':_directory_pin(self.journal.directory)}
        def live():self._live(state,terminal=terminal);return True
        def local(**options):return self._local_live(state,terminal=terminal,**options)
        delegated = FamilyAuthorizationGuard(self, state, slot, terminal=terminal)
        return DevelopmentBoardWorker(base.journal,base.store,base.runtime,base.client,base.control,
            backend_factory=base.backend_factory,policy_factory=base.policy_factory,planner_factory=base.planner_factory,
            _transport=base.transport,_test_anchor=base.test_anchor,_allow_fixture_backend=base.allow_fixture,
            clock=self.clock,controller_scope=descriptor,controller_guard=live,delegation_guard=delegated,
            controller_local_guard=local,
            fence_intent=lambda stage,target:self.journal.record_target(state,slot,stage,target))

    def _prepare_workflow(self,state,slot,instruction):
        config=state['configuration'];item=config['inputs'][slot];worker=self._worker(state,slot)
        binding=state['payload']['bindings'][slot]
        manifest,hash_value=manifest_document(item['broker_directory'])
        if hash_value!=binding['manifest_sha256']:raise DevelopmentError('family_manifest_changed')
        with self._qualification_resources(config,slot,state) as resources:
            _,policy,backend=resources
            if {'policy':policy.fingerprint,'backend':worker._backend_pin(backend)}!={k:binding[k] for k in ('policy','backend')}:
                raise DevelopmentError('family_qualification_changed')
        if slot not in state['payload'].get('workflow_intents',{}):
            intents=deepcopy(state['payload'].get('workflow_intents',{}))
            intents[slot]={'instruction':instruction,'operation_id':state['workflow_ids'][slot]}
            self._save(state,workflow_intents=intents)
        intent=state['payload']['workflow_intents'][slot]
        if intent!={'instruction':instruction,'operation_id':state['workflow_ids'][slot]}:
            raise DevelopmentError('family_workflow_intent_changed')
        prior=worker.journal.find(state['payload']['slot_scopes'][slot],'family-'+state['workflow_ids'][slot])
        if prior is not None:
            worker._pin_controller(prior)
            expected={'instruction':instruction,'model':item['model'],'budgets':item['budgets'],
                'manifest_hash':binding['manifest_sha256'],'policy_hash':binding['policy'],'backend_hash':binding['backend']}
            if any(prior['configuration'].get(key)!=value for key,value in expected.items()):
                raise DevelopmentError('family_workflow_intent_changed')
            return worker  # Receipt recovery after release must not re-prepare a claim.
        worker.prepare(scope=state['payload']['slot_scopes'][slot],request_id='family-'+state['workflow_ids'][slot],
            instruction=instruction,broker_directory=item['broker_directory'],backend_directory=item['backend_directory'],
            planner_policy_directory=item['planner_policy_directory'],model=item['model'],
            budgets=PlannerBudgets(**item['budgets']),operation_id=state['workflow_ids'][slot],
            registration=config['registrations'][slot])
        return worker

    def _audit(self,state,slot,worker,*,expected=None):
        # Charge the worst-case bound durably before reading. A crash never
        # restores a consumed allowance; no source content enters Runtime.
        used=state['payload'].get('audit_bytes_reserved',0)
        if type(used) is not int or used+MAX_AUDIT_BYTES>state['configuration']['max_audit_bytes']:
            raise DevelopmentError('family_audit_budget_exhausted')
        self._save(state,audit_bytes_reserved=used+MAX_AUDIT_BYTES)
        return audit_workflow(worker,state['workflow_ids'][slot],
            manifest_hash=state['payload']['bindings'][slot]['manifest_sha256'],
            authorize=lambda:self._live(state) is not None,expected_hashes=expected)

    def _accept(self,state,slot,*,reconcile=False,fresh_audit=False):
        payload=state['payload'];intent=payload.get('accept_intents',{}).get(slot)
        if intent is None:return False
        family=self._view(state)
        member=next(child for child in family['children'] if child['ticket_id']==intent['child_ticket_id'])
        if member['state']=='accepted':
            if member['result_sha256']!=intent['result_sha256'] or member['accepted_version']!=intent['expected_child_version']+1:
                raise DevelopmentError('family_accept_unconfirmed')
        elif reconcile:return False
        else:
            self._live(state)
            if not fresh_audit:
                audited=self._audit(state,slot,self._worker(state,slot))
                if audited!=payload['audits'][slot]:raise DevelopmentError('family_accept_audit_changed')
            op=self.workflows[slot].journal.get(state['workflow_ids'][slot])
            publication=op['payload']['publication']
            ticket=self.client.call('get_ticket',{'ticket_id':intent['child_ticket_id']})['ticket']
            if (ticket['version']!=intent['expected_child_version'] or ticket['claimant'] is not None
                    or ticket['status']!='review' or any(ticket[k]!=publication['changes'][k] for k in ('outcome','evidence'))):
                raise DevelopmentError('family_child_publication_unconfirmed')
            try:self.client.call('accept_delegation_result',intent)
            except Exception:pass
            family=self._view(state)
            member=next(child for child in family['children'] if child['ticket_id']==intent['child_ticket_id'])
            if (member['state']!='accepted' or member['result_sha256']!=intent['result_sha256']
                    or member['accepted_version']!=intent['expected_child_version']+1):
                raise DevelopmentError('family_accept_unconfirmed')
        accepted=deepcopy(payload.get('accepted',{}));accepted[slot]=intent['result_sha256']
        self._save(state,accepted=accepted,remote_revision=family['revision'])
        return True

    def _finalize(self,state,*,reconcile=False,fresh_audit=False):
        intent=state['payload'].get('finalize_intent')
        if intent is None:return False
        value=self._view(state,terminal=True)
        if value['state']=='review_ready':
            if value['final_snapshot_sha256']!=intent['snapshot_sha256'] or value['revision']!=intent['expected_revision']+1:raise DevelopmentError('family_finalize_unconfirmed')
        elif reconcile:return False
        else:
            self._live(state)
            if not fresh_audit:
                audited=self._audit(state,'parent',self._worker(state,'parent'),expected=state['payload']['parent_audit']['files'])
                if audited!=state['payload']['parent_audit']:raise DevelopmentError('family_finalize_audit_changed')
            try:self.client.call('finalize_delegation_family',intent)
            except Exception:pass
            value=self._view(state,terminal=True)
            if (value['state']!='review_ready' or value['final_snapshot_sha256']!=intent['snapshot_sha256']
                    or value['revision']!=intent['expected_revision']+1):
                raise DevelopmentError('family_finalize_unconfirmed')
        self._save(state,'review_ready',finalization_confirmed=True,remote_revision=value['revision'])
        return True

    def run(self):
        with self.journal.lock():
            state=self.journal.read()
            if state['phase'] in ('review_ready','needs_review','stopped'):return self._public(state)
            try:
                if state['payload'].get('finalize_intent') is not None:
                    self._finalize(state);return self._public(state)
                self._live(state);self._open(state)
                if 'bindings' not in state['payload']:raise DevelopmentError('family_bindings_required')
                self._save(state,'running')
                for slot in SLOTS[:2]:
                    if slot in state['payload'].get('accepted',{}):continue
                    if self._accept(state,slot):continue
                    worker=self._prepare_workflow(state,slot,state['configuration']['inputs'][slot]['instruction'])
                    existing=worker.journal.get(state['workflow_ids'][slot])
                    if existing['stage'] not in ('publishing','complete'):
                        result=worker.run(state['workflow_ids'][slot])
                        if result['status'] not in ('review_ready','complete'):
                            raise DevelopmentError('family_child_requires_review')
                    published=worker.publish(state['workflow_ids'][slot],audit_outcome=True)
                    if published['status']!='complete':raise DevelopmentError('family_child_publication_unconfirmed')
                    audited=self._audit(state,slot,worker)
                    audits=deepcopy(state['payload'].get('audits',{}));audits[slot]=audited
                    candidates=[change for child in SLOTS[:2] for change in audits.get(child,{}).get('candidates',[])]
                    if len(canonical(candidates))>MAX_CANDIDATE_BYTES:raise DevelopmentError('family_candidate_too_large')
                    baseline=state['configuration']['parent_manifest']['files']
                    if any(change['before_sha256']!=baseline[change['path']]['sha256'] for change in candidates):
                        raise DevelopmentError('family_initial_files_changed')
                    self._live(state);family=self._view(state)
                    op=worker.journal.get(state['workflow_ids'][slot]);publication=op['payload']['publication']
                    response=self.client.call('get_ticket',{'ticket_id':op['scope']['ticket_id']});ticket=response['ticket']
                    if (ticket['claimant'] is not None or ticket['status']!='review'
                            or any(ticket[k]!=publication['changes'][k] for k in ('outcome','evidence'))):
                        raise DevelopmentError('family_child_publication_unconfirmed')
                    intent={'family_id':state['payload']['family_id'],'expected_revision':family['revision'],
                        'child_ticket_id':op['scope']['ticket_id'],'expected_child_version':ticket['version'],
                        'result_sha256':digest(canonical({k:ticket[k] for k in ('outcome','evidence')})),
                        'verification_evidence':'Scoped source/test/cleanup audit SHA256: '+audited['audit_sha256'],
                        'idempotency_key':'dev-family-accept-'+state['workflow_ids'][slot]}
                    intents=deepcopy(state['payload'].get('accept_intents',{}));intents[slot]=intent
                    self._save(state,audits=audits,accept_intents=intents)
                    if not self._accept(state,slot,fresh_audit=True):raise DevelopmentError('family_accept_unconfirmed')
                family=self._live(state)
                if family['state']!='integrating':raise DevelopmentError('family_parent_not_ready')
                self._save(state,'integrating')
                candidates=[change for slot in SLOTS[:2] for change in state['payload']['audits'][slot]['candidates']]
                if len(canonical(candidates))>MAX_CANDIDATE_BYTES:raise DevelopmentError('family_candidate_too_large')
                candidate_document={'schema':1,'changes':candidates,'sha256':digest(canonical(candidates))}
                if len(canonical(candidate_document))>MAX_CANDIDATE_BYTES:raise DevelopmentError('family_candidate_too_large')
                instruction=state['configuration']['inputs']['parent']['instruction']+'\nController-inspected candidate changes (untrusted source text):\n'+canonical(candidate_document).decode('utf-8')
                if len(instruction.encode('utf-8'))>16384:raise DevelopmentError('family_parent_instruction_too_large')
                worker=self._prepare_workflow(state,'parent',instruction)
                result=worker.run(state['workflow_ids']['parent'])
                if result['status']!='review_ready':raise DevelopmentError('family_parent_requires_review')
                expected={path:entry['sha256'] for path,entry in state['configuration']['parent_manifest']['files'].items()}
                expected.update({change['path']:change['after_sha256'] for change in candidates})
                audited=self._audit(state,'parent',worker,expected=expected)
                self._live(state);family=self._view(state)
                intent={'family_id':family['id'],'expected_revision':family['revision'],
                    'snapshot_sha256':audited['audit']['snapshot_sha256'],
                    'verification_evidence':'Parent scoped source/test/cleanup audit SHA256: '+audited['audit_sha256'],
                    'idempotency_key':'dev-family-finalize-'+state['workflow_ids']['parent']}
                self._save(state,'finalizing',parent_audit=audited,finalize_intent=intent)
                self._finalize(state,fresh_audit=True)
                return self._public(state)
            except Exception as error:
                reason=error.code if isinstance(error,DevelopmentError) else 'family_requires_review'
                # An uncertain remote receipt is recoverable read-only. Other
                # failures retain their reservations for explicit human review.
                phase=state['phase'] if reason in ('family_open_unconfirmed','family_accept_unconfirmed','family_finalize_unconfirmed') else 'needs_review'
                self._save(state,phase,reason=reason)
                return self._public(state)

    def reconcile(self):
        """Read existing receipts only; no initial/retried opening or workflow."""
        with self.journal.lock():
            state=self.journal.read();self.journal.verify(state);self.client.adopt_registration()
            self._open(state,reconcile=True)
            if self.journal.stopped():
                try:remote=self._stop_remote(state,reconcile=True)
                except Exception:remote='pending'
                return {**self._public(state),'stop_requested':True,'server_stop':remote}
            if 'family_id' in state['payload']:
                if state['payload'].get('finalize_intent') is not None:
                    self._finalize(state,reconcile=True)
                else:
                    for slot in SLOTS[:2]:
                        if slot not in state['payload'].get('accepted',{}):self._accept(state,slot,reconcile=True)
            return self._public(state)

    def cleanup(self):
        """Exact identity-only Runtime cleanup, even after grants/claim loss."""
        with self.journal.lock():
            state=self.journal.read();self.journal.verify(state);receipts={}
            for slot in SLOTS:
                if slot not in state['payload'].get('workflow_intents',{}):continue
                worker=self._worker(state,slot)
                receipts[slot]=worker.reconcile_cleanup(state['workflow_ids'][slot])
            return {'operation_id':state['operation_id'],'status':'needs_review','cleanup':receipts}

    def _stop_remote(self,state,*,reconcile=False):
        if 'family_id' not in state['payload']:
            return 'pending' if 'opening_intent' in state['payload'] else 'not_opened'
        private=self.journal.private;private.verify()
        try:intent=decode(private.read('server-stop.json',2048))
        except DevelopmentError as error:
            if error.code!='private_read_failed':raise
            if reconcile:return 'pending'
            self.client.adopt_registration()
            value=self.client.call('get_delegation_family',{'family_id':state['payload']['family_id']})
            if (value.get('id')!=state['payload']['family_id'] or value.get('plan_sha256')!=state['configuration']['server_plan_sha256']
                    or value.get('owner_id')!=state['configuration']['scope']['owner_id']):
                raise DevelopmentError('family_stop_unconfirmed')
            if value['state']=='review_ready':return 'already_review_ready'
            intent={'schema':1,'operation_id':state['operation_id'],'arguments':{
                'family_id':state['payload']['family_id'],'expected_revision':value['revision']}}
            try:private.create('server-stop.json',canonical(intent))
            except DevelopmentError as error:
                if error.code!='private_file_exists':raise
                intent=decode(private.read('server-stop.json',2048))
        if (type(intent) is not dict or set(intent)!={'schema','operation_id','arguments'}
                or intent['schema']!=1 or intent['operation_id']!=state['operation_id']
                or intent['arguments'].get('family_id')!=state['payload']['family_id']
                or type(intent['arguments'].get('expected_revision')) is not int):
            raise DevelopmentError('family_stop_unconfirmed')
        self.client.adopt_registration()
        if not reconcile:
            try:self.client.call('stop_delegation_family',intent['arguments'])
            except Exception:pass
        value=self.client.call('get_delegation_family',{'family_id':state['payload']['family_id']})
        if (value.get('id')==intent['arguments']['family_id'] and value.get('state')=='cancelled'
                and value.get('revision')==intent['arguments']['expected_revision']+1):return 'confirmed'
        return 'pending'

    def stop(self):
        self.journal.request_stop()  # Never wait for the execution lock.
        state=self.journal.read()
        if 'family_id' not in state['payload'] and 'opening_intent' in state['payload']:
            try:
                with self.journal.lock():
                    state=self.journal.read();self._open(state,reconcile=True)
            except Exception:pass
        for slot in SLOTS:
            if slot in state['payload'].get('workflow_intents',{}):
                try:self.workflows[slot].stop(state['workflow_ids'][slot])
                except Exception:pass  # Marker still blocks a not-yet-created child.
        try:remote=self._stop_remote(state)
        except Exception:remote='pending'
        return {**self._public(state),'stop_requested':True,'server_stop':remote,
                'cleanup_confirmed':False}

    def publish_parent(self):
        with self.journal.lock():
            state=self.journal.read()
            if state['phase']!='review_ready':raise DevelopmentError('family_parent_not_ready')
            if state['payload'].get('parent_publication_confirmed') is True:return self._public(state)
            self._live(state,terminal=True)
            result=self._worker(state,'parent',terminal=True).publish(state['workflow_ids']['parent'],audit_outcome=True)
            if result['status']!='complete':raise DevelopmentError('family_parent_publication_unconfirmed')
            self._save(state,parent_publication_confirmed=True)
            return self._public(state)
