"""Controller-authored finite plans; no model/transport/qualification operation."""
from copy import deepcopy
import math
from pathlib import Path
import unicodedata

from .context import ContextScope,_ticket_sources,build_context
from .development_authorization import DevelopmentAuthorization
from .dev_journal import PrivateDirectory
from .dev_manifest import DevelopmentError,canonical,check_content,digest,opaque,relative_path
from .dev_planner import _directory_pin
from .dev_planner_journal import PlannerBudgets
from .family_audit import inspect_files,manifest_document
from .family_client import _arguments
from .family_journal import SLOTS
from .work_journal import _scope,_encode
from .family_sources import controller_sources
from .development_frame import LEGACY, FRAME, read_protocol

_INPUTS={'broker_directory','backend_directory','planner_policy_directory','model','instruction','budgets'}
_CHILD={'session_id','role','title','description','acceptance_criteria','files','root'}


def root_sources(client,scope,manifest,registration,*,children=(),baseline=None,clock):
    """Root claim stays live while only known family dependency changes are allowed."""
    authority=DevelopmentAuthorization(client,scope=scope,manifest=manifest,registration=registration,clock=clock)
    authority._registration();policy=authority._policy()
    first=client.call('get_ticket',{'ticket_id':scope['ticket_id']})
    validate_root_start(authority, first, scope, manifest, children=children)
    context=build_context(client.call,scope=ContextScope(**{k:scope[k] for k in
        ('owner_id','agent_id','project_id','ticket_id')}),registration=registration)
    last=client.call('get_ticket',{'ticket_id':scope['ticket_id']})
    return validate_root_reads(authority, first, last, context, policy, scope, manifest, children=children, baseline=baseline)


def validate_root_start(authority, first, scope, manifest, *, children=()):
    project,ticket=first['project'],first['ticket']
    child_ids=set(children)
    if (project['id']!=scope['project_id'] or project['archived'] is not False
            or scope['owner_id'] not in project['participants'] or ticket['id']!=scope['ticket_id']
            or ticket['project_id']!=scope['project_id'] or ticket['waiting_on_human']
            or ticket['resource']!=manifest['resource']
            or ticket['assignee_owner_id'] not in (None,scope['owner_id'])
            or ticket['assignee_agent_id'] not in (None,scope['agent_id'])
            or set(ticket['depends_on'])!=child_ids
            or any(item['id'] not in child_ids for item in ticket['blocked_by'])
            or not authority.own_claim(ticket,remaining=10)):
        raise DevelopmentError('family_root_authorization_changed')
    if not children and ticket.get('delegation') is not None:
        raise DevelopmentError('family_root_already_delegated')


def validate_root_reads(authority, first, last, context, policy, scope, manifest, *, children=(), baseline=None):
    """Pure validation of one bounded root read window; not server atomicity."""
    validate_root_start(authority, first, scope, manifest, children=children)
    if last['ticket']['version']!=first['ticket']['version'] or not authority.own_claim(last['ticket'],remaining=10):
        raise DevelopmentError('family_root_source_changed')
    _,selected=_ticket_sources(last,ContextScope(**{k:scope[k] for k in
        ('owner_id','agent_id','project_id','ticket_id')}))
    stable={k:v for k,v in selected.items() if k not in
        ('version','depends_on','dependencies','blocked_by','assignee_agent_id','delegation')}
    # open may fill an initially unassigned root with this same profile only.
    stable['assignee_agent_id']=scope['agent_id']
    frame={'ticket_sha256':digest(canonical(stable)),
           'sources':{k:v for k,v in context['sources'].items() if k!='ticket'}}
    if baseline is not None and baseline!=frame:
        raise DevelopmentError('family_root_source_changed')
    return {'ticket':last['ticket'],'project':last['project'],'frame':frame,'policy':policy}


