"""Explicit human-delegated management of ordinary, bounded agent profiles.

Existing action/resource intersections carry this grant. A role label, legacy
wildcard, device capability or management tool argument never grants authority.
There is no credential, device binding, Runtime or server-management API here.
"""
import hashlib
import json
import math
import secrets
import time

from profiles import DEFAULT_POLICY,_insert_profile
from projects import _fail,_json,_text,_version
from owner_powers import has_owner_power

MANAGEMENT_ACTIONS=('agent.profile.read','agent.profile.create','agent.profile.update','agent.profile.archive')
CLAIM_RELEASE_ACTION='agent.profile.release_claim'
TICKET_RESOURCE_ACTION='agent.ticket.retarget_resource'
TICKET_CREATION_ACTION='agent.ticket.create_assigned'
MANAGEMENT_PREFIX='agent.profile.'
TICKET_MANAGEMENT_PREFIX='agent.ticket.'
RESOURCE_PREFIX='agent-profiles:'
AUTONOMY={'responses':0,'requested':1,'queue':2}
PROFILE_MANAGEMENT_TOOLS=frozenset({'list_managed_agent_profiles','get_managed_agent_profile',
    'create_managed_agent_profile','update_managed_agent_profile','archive_managed_agent_profile',
    'release_managed_agent_claim','retarget_managed_ticket_resource'})


def _identity(value):
    if (type(value) is not str or not value.strip() or len(value)>200
            or any(ord(char)<32 or ord(char)==127 for char in value)):_fail('Invalid management identifier')


def validate_profile_management_arguments(name,args):
    allowed={'session_id','view'};required={'session_id'}
    if name=='list_managed_agent_profiles':allowed|={'include_archived','limit','cursor'}
    elif name=='get_managed_agent_profile':allowed|={'profile_id'};required|={'profile_id'}
    elif name=='create_managed_agent_profile':allowed|={'profile'};required|={'profile'}
    elif name in ('update_managed_agent_profile','archive_managed_agent_profile'):
        allowed|={'profile_id','expected_version','idempotency_key'};required|={'profile_id','expected_version','idempotency_key'}
        if name=='update_managed_agent_profile':allowed.add('changes');required.add('changes')
    elif name=='release_managed_agent_claim':
        fields={'ticket_id','expected_version','idempotency_key','reason','recovery_evidence'}
        allowed|=fields;required|=fields
    elif name=='retarget_managed_ticket_resource':
        fields={'ticket_id','expected_version','expected_resource','new_resource','idempotency_key','reason'}
        allowed|=fields;required|=fields
    else:_fail('Unknown management tool')
    if type(args) is not dict or set(args)-allowed or not required<=set(args):_fail('Supply only management tool fields')
    for key in ('session_id','profile_id','ticket_id','idempotency_key'):
        if key in args:_identity(args[key])
    if 'view' in args and (type(args['view']) is not str or args['view'] not in ('full','compact')):_fail('Invalid management view')
    if 'expected_version' in args:_version(args['expected_version'])
    for key in ('profile','changes'):
        if key in args and type(args[key]) is not dict:_fail('Profile and changes must be JSON objects')
    if 'include_archived' in args and type(args['include_archived']) is not bool:_fail('include_archived must be boolean')
    if 'limit' in args and (type(args['limit']) is not int or not 1<=args['limit']<=100):_fail('Invalid management page size')
    if args.get('cursor') is not None:_identity(args['cursor'])
    for key in ('reason','recovery_evidence'):
        if key in args:_text(args[key],key,1,20000)
    for key in ('expected_resource','new_resource'):
        if key in args:_ticket_resource(args[key],key,empty=key=='expected_resource')


def management_resource(owner_id):return RESOURCE_PREFIX+owner_id


def _reserved_action(value):
    return '*' in value or value.startswith((MANAGEMENT_PREFIX,TICKET_MANAGEMENT_PREFIX,'runtime.','server.','provider.','device.','credential.','policy.'))


def _reserved_resource(value):
    return '*' in value or value.startswith((RESOURCE_PREFIX,'runtime:','server:','provider:','device:','credential:','policy:'))


def _is_management(row):
    policy=json.loads(row['policy'])
    return (any(action.startswith((MANAGEMENT_PREFIX,TICKET_MANAGEMENT_PREFIX)) for action in policy['actions'])
            or any(resource.startswith(RESOURCE_PREFIX) for resource in policy['resources']))


def _ticket_resource(value,name,*,empty=False):
    _text(value,name,0 if empty else 1,200)
    if (value!=value.strip() or any(ord(char)<32 or ord(char)==127 for char in value)
            or value and _reserved_resource(value)):_fail('Supply an exact ordinary ticket resource')
    return value


