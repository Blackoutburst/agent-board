"""Closed metadata protocol. It cannot carry a command, environment or prompt."""
import json
import math

from .host_config import HostError
from .managed_host import _opaque,_sha

TOOLS=frozenset({'publish_server_catalog','poll_instance_commands','ack_instance_command','report_instance_state'})
STATES=frozenset({'unknown','stopped','starting','ready','busy','stopping','needs_review'})
REASONS=frozenset({'','stopped','not_started','launch_refused','configuration_changed','authorization_changed',
    'host_revoked','cleanup_unconfirmed','connection_lost','budget_exhausted','execution_failed',
    'authentication_required','quota_unavailable'})

def _int(value,low=0,high=2**63-1):
    if type(value) is not int or not low<=value<=high:raise HostError('managed_protocol_invalid')

def _bool(value):
    if type(value) is not bool:raise HostError('managed_protocol_invalid')

def _bounded(value,maximum):
    try:
        if len(json.dumps(value,ensure_ascii=True,allow_nan=False,separators=(',',':')).encode('ascii'))>maximum:
            raise ValueError()
    except (ValueError,TypeError,RecursionError,UnicodeError):raise HostError('managed_protocol_invalid') from None

def _exact(value,keys):
    if type(value) is not dict or set(value)!=set(keys.split()):raise HostError('managed_protocol_invalid')

def _time(value):
    if type(value) not in (int,float) or not 0<=value<=2**63-1:raise HostError('managed_protocol_invalid')

def entry(value):
    _exact(value,'catalog_key profile_id profile_version launchspec_sha256 mode')
    _opaque(value['catalog_key']);_opaque(value['profile_id']);_int(value['profile_version'],1);_sha(value['launchspec_sha256'])
    if value['mode'] not in ('messages','ticket_text'):raise HostError('managed_protocol_invalid')

def command(value):
    _exact(value,'id seq instance_id action generation launchspec_sha256 budget instance_version revision status created_at updated_at stop_confirmed')
    for k in ('id','instance_id'):_opaque(value[k])
    for k in ('seq','instance_version','revision'):_int(value[k],1)
    _int(value['generation']);_sha(value['launchspec_sha256']);_bool(value['stop_confirmed'])
    if value['action'] not in ('start','stop','restart') or value['status'] not in ('queued','accepted','complete','superseded','rejected'):
        raise HostError('managed_protocol_invalid')
    _int(value['budget'],0 if value['action']=='stop' else 1,0 if value['action']=='stop' else 20)
    for k in ('created_at','updated_at'):_time(value[k])

def instance(value):
    _exact(value,'id host_id catalog_key profile_id profile_version launchspec_sha256 mode version generation desired_state observed_state observation_revision reason last_seen_at command_id active cleanup_confirmed can_start control_reason current_command')
    for k in ('id','host_id','catalog_key','profile_id'):_opaque(value[k])
    for k in ('profile_version','version'):_int(value[k],1)
    for k in ('generation','observation_revision'):_int(value[k])
    for k in ('active','cleanup_confirmed','can_start'):_bool(value[k])
    _sha(value['launchspec_sha256'])
    if (value['mode'] not in ('messages','ticket_text') or value['desired_state'] not in ('running','stopped')
            or value['observed_state'] not in STATES or value['reason'] not in REASONS or value['control_reason'] not in REASONS):
        raise HostError('managed_protocol_invalid')
    if value['last_seen_at'] is not None:_time(value['last_seen_at'])
    if value['command_id'] is not None:_opaque(value['command_id'])
    c=value['current_command']
    if c is None:
        if value['command_id'] is not None:raise HostError('managed_protocol_invalid')
    else:
        command(c)
        if c['id']!=value['command_id'] or c['instance_id']!=value['id'] or c['generation']!=value['generation']:
            raise HostError('managed_protocol_invalid')