def build_configuration(request,workflows,registrations,*,clock):
    if (type(request) is not dict or set(request) not in ({'scope','parent','children','limits'},
                {'scope','parent','children','limits','read_protocol'})
            or type(request['children']) is not list or len(request['children'])!=2
            or set(workflows)!=set(SLOTS) or type(registrations) is not dict or set(registrations)!=set(SLOTS)):
        raise DevelopmentError('invalid_family_plan')
    _encode(registrations,256*1024,'invalid_family_registrations')
    request=deepcopy(request);scope=request['scope'];_scope(scope)
    protocol=read_protocol(request.get('read_protocol',LEGACY))
    parent=request['parent']
    if type(parent) is not dict or set(parent)!=_INPUTS:
        raise DevelopmentError('invalid_family_plan')
    manifest,manifest_hash=manifest_document(parent['broker_directory'])
    root=workflows['parent']
    root.client.adopt_registration()
    if protocol==FRAME:
        root.client.require_development_frame()
    initial=root_sources(root.client,scope,manifest,registrations['parent'],clock=clock)
    now=clock();limits=request['limits']
    if (type(limits) is not dict or set(limits)!={'max_model_turns','max_tool_actions','max_concurrent','deadline'}
            or type(now) not in (int,float) or not math.isfinite(now)
            or type(limits.get('deadline')) not in (int,float) or not math.isfinite(limits['deadline'])
            or not now+10<limits['deadline']<=min(now+86400,initial['ticket']['lease_expires_at']-10)):
        raise DevelopmentError('invalid_family_budget')
    limits['deadline']=float(limits['deadline'])
    inputs={'parent':parent};roots={'parent':_directory_pin(manifest['root'])};stores={};pins={};scopes={}
    public=[];allocated=set();sessions={scope['board_session_id']}
    for index,child in enumerate(request['children']):
        slot='child_'+str(index)
        if type(child) is not dict or set(child)!=_INPUTS|_CHILD:
            raise DevelopmentError('invalid_family_plan')
        if child['session_id'] in sessions:raise DevelopmentError('family_session_overlap')
        sessions.add(child['session_id']);inputs[slot]=child
        if type(child['files']) is not list or not 1<=len(child['files'])<=64:
            raise DevelopmentError('invalid_family_files')
        if any(type(path) is not str for path in child['files']):raise DevelopmentError('invalid_family_files')
        child['files']=sorted(child['files'])
        for path in child['files']:
            relative_path(path);normalized=unicodedata.normalize('NFC',path).casefold()
            if path not in manifest['files'] or normalized in allocated:
                raise DevelopmentError('family_file_caps_overlap')
            allocated.add(normalized)
        roots[slot]=_directory_pin(child['root'])
        public.append({key:deepcopy(child[key]) for key in
            ('session_id','role','model','title','description','acceptance_criteria','files')})
    if len({child['role'] for child in public})!=2:
        raise DevelopmentError('family_roles_ambiguous')
    sum_turns=sum_actions=sum_wall=0
    for slot,item in inputs.items():
        worker=workflows[slot]
        for key in ('broker_directory','backend_directory','planner_policy_directory'):
            path=Path(item[key])
            if not path.is_absolute() or '..' in path.parts:raise DevelopmentError('invalid_family_directory')
            with PrivateDirectory(path):pass
            item[key]=str(path)
        opaque(item['model'],'invalid_development_model',128)
        if type(item['instruction']) is not str or not item['instruction'].strip() or len(item['instruction'].encode('utf-8','surrogatepass'))>16384:
            raise DevelopmentError('invalid_planner_instruction')
        if slot=='parent' and len(item['instruction'].encode('utf-8'))>4096:
            raise DevelopmentError('family_parent_instruction_too_large')
        check_content(item['instruction'].encode('utf-8','strict'))
        item['budgets']=PlannerBudgets(**item['budgets']).document()
        sum_turns+=item['budgets']['max_turns'];sum_actions+=item['budgets']['max_actions']
        sum_wall+=item['budgets']['max_wall_seconds']
        selected={**scope,'board_session_id':scope['board_session_id'] if slot=='parent' else item['session_id']}
        expected=deepcopy(manifest);expected['scope']['session_id']=selected['board_session_id']
        worker.client.adopt_registration()
        DevelopmentAuthorization(worker.client,scope=selected,manifest=expected,
                                 registration=registrations[slot],clock=clock)._registration()
        scopes[slot]=selected
        stores[slot]={'runs':_directory_pin(worker.store.state_dir),'runtime':_directory_pin(worker.runtime.directory)}
        pins[slot]={'journal':_directory_pin(worker.journal.directory),
                    **{key:_directory_pin(item[key]) for key in ('broker_directory','backend_directory','planner_policy_directory')}}
    if (sum_turns>limits.get('max_model_turns',-1) or sum_actions>limits.get('max_tool_actions',-1)
            or sum_wall>limits['deadline']-now-10):
        raise DevelopmentError('family_budget_overallocated')
    opening={'parent_ticket_id':scope['ticket_id'],'expected_version':initial['ticket']['version'],
        'children':public,'limits':limits,'actions':manifest['actions'],'resource':manifest['resource'],
        'manifest_sha256':manifest_hash,'idempotency_key':'validation-only'}
    _arguments('open_delegation_family',opening)
    server_plan={'schema':1,'depth':1,'children':public,'limits':limits,'actions':sorted(manifest['actions']),
        'resource':manifest['resource'],'parent_manifest_sha256':manifest_hash}
    # Verify actual bytes, not just a caller-supplied base_sha declaration.
    allowed=lambda: root_sources(root.client,scope,manifest,registrations['parent'],baseline=initial['frame'],clock=clock) is not None
    for slot,pin in roots.items():
        template=deepcopy(manifest);template['root']=pin['path'];template['root_identity']=pin['identity']
        if slot!='parent':template['files']={path:manifest['files'][path] for path in inputs[slot]['files']}
        snapshot=inspect_files(template,authorize=allowed)
        if snapshot['files']!={path:entry['sha256'] for path,entry in template['files'].items()}:
            raise DevelopmentError('family_initial_files_changed')
    return {'schema':1,'read_protocol':protocol,'scope':scope,'request':request,'inputs':inputs,'roots':roots,'child_stores':stores,
        'pins':pins,'registrations':deepcopy(registrations),'provisional_scopes':scopes,
        'parent_manifest':manifest,'parent_manifest_sha256':manifest_hash,'root_frame':initial['frame'],
        'opening':opening,'server_plan':server_plan,'server_plan_sha256':digest(canonical(server_plan)),
        'max_audit_bytes':32*1024*1024,'controller_sources':controller_sources(roots)}
