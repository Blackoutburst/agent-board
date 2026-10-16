"""Human-selected response subscriptions; schema1 ticket activity is unchanged."""
import json
import os
from pathlib import Path
from .board_reader import _connection_file, _endpoint, _text
from .host_config import HostError, digest, connection_digest, MAX_BYTES
from .message_journal import identifier


def validate(config,*,bind_connections=False):
    try:
        required={'schema_version','owner_id','board_endpoint','interval_seconds','max_pages','page_limit','dispatch_budget','targets'}
        if not isinstance(config,dict) or set(config)!=required or type(config['schema_version']) is not int or config['schema_version']!=2:raise ValueError
        result=json.loads(json.dumps(config,allow_nan=False))
        if len(json.dumps(result).encode())>MAX_BYTES:raise ValueError
        identifier(result['owner_id']);_endpoint(result['board_endpoint'])
        for key,low,high in [('interval_seconds',5,300),('max_pages',1,4),('page_limit',1,200),('dispatch_budget',1,20)]:
            if type(result[key]) is not int or not low<=result[key]<=high:raise ValueError
        if not isinstance(result['targets'],list) or not 1<=len(result['targets'])<=16:raise ValueError
        ids,agents,sessions=set(),set(),set()
        for target in result['targets']:
            fields={'id','agent_id','board_session_id','profile_version','connection_file','instruction','workspace','executable','model','timeout','thread_ids','start_at'}
            if not isinstance(target,dict) or not fields<=set(target) or set(target)-fields-{'connection_sha256','instruction_sha256'}:raise ValueError
            for key in ('id','agent_id','board_session_id'):identifier(target[key])
            if target['id'] in ids or target['agent_id'] in agents or target['board_session_id'] in sessions:raise ValueError
            ids.add(target['id']);agents.add(target['agent_id']);sessions.add(target['board_session_id'])
            if type(target['profile_version']) is not int or target['profile_version']<1:raise ValueError
            if target['start_at'] not in ('beginning','now'):raise ValueError
            if (not isinstance(target['thread_ids'],list) or not 1<=len(target['thread_ids'])<=32
                    or len(set(target['thread_ids']))!=len(target['thread_ids'])):raise ValueError
            for thread in target['thread_ids']:identifier(thread)
            if (not isinstance(target['instruction'],str) or not target['instruction'].strip() or '\0' in target['instruction']
                    or len(target['instruction'].encode())>12000 or not _text(target['model'],200)
                    or type(target['timeout']) not in (int,float) or not 1<=target['timeout']<=300):raise ValueError
            for key in ('workspace','executable','connection_file'):
                path=Path(target[key]).expanduser()
                if not path.is_absolute() or path.is_symlink():raise ValueError
                if bind_connections:path=path.resolve(strict=True)
                target[key]=str(path)
            if bind_connections:
                if not Path(target['workspace']).is_dir() or not Path(target['executable']).is_file() or not os.access(target['executable'],os.X_OK):raise ValueError
                connection=_connection_file(target['connection_file'])
                if connection['mcp_url']!=result['board_endpoint']:raise ValueError
                target['connection_sha256']=connection_digest(target['connection_file'])
            if (not isinstance(target.get('connection_sha256'),str) or len(target['connection_sha256'])!=64
                    or any(c not in '0123456789abcdef' for c in target['connection_sha256'])):raise ValueError
            expected=digest(target['instruction'])
            if not bind_connections and target.get('instruction_sha256')!=expected:raise ValueError
            target['instruction_sha256']=expected
        return result
    except HostError:raise
    except Exception:raise HostError('invalid_host_configuration') from None