def _arguments(name,args):
    if name not in TOOLS:raise HostError('managed_tool_forbidden')
    _bounded(args,16384 if name=='publish_server_catalog' else 4096)
    keys={
        'publish_server_catalog':'host_id expected_catalog_version entries idempotency_key',
        'poll_instance_commands':'host_id limit',
        'ack_instance_command':'host_id command_id expected_revision idempotency_key',
        'report_instance_state':'host_id instance_id expected_observation_revision generation launchspec_sha256 command_id state cleanup_confirmed reason idempotency_key'}
    _exact(args,keys[name]);_opaque(args['host_id'])
    if 'idempotency_key' in args:_opaque(args['idempotency_key'])
    if name=='poll_instance_commands':_int(args['limit'],1,20)
    elif name=='publish_server_catalog':
        _int(args['expected_catalog_version']);entries=args['entries']
        if type(entries) is not list or len(entries)>16:raise HostError('managed_protocol_invalid')
        for e in entries:entry(e)
        if len({e['catalog_key'] for e in entries})!=len(entries) or len({e['profile_id'] for e in entries})!=len(entries):
            raise HostError('managed_protocol_invalid')
    elif name=='ack_instance_command':_opaque(args['command_id']);_int(args['expected_revision'],1)
    else:
        _opaque(args['instance_id']);_int(args['expected_observation_revision']);_int(args['generation']);_sha(args['launchspec_sha256'])
        if args['command_id'] is not None:_opaque(args['command_id'])
        if args['state'] not in STATES or args['reason'] not in REASONS:raise HostError('managed_protocol_invalid')
        _bool(args['cleanup_confirmed'])
        if args['cleanup_confirmed'] and args['state'] not in ('stopped','needs_review'):raise HostError('managed_protocol_invalid')

def _response(name,value):
    _bounded(value,65536)
    if name=='poll_instance_commands':
        _exact(value,'host_id host_version enabled last_seen_at catalog_version instances commands')
        _opaque(value['host_id']);_int(value['host_version'],1);_bool(value['enabled']);_time(value['last_seen_at']);_int(value['catalog_version'])
        if type(value['commands']) is not list or len(value['commands'])>20:raise HostError('managed_protocol_invalid')
        for c in value['commands']:command(c)
        if len({c['id'] for c in value['commands']})!=len(value['commands']):raise HostError('managed_protocol_invalid')
    elif name=='publish_server_catalog':
        _exact(value,'host_id catalog_version instances');_opaque(value['host_id']);_int(value['catalog_version'],1)
    elif name in ('ack_instance_command','report_instance_state'):
        _exact(value,'command instance');instance(value['instance'])
        if value['command'] is not None:command(value['command'])
        if value['command']!=value['instance']['current_command']:raise HostError('managed_protocol_invalid')
        return value
    else:raise HostError('managed_tool_forbidden')
    if type(value['instances']) is not list or len(value['instances'])>16:raise HostError('managed_protocol_invalid')
    for i in value['instances']:
        instance(i)
        if i['host_id']!=value['host_id']:raise HostError('managed_protocol_invalid')
    if len({i['id'] for i in value['instances']})!=len(value['instances']) or len({i['catalog_key'] for i in value['instances']})!=len(value['instances']):
        raise HostError('managed_protocol_invalid')
    if name=='poll_instance_commands':
        current={i['id']:i['current_command'] for i in value['instances']}
        if any(c!=current.get(c['instance_id']) or c['status'] not in ('queued','accepted') for c in value['commands']):
            raise HostError('managed_protocol_invalid')
    return value

def arguments(name,args):
    try:return _arguments(name,args)
    except HostError:raise
    except (TypeError,ValueError,KeyError,RecursionError,UnicodeError):
        raise HostError('managed_protocol_invalid') from None

def response(name,value):
    try:return _response(name,value)
    except HostError:raise
    except (TypeError,ValueError,KeyError,RecursionError,UnicodeError):
        raise HostError('managed_protocol_invalid') from None
