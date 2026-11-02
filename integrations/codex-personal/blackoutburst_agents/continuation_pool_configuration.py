"""Inert opt-in selection for a future once-per-cycle cloud controller.

These files are consistency pins, never Board permission or a live permit.
Loading/planning reads only the three operator documents. It neither reads
connection credentials nor opens a task journal, registers, or executes work.
The concrete controller must recheck these pins and all live authority.
"""
from copy import deepcopy
import os
from pathlib import Path
from urllib.parse import urlsplit

from .cloud_workflow import load_workflow
from .dev_journal import decode
from .dev_manifest import DevelopmentError,sha256
from .development_pool_selection import file_document
from .finite_pool_batch import child,load_batch,FLEXIBLE_SCHEMA,QUEUE_SCHEMA
from .linux_input_snapshot import _path
from .pool_journal import PoolError,identifier,uuid_value

SCHEMA='linux-cloud-continuation-v1'
QUEUE_CONTINUATION_SCHEMA='linux-cloud-queue-continuation-v1'
MAX_CONFIGURATION=128*1024


def require(value,code='continuation_pool_configuration_invalid'):
    if not value:raise DevelopmentError(code)


def load_queue_continuation(path,*,root,owner_id,board_endpoint,max_seconds):
    """Local standing selection only; no client, credentials, registration or grant."""
    root=Path(_path(os.fspath(root)));path=child(path,root/'config')
    raw,pin=file_document(path,MAX_CONFIGURATION);value=decode(raw)
    if type(value) is not dict or value.get('schema')!=QUEUE_CONTINUATION_SCHEMA:return None
    require(set(value)=={'schema','catalog_file','workflow_file','approved_sha256',
        'checkpoint_after_actions','provider_backoff_seconds'})
    sha256(value['approved_sha256'])
    require(type(max_seconds) is int and 1<=max_seconds<=7200)
    from .development_queue import queue_tick_plan
    plan=queue_tick_plan(root,value['catalog_file'],value['workflow_file'],
        checkpoint_after_actions=value['checkpoint_after_actions'],provider_backoff_seconds=value['provider_backoff_seconds'])
    catalog=plan['catalog']
    require(catalog['owner_id']==owner_id and catalog['board_endpoint']==board_endpoint
        and catalog['timeout_seconds']<=max_seconds and plan['approval_sha256']==value['approved_sha256'],
        'continuation_pool_configuration_changed')
    pins={'configuration':pin,**{key:plan['binding'][key] for key in ('catalog','workflow')}}
    require(len({p['path'] for p in pins.values()})==3)
    for observed in pins.values():
        require(file_document(observed['path'],MAX_CONFIGURATION)[1]==observed,'continuation_pool_configuration_changed')
    return {'configuration':value,'plan':plan},pins


def cycle_limits(job):
    """Copy exact approved per-generation caps; no logical lifetime budget."""
    planner,broker=job['planner_budgets'],job['broker_budgets']
    value={**{key:planner[key] for key in ('max_turns','max_actions','max_wall_seconds')},
        **{key:broker[key] for key in ('max_operations','max_tests','max_read_bytes','max_patch_bytes')}}
    # The logical-cycle core requires positive bounds. Do not turn a zero
    # legacy budget into an implicit new allowance in this opt-in mode.
    require(all(type(item) is int and 1<=item<=2**31 for item in value.values()),
        'continuation_pool_cycle_limits_invalid')
    return value


