"""Explicit human-delegated management of ordinary, bounded agent profiles.

Existing action/resource intersections carry this grant. A role label, legacy
wildcard, device capability or management tool argument never grants authority.
There is no credential, device binding, Runtime or server-management API here.
"""
import json
import secrets
import time

from profiles import DEFAULT_POLICY,_insert_profile
from projects import _fail,_json,_version

MANAGEMENT_ACTIONS=('agent.profile.read','agent.profile.create','agent.profile.update','agent.profile.archive')
MANAGEMENT_PREFIX='agent.profile.'
RESOURCE_PREFIX='agent-profiles:'
AUTONOMY={'responses':0,'requested':1,'queue':2}
PROFILE_MANAGEMENT_TOOLS=frozenset({'list_managed_agent_profiles','get_managed_agent_profile',
    'create_managed_agent_profile','update_managed_agent_profile','archive_managed_agent_profile'})


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
    else:_fail('Unknown management tool')
    if type(args) is not dict or set(args)-allowed or not required<=set(args):_fail('Supply only management tool fields')
    for key in ('session_id','profile_id','idempotency_key'):
        if key in args:_identity(args[key])
    if 'view' in args and (type(args['view']) is not str or args['view'] not in ('full','compact')):_fail('Invalid management view')
    if 'expected_version' in args:_version(args['expected_version'])
    for key in ('profile','changes'):
        if key in args and type(args[key]) is not dict:_fail('Profile and changes must be JSON objects')
    if 'include_archived' in args and type(args['include_archived']) is not bool:_fail('include_archived must be boolean')
    if 'limit' in args and (type(args['limit']) is not int or not 1<=args['limit']<=100):_fail('Invalid management page size')
    if args.get('cursor') is not None:_identity(args['cursor'])


def management_resource(owner_id):return RESOURCE_PREFIX+owner_id


def _reserved_action(value):
    return '*' in value or value.startswith((MANAGEMENT_PREFIX,'runtime.','server.','provider.','device.','credential.','policy.'))


def _reserved_resource(value):
    return '*' in value or value.startswith((RESOURCE_PREFIX,'runtime:','server:','provider:','device:','credential:','policy:'))


def _is_management(row):
    policy=json.loads(row['policy'])
    return (any(action.startswith(MANAGEMENT_PREFIX) for action in policy['actions'])
            or any(resource.startswith(RESOURCE_PREFIX) for resource in policy['resources']))


class ProfileManagementMixin:
    def _profile_manager(self,db,actor,action,*,read=False):
        actor=self._actor(db,actor,'allow_read' if read else 'allow_requests')
        if actor['kind']!='agent':_fail('Use the human agent profile editor',403)
        profile=self._profile_for_actor(db,actor);stored=json.loads(profile['policy'])
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
        if row['id']==manager['id'] or row['legacy'] or _is_management(row):
            _fail('Management identities and policies remain human-owned',403)
        self._managed_bounds(db,actor,manager,ceiling,json.loads(row['policy']),json.loads(row['project_ids']))
        return row

    @staticmethod
    def _managed_view(row):
        # Deliberately no bindings, device identities, connection keys or active
        # claims from an unrelated/revoked project are returned to this channel.
        return {**{key:row[key] for key in ('id','owner_id','status','version','created_at','updated_at')},
                **json.loads(row['fields']),'project_ids':json.loads(row['project_ids']),'policy':json.loads(row['policy'])}

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
                policy['actions']=[value for value in policy['actions'] if not value.startswith(MANAGEMENT_PREFIX)]
                policy['resources']=[value for value in policy['resources'] if not value.startswith(RESOURCE_PREFIX)]
            self._write_profile(db,profile_id,{'policy':policy})
            self._audit(db,actor,'agent_profile_management_granted' if enabled else 'agent_profile_management_revoked',
                        dict(profile_id=profile_id,version=expected_version+1,actions=list(MANAGEMENT_ACTIONS),resource=resource))
            return self._profile_view(db,self._profile_owned(db,actor,profile_id))
