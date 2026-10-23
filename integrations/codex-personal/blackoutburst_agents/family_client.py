"""Narrow explicit family operations on the already paired Board connection."""
import copy
import math
import re

from .board_reader import BoardReadError,_text
from .development_client import DevelopmentBoardClient
from .dev_manifest import canonical

_NAMES={'open_delegation_family','get_delegation_family','lookup_delegation_family',
        'stop_delegation_family','accept_delegation_result','finalize_delegation_family'}
_SHA=re.compile(r'[0-9a-f]{64}\Z')


def _body(value,maximum):
    if (type(value) is not str or not value.strip() or len(value)>maximum
            or any(ord(char)<32 and char not in '\n\r\t' or ord(char)==127 for char in value)):
        return False
    try:return len(value.encode('utf-8','strict'))<=maximum*4
    except UnicodeError:return False


def _arguments(name,args):
    fields={
        'open_delegation_family':{'parent_ticket_id','expected_version','children','limits','actions','resource','manifest_sha256','idempotency_key'},
        'get_delegation_family':{'family_id'},
        'lookup_delegation_family':{'parent_ticket_id','idempotency_key'},
        'stop_delegation_family':{'family_id','expected_revision'},
        'accept_delegation_result':{'family_id','expected_revision','child_ticket_id','expected_child_version','result_sha256','verification_evidence','idempotency_key'},
        'finalize_delegation_family':{'family_id','expected_revision','snapshot_sha256','verification_evidence','idempotency_key'}}
    if name not in fields or type(args) is not dict or set(args)!=fields[name]:raise BoardReadError('invalid_arguments')
    for key,value in args.items():
        if key.startswith('expected_'):
            if type(value) is not int or value<1:raise BoardReadError('invalid_arguments')
        elif key.endswith('_sha256'):
            if type(value) is not str or not _SHA.fullmatch(value):raise BoardReadError('invalid_arguments')
        elif key=='verification_evidence':
            if not _body(value,20000):raise BoardReadError('invalid_arguments')
        elif key not in ('children','limits','actions'):
            if not _text(value,200):raise BoardReadError('invalid_arguments')
    if name=='open_delegation_family':
        children=args['children'];limits=args['limits']
        if (type(children) is not list or len(children)!=2 or type(limits) is not dict
                or set(limits)!={'max_model_turns','max_tool_actions','max_concurrent','deadline'}
                or type(args['actions']) is not list or not args['actions']
                or len(set(args['actions']))!=len(args['actions'])
                or any(a not in ('repo.read_file','repo.patch_file','repo.run_test') for a in args['actions'])):
            raise BoardReadError('invalid_arguments')
        for key,low,high in [('max_model_turns',2,64),('max_tool_actions',0,128),('max_concurrent',1,1)]:
            if type(limits[key]) is not int or not low<=limits[key]<=high:raise BoardReadError('invalid_arguments')
        if type(limits['deadline']) not in (int,float) or not math.isfinite(limits['deadline']) or limits['deadline']<=0:
            raise BoardReadError('invalid_arguments')
        for child in children:
            if type(child) is not dict or set(child)!={'session_id','role','model','title','description','acceptance_criteria','files'}:
                raise BoardReadError('invalid_arguments')
            for key,value in child.items():
                if key=='files':
                    if type(value) is not list or not 1<=len(value)<=64 or any(not _text(path,200) for path in value):
                        raise BoardReadError('invalid_arguments')
                elif key in ('description','acceptance_criteria'):
                    if not _body(value,20000):raise BoardReadError('invalid_arguments')
                elif not _text(value,200):raise BoardReadError('invalid_arguments')
        if (len({c['session_id'] for c in children})!=2 or len({c['role'] for c in children})!=2):
            raise BoardReadError('invalid_arguments')
    if len(canonical(args))>128*1024:raise BoardReadError('invalid_arguments')
    return copy.deepcopy(args)


class FamilyBoardClient(DevelopmentBoardClient):
    # Family configuration selects its existing protocol independently.
    development_ticket_frame = False
    def require_development_frame(self):
        """Explicit preparation probe; never fallback or refresh registration."""
        if not self._registered:raise BoardReadError('development_registration_required')
        self._initialize()
        result=self._rpc('tools/list',{})
        try:
            tools=result['tools']
            if type(tools) is not list or len(tools)>256:raise ValueError
            matches=[tool for tool in tools if type(tool) is dict and tool.get('name')=='get_development_frame']
            if len(matches)!=1:raise ValueError
            schema=matches[0]['inputSchema']
            if (type(schema) is not dict or set(schema['properties'])!={'session_id','ticket_id','family_id','terminal'}
                    or set(schema['required'])!=set(schema['properties'])
                    or schema.get('additionalProperties') is not False
                    or schema['properties']['terminal'].get('type')!='boolean'):raise ValueError
        except (KeyError,TypeError,ValueError):
            raise BoardReadError('development_frame_unavailable') from None

    def get_development_frame(self, *, ticket_id, family_id, terminal):
        if (not self._registered or any(type(value) is not str or not re.fullmatch(
                    r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}',value) for value in (self.session_id,ticket_id,family_id))
                or type(terminal) is not bool):raise BoardReadError('invalid_arguments')
        self._initialize()
        # Dedicated method; no arbitrary names, parent sessions or batch args.
        return self._tool('get_development_frame',dict(session_id=self.session_id,
            ticket_id=ticket_id,family_id=family_id,terminal=terminal))

    def call(self,name,args):
        if name not in _NAMES:return super().call(name,args)
        if not self._registered:raise BoardReadError('development_registration_required')
        values=_arguments(name,args)
        self._initialize()
        return self._tool(name,{**values,'session_id':self.session_id})