class ProfileManagementMixin:
    def _profile_manager(self,db,actor,action,*,read=False):
        actor=self._actor(db,actor,'allow_read' if read else 'allow_requests')
        if actor['kind']!='agent':_fail('Use the human agent profile editor',403)
        profile=self._profile_for_actor(db,actor);stored=json.loads(profile['policy'])
        if profile['kind']=='companion':_fail('Companions use bounded owner profile controls',403)
        effective=self._effective_profile_policy(db,actor,profile);resource=management_resource(actor['owner_id'])
        # The stored explicit grant is essential: legacy wildcard expansion must
        # not turn an owner-level grant into implicit management for old agents.
        if (profile['legacy'] or json.loads(profile['project_ids']) is None
                or action not in stored['actions'] or resource not in stored['resources']
                or action not in effective['actions'] or resource not in effective['resources']):
            _fail('Human-delegated agent profile management is required',403)
        return actor,profile,effective

    def _managed_bounds(self,db,actor,manager,ceiling,policy,project_ids):
        if (type(project_ids) is not list or not set(project_ids)<=set(json.loads(manager['project_ids']))
                or policy.get('allow_create_projects') is True):_fail('Profile exceeds delegated project scope',403)
        for identifier in project_ids:self._project_member(db,actor,identifier)
        for target,source in (('allow_read','allow_read'),('allow_messages','allow_reply'),('allow_requests','allow_requests')):
            if policy[target] and not ceiling[source]:_fail('Profile permissions exceed the management ceiling',403)
        if AUTONOMY[policy['autonomy']]>AUTONOMY[ceiling['autonomy']]:_fail('Profile autonomy exceeds the management ceiling',403)
        for name,reserved in (('actions',_reserved_action),('resources',_reserved_resource)):
            if any(reserved(value) or value not in ceiling[name] for value in policy[name]):
                _fail('Profile grants exceed the management ceiling',403)

    def _managed_target(self,db,actor,manager,ceiling,profile_id):
        row=self._profile_owned(db,actor,profile_id)
        if row['kind']=='companion' or row['id']==manager['id'] or row['legacy'] or _is_management(row):
            _fail('Management identities and policies remain human-owned',403)
        self._managed_bounds(db,actor,manager,ceiling,json.loads(row['policy']),json.loads(row['project_ids']))
        return row

    def _managed_ticket_creation(self,db,actor,project_id,assignee_owner_id,assignee_agent_id,resource):
        """Authorize only creation-time assignment; never reuse a receipt as a grant."""
        actor,manager,ceiling=self._profile_manager(db,actor,TICKET_CREATION_ACTION)
        stored=json.loads(manager['policy']);management=management_resource(actor['owner_id'])
        if (ceiling.get('allow_read') is not True
                or any('agent.profile.read' not in value['actions'] or management not in value['resources']
                    for value in (stored,ceiling))):
            _fail('Assigned ticket creation needs current explicit profile-read delegation',403)
        if assignee_owner_id not in (None,actor['owner_id']):
            _fail('Assigned ticket creation is limited to the delegated owner',403)
        self._project_member(db,actor,project_id)
        if project_id not in json.loads(manager['project_ids']):
            _fail('Assigned ticket creation is outside delegated project scope',403)
        resource=_ticket_resource(resource,'resource')
        if resource not in stored['resources'] or resource not in ceiling['resources']:
            _fail('Assigned ticket creation is outside delegated resource scope',403)
        if assignee_agent_id==manager['id']:
            # The exact authenticated manager may select itself. Its management
            # grants remain human-owned; no profile mutation or grant transfer occurs.
            target=manager;policy=stored
            if (policy.get('allow_create_projects') is True
                    or any(policy[target_key] and not ceiling[source_key] for target_key,source_key in
                        (('allow_read','allow_read'),('allow_messages','allow_reply'),('allow_requests','allow_requests')))
                    or AUTONOMY[policy['autonomy']]>AUTONOMY[ceiling['autonomy']]
                    or any(value not in ceiling[name] for name in ('actions','resources') for value in policy[name])):
                _fail('Assigned self work exceeds the management ceiling',403)
        else:
            target=self._managed_target(db,actor,manager,ceiling,assignee_agent_id)
            policy=json.loads(target['policy'])
        if (target['kind']!='agent' or target['legacy'] or target['status']!='active'
                or project_id not in json.loads(target['project_ids'])
                or not policy['allow_read'] or not policy['allow_requests'] or policy['autonomy']=='responses'
                or resource not in policy['resources']):
            _fail('Assigned ticket destination is outside delegated worker scope',403)

    @staticmethod
    def _managed_view(row):
        # Deliberately no bindings, device identities, connection keys or active
        # claims from an unrelated/revoked project are returned to this channel.
        return {**{key:row[key] for key in ('id','owner_id','status','version','created_at','updated_at')},
                **json.loads(row['fields']),'project_ids':json.loads(row['project_ids']),'policy':json.loads(row['policy'])}

    def set_companion_agent_status(self,actor,profile_id,expected_version,status,idempotency_key):
        """Pause/resume an owned ordinary agent without editing its other fields."""
        _version(expected_version);_identity(profile_id);_identity(idempotency_key)
        if status not in ('active','paused'):_fail('Use active or paused for an ordinary agent',400)
        payload=dict(profile_id=profile_id,expected_version=expected_version,status=status)
        with self._tx() as db:
            actor=self._actor(db,actor,'allow_requests')
            if not has_owner_power(actor,'manage_agents'):_fail('Companion owner profile control is required',403)
            row=self._profile_owned(db,actor,profile_id)
            if row['kind']!='agent':_fail('Only an owned ordinary agent can be paused or resumed',403)
            previous=self._retry(db,actor,'companion_agent_status',idempotency_key,payload)
            if previous is not None:return json.loads(previous)
            if row['version']!=expected_version:_fail('Agent profile changed; reload before saving',409)
            if row['status']=='archived':_fail('Only a human can restore an archived profile',403)
            self._write_profile(db,profile_id,{'status':status})
            result=self._profile_public(self._profile_owned(db,actor,profile_id))
            self._remember(db,actor,'companion_agent_status',idempotency_key,payload,_json(result))
            self._audit(db,actor,'companion_agent_status_changed',dict(profile_id=profile_id,status=status,version=expected_version+1))
            return result

    def update_companion_profile(self,actor,name=None,avatar=None,description=None):
        """A companion keeps its own name, avatar and description in step with its companion app.
        Nothing else: kind, policy, projects, status and bindings stay with the human owner."""
        fields={key:value for key,value in (('name',name),('avatar',avatar),('description',description)) if value is not None}
        if not fields:_fail('Supply name, avatar or description',400)
        with self._tx() as db:
            actor=self._actor(db,actor)
            profile=self._profile_for_actor(db,actor)
            if not profile or profile['kind']!='companion' or actor.get('companion_for')!=actor['owner_id']:
                _fail('Only a companion can update its own presentation',403)
            values=self._profile_values(db,actor,fields)
            current=json.loads(profile['fields'])
            if all(current.get(key)==value for key,value in values.items()):
                return self._profile_public(profile)
            self._write_profile(db,profile['id'],values)
            # The session that made the change stays current; other sessions re-register as usual.
            db.execute('UPDATE sessions SET profile_version=? WHERE id=?',(profile['version']+1,actor['session_id']))
            self._audit(db,actor,'companion_presentation_updated',dict(profile_id=profile['id'],version=profile['version']+1,fields=sorted(values)))
            return self._profile_public(db.execute('SELECT * FROM agent_profiles WHERE id=?',(profile['id'],)).fetchone())

    def _managed_retry(self,db,actor,manager,ceiling,operation,key,payload):
        _identity(key)
        previous=self._retry(db,actor,operation,key,payload)
        if previous is None:return None
        result=json.loads(previous)
        self._managed_target(db,actor,manager,ceiling,result['id'])
        self._managed_bounds(db,actor,manager,ceiling,result['policy'],result['project_ids'])
        return result

    def list_managed_agent_profiles(self,actor,include_archived=False):
        if type(include_archived) is not bool:_fail('include_archived must be a boolean')
        from board import BoardError
        with self._connection() as db:
            actor,manager,ceiling=self._profile_manager(db,actor,'agent.profile.read',read=True)
            result=[]
            for row in db.execute('SELECT * FROM agent_profiles WHERE owner_id=? AND (? OR status!=?) ORDER BY created_at,id',
                                  (actor['owner_id'],int(include_archived),'archived')):
                try:self._managed_target(db,actor,manager,ceiling,row['id'])
                except BoardError:continue
                result.append(self._managed_view(row))
            return result

    def get_managed_agent_profile(self,actor,profile_id):
        with self._connection() as db:
            actor,manager,ceiling=self._profile_manager(db,actor,'agent.profile.read',read=True)
            return self._managed_view(self._managed_target(db,actor,manager,ceiling,profile_id))

    def create_managed_agent_profile(self,actor,name,idempotency_key,**fields):
        with self._tx() as db:
            actor,manager,ceiling=self._profile_manager(db,actor,'agent.profile.create')
            values=self._profile_values(db,actor,dict(name=name,**fields))
            if values.get('status')=='archived':_fail('Create an active or paused profile; archive uses its own grant',403)
            policy=values.get('policy',dict(DEFAULT_POLICY));projects=values.get('project_ids',[])
            self._managed_bounds(db,actor,manager,ceiling,policy,projects)
            previous=self._managed_retry(db,actor,manager,ceiling,'managed_profile_create',idempotency_key,values)
            if previous is not None:return previous
            identifier=secrets.token_hex(16);_insert_profile(db,identifier,actor['owner_id'],values['name'])
            self._write_profile(db,identifier,values,increment=False)
            result=self._managed_view(self._profile_owned(db,actor,identifier))
            self._remember(db,actor,'managed_profile_create',idempotency_key,values,_json(result))
            self._audit(db,actor,'managed_agent_profile_created',dict(profile_id=identifier,manager_profile_id=manager['id'],version=1))
            return result

    def update_managed_agent_profile(self,actor,profile_id,expected_version,idempotency_key,**fields):
        _version(expected_version)
        if not fields:_fail('Supply at least one profile change')
        if fields.get('status')=='archived':_fail('Use archive_managed_agent_profile with its explicit grant',403)
        with self._tx() as db:
            actor,manager,ceiling=self._profile_manager(db,actor,'agent.profile.update')
            row=self._managed_target(db,actor,manager,ceiling,profile_id)
            values=self._profile_values(db,actor,fields)
            payload=dict(profile_id=profile_id,expected_version=expected_version,changes=values)
            previous=self._managed_retry(db,actor,manager,ceiling,'managed_profile_update',idempotency_key,payload)
            if previous is not None:return previous
            if row['version']!=expected_version:_fail('Agent profile changed; reload before saving',409)
            policy=dict(values.get('policy',json.loads(row['policy'])))
            if 'allow_create_projects' not in policy and 'allow_create_projects' in json.loads(row['policy']):
                policy['allow_create_projects']=json.loads(row['policy'])['allow_create_projects']
            self._managed_bounds(db,actor,manager,ceiling,policy,values.get('project_ids',json.loads(row['project_ids'])))
            self._write_profile(db,profile_id,values)
            result=self._managed_view(self._profile_owned(db,actor,profile_id))
            self._remember(db,actor,'managed_profile_update',idempotency_key,payload,_json(result))
            self._audit(db,actor,'managed_agent_profile_updated',dict(profile_id=profile_id,manager_profile_id=manager['id'],
                version=expected_version+1,fields=sorted(values)))
            return result

    def archive_managed_agent_profile(self,actor,profile_id,expected_version,idempotency_key):
        _version(expected_version);payload=dict(profile_id=profile_id,expected_version=expected_version)
        with self._tx() as db:
            actor,manager,ceiling=self._profile_manager(db,actor,'agent.profile.archive')
            row=self._managed_target(db,actor,manager,ceiling,profile_id)
            previous=self._managed_retry(db,actor,manager,ceiling,'managed_profile_archive',idempotency_key,payload)
            if previous is not None:return previous
            if row['version']!=expected_version:_fail('Agent profile changed; reload before archiving',409)
            if row['status']=='archived':_fail('Profile is already archived; use the original retry key',409)
            self._write_profile(db,profile_id,{'status':'archived'})
            result=self._managed_view(self._profile_owned(db,actor,profile_id))
            self._remember(db,actor,'managed_profile_archive',idempotency_key,payload,_json(result))
            self._audit(db,actor,'managed_agent_profile_archived',dict(profile_id=profile_id,manager_profile_id=manager['id'],version=expected_version+1))
            return result

    def release_managed_agent_claim(self,actor,ticket_id,expected_version,idempotency_key,reason,recovery_evidence):
        """Release an inspected, expired ordinary-agent claim within delegation.

        This edits responsibility only. It never stops, resumes or completes an
        execution, fabricates cleanup, clears Runtime capacity or reassigns work.
        """
        _version(expected_version);_identity(ticket_id);_identity(idempotency_key)
        reason=_text(reason,'reason',1,20000)
        recovery_evidence=_text(recovery_evidence,'recovery_evidence',1,20000)
        payload=dict(ticket_id=ticket_id,expected_version=expected_version,
            reason=reason,recovery_evidence=recovery_evidence)
        with self._tx() as db:
            actor,manager,ceiling=self._profile_manager(db,actor,CLAIM_RELEASE_ACTION)
            if ceiling.get('allow_read') is not True:_fail('managed_claim_read_permission_required',403)
            row=self._ticket_member(db,actor,ticket_id)
            try:claimant=json.loads(row['claimant']) if row['claimant'] else None
            except (ValueError,TypeError):_fail('managed_claim_source_mismatch',409)
            previous=self._retry(db,actor,'managed_agent_claim_release',idempotency_key,payload)
            if previous is not None:
                # Revalidate the original managed target, never release a later
                # claimant merely because the same retry key is presented.
                saved=json.loads(previous)
                target=self._managed_target(db,actor,manager,ceiling,saved['profile_id'])
                self._managed_claim_ticket_scope(row,target,ceiling)
                return self._ticket(row,db)
            if row['version']!=expected_version:_fail('managed_claim_version_conflict',409)
            if row['status']=='done':_fail('managed_claim_completed',409)
            if (type(claimant) is not dict or claimant.get('kind')!='agent'
                    or claimant.get('owner_id')!=actor['owner_id']
                    or type(claimant.get('session_id')) is not str):
                _fail('managed_claim_ordinary_agent_required',403)
            source=db.execute('SELECT * FROM sessions WHERE id=? AND owner_id=?',
                (claimant['session_id'],actor['owner_id'])).fetchone()
            if (source is None or not source['agent_id']
                    or claimant.get('device_id')!=source['device_id']
                    or ('agent_id' in claimant and claimant['agent_id']!=source['agent_id'])
                    or ('profile_version' in claimant and (type(claimant['profile_version']) is not int
                        or claimant['profile_version']!=source['profile_version']))):
                _fail('managed_claim_source_mismatch',403)
            target=self._managed_target(db,actor,manager,ceiling,source['agent_id'])
            self._managed_claim_ticket_scope(row,target,ceiling)
            prior=db.execute('SELECT snapshot FROM agent_profile_versions WHERE profile_id=? AND version=?',
                (target['id'],source['profile_version'])).fetchone()
            try:
                prior=json.loads(prior['snapshot']) if prior else None
                if (type(prior) is not dict or prior.get('id')!=target['id']
                        or prior.get('owner_id')!=actor['owner_id'] or prior.get('legacy') is not False
                        or type(prior.get('version')) is not int or prior['version']!=source['profile_version']
                        or type(prior.get('policy')) is not dict or type(prior.get('project_ids')) is not list):
                    _fail('managed_claim_source_profile_outside_scope',403)
                self._profile_values(db,actor,dict(policy=prior['policy'],project_ids=prior['project_ids']))
                if _is_management({'policy':_json(prior['policy'])}):
                    _fail('managed_claim_source_profile_outside_scope',403)
                self._managed_bounds(db,actor,manager,ceiling,prior['policy'],prior['project_ids'])
                if (row['project_id'] not in prior['project_ids'] or row['resource'] and
                        row['resource'] not in prior['policy']['resources']):
                    _fail('managed_claim_source_profile_outside_scope',403)
            except (ValueError,TypeError,KeyError):_fail('managed_claim_source_profile_outside_scope',403)
            if (row['assignee_owner_id'] not in (None,actor['owner_id'])
                    or row['assignee_agent_id'] not in (None,target['id'])):
                _fail('managed_claim_assigned_elsewhere',403)
            expiry=row['lease_expires_at']
            if type(expiry) not in (int,float) or not math.isfinite(expiry) or expiry>time.time():
                _fail('managed_claim_lease_not_expired',409)
            if db.execute('''SELECT 1 FROM runtime_executions WHERE ticket_id=?
                AND (state IS NOT 'finished' OR cleanup_confirmed IS NOT 1) LIMIT 1''',
                (ticket_id,)).fetchone():
                _fail('managed_claim_runtime_unresolved',409)
            now=time.time();next_status='todo' if row['status']=='in_progress' else row['status']
            db.execute('''UPDATE tickets SET status=?,claimant=NULL,claim_started_version=NULL,
                lease_expires_at=NULL,version=version+1,updated_at=? WHERE id=? AND version=?''',
                (next_status,now,ticket_id,expected_version))
            db.execute('UPDATE projects SET updated_at=? WHERE id=?',(now,row['project_id']))
            self._remember(db,actor,'managed_agent_claim_release',idempotency_key,payload,
                _json(dict(ticket_id=ticket_id,profile_id=target['id'])))
            self._audit(db,actor,'managed_agent_claim_released',dict(payload,
                manager_profile_id=manager['id'],target_profile_id=target['id'],
                source_session_id=source['id'],source_profile_version=source['profile_version'],
                version=expected_version+1))
            return self._ticket(self._ticket_member(db,actor,ticket_id),db)

    @staticmethod
    def _managed_claim_ticket_scope(row,target,ceiling):
        policy=json.loads(target['policy']);projects=json.loads(target['project_ids'])
        if (row['project_id'] not in projects or row['resource'] and
                (row['resource'] not in ceiling['resources'] or row['resource'] not in policy['resources'])):
            _fail('managed_claim_ticket_outside_scope',403)

    def set_agent_profile_claim_management(self,actor,profile_id,expected_version,enabled):
        """Human-only, separate opt-in; existing profile managers gain nothing."""
        _version(expected_version)
        if type(enabled) is not bool:_fail('enabled must be a boolean')
        with self._tx() as db:
            actor=self._human(db,actor);row=self._profile_owned(db,actor,profile_id)
            if row['version']!=expected_version:_fail('Agent profile changed; reload before granting claim management',409)
            policy=json.loads(row['policy']);owner=json.loads(self._owner(db,actor['owner_id'])['policy'])
            resource=management_resource(actor['owner_id'])
            if enabled:
                if (row['legacy'] or json.loads(row['project_ids']) is None
                        or any(owner.get(key) is not True for key in ('enabled','allow_read','allow_requests'))
                        or not policy['allow_read'] or not policy['allow_requests'] or policy['autonomy']=='responses'
                        or any('agent.profile.read' not in value['actions'] or resource not in value['resources']
                            for value in (policy,owner))):
                    _fail('Claim management needs an explicit scoped profile-management grant',403)
                policy['actions']=list(dict.fromkeys([*policy['actions'],CLAIM_RELEASE_ACTION]))
                owner['actions']=list(dict.fromkeys([*owner['actions'],CLAIM_RELEASE_ACTION]))
                if len(policy['actions'])>100 or len(owner['actions'])>100:_fail('Management grants exceed the action/resource limit')
                owner.update(granted_by=actor['owner_id'],granted_at=time.time())
                db.execute('UPDATE owners SET policy=? WHERE id=?',(_json(owner),actor['owner_id']))
            else:policy['actions']=[value for value in policy['actions'] if value!=CLAIM_RELEASE_ACTION]
            self._write_profile(db,profile_id,{'policy':policy})
            self._audit(db,actor,'agent_claim_management_granted' if enabled else 'agent_claim_management_revoked',
                dict(profile_id=profile_id,version=expected_version+1,action=CLAIM_RELEASE_ACTION,resource=resource))
            return self._profile_view(db,self._profile_owned(db,actor,profile_id))

    def retarget_managed_ticket_resource(self,actor,ticket_id,expected_version,expected_resource,
                                         new_resource,idempotency_key,reason):
        """Move one unclaimed ordinary ticket's metadata; never move execution."""
        _version(expected_version);_identity(ticket_id);_identity(idempotency_key)
        expected_resource=_ticket_resource(expected_resource,'expected_resource',empty=True)
        new_resource=_ticket_resource(new_resource,'new_resource')
        if expected_resource==new_resource:_fail('The ticket resource is already selected',409)
        reason=_text(reason,'reason',1,20000)
        payload=dict(ticket_id=ticket_id,expected_version=expected_version,
            expected_resource=expected_resource,new_resource=new_resource,reason=reason)
        with self._tx() as db:
            actor,manager,ceiling=self._profile_manager(db,actor,TICKET_RESOURCE_ACTION)
            if ceiling.get('allow_read') is not True:_fail('managed_ticket_read_permission_required',403)
            row=self._ticket_member(db,actor,ticket_id)
            project=self._project_member(db,actor,row['project_id'])
            if row['assignee_owner_id']!=actor['owner_id']:_fail('managed_ticket_owner_mismatch',403)
            if project['archived'] or row['status']=='done':_fail('managed_ticket_closed',409)
            if self._delegation_ticket_family(db,ticket_id) is not None:_fail('managed_ticket_family_forbidden',403)
            if any(row[key] is not None for key in ('claimant','claim_started_version','lease_expires_at')):
                _fail('managed_ticket_claim_present',409)
            if db.execute("SELECT 1 FROM human_requests WHERE ticket_id=? AND status='open' LIMIT 1",(ticket_id,)).fetchone():
                _fail('managed_ticket_waiting_on_human',409)
            if db.execute('''SELECT 1 FROM runtime_executions WHERE ticket_id=?
                AND (state IS NOT 'finished' OR cleanup_confirmed IS NOT 1) LIMIT 1''',(ticket_id,)).fetchone():
                _fail('managed_ticket_runtime_unresolved',409)
            resources=[value for value in (expected_resource,new_resource) if value]
            stored=json.loads(manager['policy'])
            if any(value not in stored['resources'] or value not in ceiling['resources'] for value in resources):
                _fail('managed_ticket_resource_outside_scope',403)
            target=None
            if row['assignee_agent_id'] not in (None,manager['id']):
                target=self._managed_target(db,actor,manager,ceiling,row['assignee_agent_id'])
                policy=json.loads(target['policy'])
                if (row['project_id'] not in json.loads(target['project_ids'])
                        or any(value not in policy['resources'] for value in resources)):
                    _fail('managed_ticket_assignee_outside_scope',403)
            # Ordinary row changes increment version; also pin the remaining
            # source fields and selected profile/project versions for exact ACK
            # replay. No historical receipt becomes fresh authority.
            context=dict(ticket={key:row[key] for key in row.keys() if key not in ('resource','version','updated_at')},
                manager_id=manager['id'],manager_version=manager['version'],project_version=project['version'],
                target=None if target is None else dict(id=target['id'],version=target['version']))
            context_sha=hashlib.sha256(_json(context).encode('utf-8')).hexdigest()
            previous=self._retry(db,actor,'managed_ticket_resource',idempotency_key,payload)
            if previous is not None:
                saved=json.loads(previous)
                if (row['version']!=expected_version+1 or row['resource']!=new_resource
                        or saved!=dict(ticket_id=ticket_id,context_sha256=context_sha)):
                    _fail('managed_ticket_retry_conflict',409)
                return self._ticket(row,db)
            if row['version']!=expected_version or row['resource']!=expected_resource:
                _fail('managed_ticket_source_conflict',409)
            now=time.time()
            changed=db.execute('''UPDATE tickets SET resource=?,version=version+1,updated_at=?
                WHERE id=? AND version=? AND resource=?''',(new_resource,now,ticket_id,expected_version,expected_resource))
            if changed.rowcount!=1:_fail('managed_ticket_source_conflict',409)
            db.execute('UPDATE projects SET updated_at=? WHERE id=?',(now,row['project_id']))
            self._remember(db,actor,'managed_ticket_resource',idempotency_key,payload,
                _json(dict(ticket_id=ticket_id,context_sha256=context_sha)))
            self._audit(db,actor,'managed_ticket_resource_retargeted',dict(payload,
                manager_profile_id=manager['id'],target_profile_id=row['assignee_agent_id'],version=expected_version+1))
            return self._ticket(self._ticket_member(db,actor,ticket_id),db)

    def set_agent_profile_ticket_resource_management(self,actor,profile_id,expected_version,enabled):
        """Human-only opt-in, separate from profile and stale-claim management."""
        _version(expected_version)
        if type(enabled) is not bool:_fail('enabled must be a boolean')
        with self._tx() as db:
            actor=self._human(db,actor);row=self._profile_owned(db,actor,profile_id)
            if row['version']!=expected_version:_fail('Agent profile changed; reload before granting ticket management',409)
            policy=json.loads(row['policy']);owner=json.loads(self._owner(db,actor['owner_id'])['policy'])
            resource=management_resource(actor['owner_id'])
            if enabled:
                if (row['legacy'] or json.loads(row['project_ids']) is None
                        or any(owner.get(key) is not True for key in ('enabled','allow_read','allow_requests'))
                        or not policy['allow_read'] or not policy['allow_requests'] or policy['autonomy']=='responses'
                        or any('agent.profile.read' not in value['actions'] or resource not in value['resources']
                            for value in (policy,owner))):
                    _fail('Ticket management needs an explicit scoped profile-management grant',403)
                policy['actions']=list(dict.fromkeys([*policy['actions'],TICKET_RESOURCE_ACTION]))
                owner['actions']=list(dict.fromkeys([*owner['actions'],TICKET_RESOURCE_ACTION]))
                if len(policy['actions'])>100 or len(owner['actions'])>100:_fail('Management grants exceed the action/resource limit')
                owner.update(granted_by=actor['owner_id'],granted_at=time.time())
                db.execute('UPDATE owners SET policy=? WHERE id=?',(_json(owner),actor['owner_id']))
            else:policy['actions']=[value for value in policy['actions'] if value!=TICKET_RESOURCE_ACTION]
            self._write_profile(db,profile_id,{'policy':policy})
            self._audit(db,actor,'agent_ticket_resource_management_granted' if enabled else 'agent_ticket_resource_management_revoked',
                dict(profile_id=profile_id,version=expected_version+1,action=TICKET_RESOURCE_ACTION,resource=resource))
            return self._profile_view(db,self._profile_owned(db,actor,profile_id))

    def set_agent_profile_ticket_creation_management(self,actor,profile_id,expected_version,enabled):
        """Human-only opt-in; profile management never implies ticket assignment."""
        _version(expected_version)
        if type(enabled) is not bool:_fail('enabled must be a boolean')
        with self._tx() as db:
            actor=self._human(db,actor);row=self._profile_owned(db,actor,profile_id)
            if row['version']!=expected_version:_fail('Agent profile changed; reload before granting assigned ticket creation',409)
            if row['kind']!='agent' or row['legacy'] or (enabled and row['status']=='archived'):
                _fail('Assigned ticket creation needs an ordinary scoped agent profile',403)
            policy=json.loads(row['policy']);owner=json.loads(self._owner(db,actor['owner_id'])['policy'])
            resource=management_resource(actor['owner_id'])
            if enabled:
                if (json.loads(row['project_ids']) is None
                        or any(owner.get(key) is not True for key in ('enabled','allow_read','allow_requests'))
                        or not policy['allow_read'] or not policy['allow_requests'] or policy['autonomy']=='responses'
                        or any('agent.profile.read' not in value['actions'] or resource not in value['resources']
                            for value in (policy,owner))):
                    _fail('Assigned ticket creation needs an explicit scoped profile-management grant',403)
                policy['actions']=list(dict.fromkeys([*policy['actions'],TICKET_CREATION_ACTION]))
                owner['actions']=list(dict.fromkeys([*owner['actions'],TICKET_CREATION_ACTION]))
                if len(policy['actions'])>100 or len(owner['actions'])>100:_fail('Management grants exceed the action/resource limit')
                owner.update(granted_by=actor['owner_id'],granted_at=time.time())
                db.execute('UPDATE owners SET policy=? WHERE id=?',(_json(owner),actor['owner_id']))
            else:policy['actions']=[value for value in policy['actions'] if value!=TICKET_CREATION_ACTION]
            self._write_profile(db,profile_id,{'policy':policy})
            self._audit(db,actor,'agent_ticket_creation_management_granted' if enabled else 'agent_ticket_creation_management_revoked',
                dict(profile_id=profile_id,version=expected_version+1,action=TICKET_CREATION_ACTION,resource=resource))
            return self._profile_view(db,self._profile_owned(db,actor,profile_id))

    def set_agent_profile_management(self,actor,profile_id,expected_version,enabled):
        """Human-only grant/revoke; never binds a device or changes identity."""
        _version(expected_version)
        if type(enabled) is not bool:_fail('enabled must be a boolean')
        with self._tx() as db:
            actor=self._human(db,actor);row=self._profile_owned(db,actor,profile_id)
            if row['version']!=expected_version:_fail('Agent profile changed; reload before granting management',409)
            policy=json.loads(row['policy']);owner=json.loads(self._owner(db,actor['owner_id'])['policy'])
            resource=management_resource(actor['owner_id'])
            if enabled:
                if (row['legacy'] or json.loads(row['project_ids']) is None
                        or any(owner.get(key) is not True for key in ('enabled','allow_read','allow_requests'))
                        or not policy['allow_read'] or not policy['allow_requests'] or policy['autonomy']=='responses'):
                    _fail('Management needs an explicit scoped profile and enabled read/work permissions',403)
                for key,added in (('actions',MANAGEMENT_ACTIONS),('resources',[resource])):
                    policy[key]=list(dict.fromkeys([*policy[key],*added]));owner[key]=list(dict.fromkeys([*owner[key],*added]))
                    if len(policy[key])>100 or len(owner[key])>100:_fail('Management grants exceed the action/resource limit')
                owner.update(granted_by=actor['owner_id'],granted_at=time.time())
                db.execute('UPDATE owners SET policy=? WHERE id=?',(_json(owner),actor['owner_id']))
            else:
                policy['actions']=[value for value in policy['actions'] if not value.startswith((MANAGEMENT_PREFIX,TICKET_MANAGEMENT_PREFIX))]
                policy['resources']=[value for value in policy['resources'] if not value.startswith(RESOURCE_PREFIX)]
            self._write_profile(db,profile_id,{'policy':policy})
            self._audit(db,actor,'agent_profile_management_granted' if enabled else 'agent_profile_management_revoked',
                        dict(profile_id=profile_id,version=expected_version+1,actions=list(MANAGEMENT_ACTIONS),resource=resource))
            return self._profile_view(db,self._profile_owned(db,actor,profile_id))