def load_continuation(path,*,root,owner_id,board_endpoint,max_seconds):
    """Return selected operator data and pins, with no execution authority."""
    try:
        root=Path(_path(os.fspath(root)))
        require(type(max_seconds) is int and 1<=max_seconds<=7200)
        endpoint=urlsplit(board_endpoint)
        require(endpoint.scheme=='https' and endpoint.hostname and not endpoint.username
            and not endpoint.password and not endpoint.query and not endpoint.fragment)
        path=child(path,root/'config')
        raw,pin=file_document(path,MAX_CONFIGURATION);value=decode(raw)
        require(type(value) is dict and set(value)=={'schema','batch_file','workflow_file',
            'concurrency','provider_backoff_seconds','tasks'} and value['schema']==SCHEMA)
        batch_path=child(value['batch_file'],root/'config')
        workflow_path=child(value['workflow_file'],root/'config')
        require(len({path,batch_path,workflow_path})==3)
        batch,batch_pin=load_batch(batch_path,root=root,owner_id=owner_id,
            board_endpoint=board_endpoint,max_seconds=max_seconds)
        require(batch['schema'] in (FLEXIBLE_SCHEMA,QUEUE_SCHEMA),'continuation_pool_explicit_batch_required')
        workflow,workflow_pin=load_workflow(workflow_path,root=root,batch=batch)
        tasks=value['tasks'];jobs={job['name']:job for job in batch['jobs']}
        require(type(tasks) is list and len(tasks)==len(jobs))
        require(type(value['concurrency']) is int and 1<=value['concurrency']<=min(8,len(tasks)))
        require(type(value['provider_backoff_seconds']) is int and 30<=value['provider_backoff_seconds']<=3600)
        names=set();task_ids=set();limits={}
        for task in tasks:
            require(type(task) is dict and set(task)=={'name','logical_task_id','checkpoint_after_actions'})
            identifier(task['name']);uuid_value(task['logical_task_id'])
            require(task['name'] in jobs and task['name'] not in names
                and task['logical_task_id'] not in task_ids)
            names.add(task['name']);task_ids.add(task['logical_task_id'])
            limits[task['name']]=cycle_limits(jobs[task['name']])
            cap=limits[task['name']]
            # A complete workflow needs a named test followed by its final action.
            require(type(task['checkpoint_after_actions']) is int
                and 2<=task['checkpoint_after_actions']<=min(cap['max_actions'],cap['max_turns']),
                'continuation_pool_cycle_limits_invalid')
            require(cap['max_wall_seconds']<=batch['timeout_seconds'],
                'continuation_pool_cycle_exceeds_batch_timeout')
        require(names==set(jobs))
        # Detect changes to a document already read while another was parsed.
        pins={'configuration':pin,'batch':batch_pin,'workflow':workflow_pin}
        for expected in pins.values():
            require(file_document(expected['path'],MAX_CONFIGURATION)[1]==expected,
                'continuation_pool_configuration_changed')
        return deepcopy({'configuration':value,'batch':batch,'workflow':workflow,
            'cycle_limits':limits}),deepcopy(pins)
    except (TypeError,ValueError,KeyError,OverflowError,PoolError):
        raise DevelopmentError('continuation_pool_configuration_invalid') from None


def continuation_plan(path,*,root,owner_id,board_endpoint,max_seconds):
    """Closed, non-activating projection; never expose instructions or secrets."""
    standing=load_queue_continuation(path,root=root,owner_id=owner_id,
        board_endpoint=board_endpoint,max_seconds=max_seconds)
    if standing is not None:
        loaded,pins=standing;plan=loaded['plan'];catalog=plan['catalog']
        return {'schema':QUEUE_CONTINUATION_SCHEMA,'status':'plan','configuration_sha256':pins['configuration']['sha256'],
            'approval_sha256':plan['approval_sha256'],'catalog_sha256':pins['catalog']['sha256'],
            'workflow_sha256':pins['workflow']['sha256'],'agent_id':catalog['agent_id'],
            'project_id':catalog['project_id'],'entries':[e['entry_id'] for e in catalog['entries']],
            'activated':False,'execution_qualified':False,'launchable':False}
    loaded,pins=load_continuation(path,root=root,owner_id=owner_id,
        board_endpoint=board_endpoint,max_seconds=max_seconds)
    config=loaded['configuration'];jobs={job['name']:job for job in loaded['batch']['jobs']}
    reviews={job['name']:job['reviewer'] for job in loaded['workflow']['jobs']}
    tasks=[]
    for task in config['tasks']:
        job=jobs[task['name']];review=reviews[task['name']]
        item={**deepcopy(task),**{key:job[key] for key in
            ('agent_id','project_id','ticket_id','resource','model')},
            'original_public_base':job['repository']['base_commit'],
            'paths':deepcopy(job['paths']),'cycle_limits':deepcopy(loaded['cycle_limits'][task['name']]),
            'reviewer_agent_id':review['agent_id'],'reviewer_model':review['model']}
        if 'reasoning_effort' in job:item['reasoning_effort']=job['reasoning_effort']
        tasks.append(item)
    return {'schema':SCHEMA,'status':'plan','configuration_sha256':pins['configuration']['sha256'],
        'batch_configuration_sha256':pins['batch']['sha256'],
        'workflow_configuration_sha256':pins['workflow']['sha256'],
        'concurrency':config['concurrency'],'provider_backoff_seconds':config['provider_backoff_seconds'],
        'tasks':tasks,'activated':False,'execution_qualified':False,'launchable':False}
