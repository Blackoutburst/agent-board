"""Small MCP views; browser/REST and explicit full views retain original text.

The 6 KB data budget reserves room for MCP's text + structured copies.
Omitted prose is never returned under its original field name. Full text is
available through authorized, hash-checked code-point chunks.
"""
import hashlib
import json
import secrets

from projects import _fail

BUDGET = 6000
PROSE = frozenset({'body', 'description', 'context', 'environment',
    'acceptance_criteria', 'progress', 'outcome', 'evidence', 'question', 'answer',
    'instructions', 'requirements', 'message_preview'})


def encoded(value):
    # ASCII escaping also budgets controls, non-BMP characters and the text copy.
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()


def size(value):
    return len(encoded(value))


def wire(value):
    # Explicit compact serialization avoids FastMCP's pretty-printed text copy.
    # ASCII JSON can at most double when quoted as text. With the structured
    # copy, 6 KiB of data therefore fits below 20 KiB even for all-escape input.
    from mcp.types import CallToolResult, TextContent
    if size(value)>BUDGET:
        _fail('MCP data budget exceeded; request a smaller page or explicit full view',413)
    result=CallToolResult(content=[TextContent(type='text',text=encoded(value).decode())],structuredContent=value)
    if size(result.model_dump(mode='json',exclude_none=True))>=20000:
        _fail('MCP wire budget exceeded; request a smaller page',413)
    return result


def full_list(value):
    """Preserve the legacy structured list wrapper for explicit full clients."""
    from mcp.types import CallToolResult, TextContent
    return CallToolResult(content=[TextContent(type='text',text=encoded(value).decode())],
                          structuredContent={'result':value})


def text_info(value):
    raw = value.encode('utf-8')
    return {'chars': len(value), 'utf8_bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest()}


def compact(value):
    if isinstance(value, list):
        return [compact(item) for item in value]
    if not isinstance(value, dict):
        return value
    result, prose = {}, {}
    for key, item in value.items():
        if key=='answer' and isinstance(item,str) and len(encoded(item))<=512:
            result[key]=item
        elif isinstance(item, str) and (key in PROSE or len(encoded(item)) > 1024):
            if item:
                prose[key] = text_info(item)
                result[key + '_preview'] = item[:80]
        else:
            result[key] = compact(item)
    if prose:
        result['text_fields'] = prose
        result['text_detail_tool'] = 'get_text'
    return result


def summary(value):
    if 'number' in value and 'thread_id' in value:
        keep = {'id','project_id','number','thread_id','title','status','priority',
            'version','assignee_owner_id','assignee_agent_id','claimant',
            'lease_expires_at','claim_expired','ready','work_state','waiting_on_human'}
        return {**compact({k:v for k,v in value.items() if k in keep}),
                'detail_tool': 'get_ticket'}
    if 'question' in value:
        keep = {'id','ticket_id','owner_id','kind','status','created_at','resolved_at'}
        return {**compact({k:v for k,v in value.items() if k in keep|{'question','answer'}}),
                'detail_tool': 'get_text'}
    return compact(value)


def bounded(value):
    result = compact(value)
    if size(result) > BUDGET:
        # A successful write must not become an oversized/lost acknowledgement.
        # Keep its IDs, optimistic versions and claim/control state; explicitly
        # label absent collections instead of returning a misleading empty list.
        def acknowledgement(item):
            if not isinstance(item,dict):
                return item
            small,omitted={},[]
            for key,child in item.items():
                if isinstance(child,list) or key in PROSE or key=='text_fields' or key.endswith('_preview'):
                    omitted.append(key)
                elif isinstance(child,dict):
                    small[key]=acknowledgement(child)
                elif size(child)>1024:
                    omitted.append(key)
                else:
                    small[key]=child
            if omitted:
                small['omitted_fields']=omitted
                small['details']='Explicit view=full or authorized get_text'
            return small
        result=acknowledgement(value)
        if size(result)>BUDGET:
            _fail('Acknowledgement metadata exceeds budget; reconcile through explicit full detail',413)
    return result


class MCPViews:
    def __init__(self, board):
        self.board = board

    def _scope(self, kind, filters):
        return hashlib.sha256(encoded([kind, filters])).hexdigest()

    def thread(self, actor, thread_id, before=None, limit=50):
        limit = self.board._limit(limit)
        with self.board._connection() as db:
            actor = self.board._actor(db,actor,'allow_read')
            thread = self.board._member(db,actor,thread_id)
            boundary = 9223372036854775807
            if before:
                marker = db.execute('SELECT seq FROM messages WHERE id=? AND thread_id=?',(before,thread_id)).fetchone()
                if marker is None:
                    _fail('Invalid message pagination marker')
                boundary = marker['seq']
            rows = list(db.execute('SELECT * FROM messages WHERE thread_id=? AND seq<? ORDER BY seq DESC LIMIT ?',
                                  (thread_id,boundary,limit+1)))
            result = {'thread':compact(self.board._thread(db,thread,actor['owner_id'])),
                      'messages':[],'tasks':[],'next_before':None}
            selected = []
            for row in rows[:limit]:
                item = compact(self.board._message(db,row))
                result['messages'].append(item)
                if size(result)>BUDGET-400:
                    result['messages'].pop()
                    break
                selected.append(row)
            if rows and not selected:
                _fail('One thread summary exceeds budget; use an explicit full read',413)
            result['messages'].reverse()
            if len(selected)<len(rows):
                result['next_before'] = selected[-1]['id']
            tasks = list(db.execute('SELECT id,title,status FROM tasks WHERE thread_id=? ORDER BY created_at,id',(thread_id,)))
            # Legacy work remains explicitly retrievable through the full view;
            # never pass truncated instructions under source field names.
            result['legacy_task_count'] = len(tasks)
            for task in tasks:
                result['tasks'].append(dict(task))
                if size(result)>BUDGET-160:
                    result['tasks'].pop()
                    break
            result['legacy_tasks_detail'] = 'list_thread_tasks'
            result['legacy_tasks_omitted'] = len(tasks)-len(result['tasks'])
            return result

    def _position(self, db, actor, scope, cursor):
        if not cursor:
            return {}
        if not isinstance(cursor, str) or len(cursor) > 100:
            _fail('Invalid MCP page cursor')
        row = db.execute('SELECT position FROM mcp_page_cursors WHERE token_hash=? AND reader=? AND scope=?',
            (hashlib.sha256(cursor.encode()).hexdigest(), self.board._actor_key(actor), scope)).fetchone()
        if row is None:
            _fail('MCP cursor was not issued for this session and query')
        return json.loads(row['position'])

    def page(self, actor, kind, value, *, limit=20, cursor=None, filters=None):
        limit = self.board._limit(limit)
        is_list = isinstance(value, list)
        groups = {'items': value} if is_list else {k:v for k,v in value.items() if isinstance(v,list)}
        fixed = {} if is_list else {k:v for k,v in value.items() if not isinstance(v,list)}
        scope = self._scope(kind, filters)
        with self.board._tx() as db:
            actor = self.board._actor(db, actor, 'allow_read')
            position = self._position(db, actor, scope, cursor)
            # Immutable IDs, rather than updated_at, prevent updates from moving
            # already observed rows across a page boundary. Authorization is
            # re-evaluated by the source read on every page.
            def key(item):
                value = item.get('id',item.get('name',''))
                return f'n:{value:020d}' if type(value) is int else 's:'+value
            reverse = kind=='list_activity' and (filters or {}).get('order')=='desc'
            def beyond(group,item):
                old=position.get(group)
                return old is None or (key(item)<old if reverse else key(item)>old)
            rows = {k:sorted(v, key=key, reverse=reverse) for k,v in groups.items()}
            remaining = {k:[x for x in v if beyond(k,x)] for k,v in rows.items()}
            result = {**compact(fixed), **{k:[] for k in groups},
                      'totals': {k:len(v) for k,v in rows.items()}, 'next_cursor':None}
            # Reserve cursor and envelope space before adding items. Round robin
            # means large ready queues cannot hide an assigned claim or request.
            blocked = set()
            for index in range(limit):
                advanced = False
                for group, candidates in remaining.items():
                    if group in blocked or index >= len(candidates):
                        continue
                    item = summary(candidates[index])
                    result[group].append(item)
                    if size(result) > BUDGET-160:
                        result[group].pop()
                        if not any(result[k] for k in groups):
                            _fail('One summary exceeds the MCP budget; request its explicit full detail',413)
                        blocked.add(group)
                        continue
                    position[group] = key(candidates[index])
                    advanced = True
                if not advanced:
                    break
            more = any(any(beyond(k,x) for x in v) for k,v in rows.items())
            if more:
                token = secrets.token_urlsafe(24)
                db.execute('INSERT INTO mcp_page_cursors VALUES(?,?,?,?)',
                    (hashlib.sha256(token.encode()).hexdigest(),self.board._actor_key(actor),scope,json.dumps(position)))
                result['next_cursor'] = token
            return result

    def get_text(self, actor, kind, identifier, field, offset=0, limit=2048, expected_sha256=''):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 64<=limit<=4096:
            _fail('Use a nonnegative code-point offset and 64..4096 encoded bytes')
        with self.board._connection() as db:
            actor = self.board._actor(db,actor,'allow_read')
            if kind == 'message':
                row = db.execute('SELECT * FROM messages WHERE id=?',(identifier,)).fetchone()
                if row is None:
                    _fail('Message not found',404)
                self.board._member(db,actor,row['thread_id'])
                allowed = {'body'}
            elif kind == 'ticket':
                row = self.board._ticket_member(db,actor,identifier)
                allowed = {'title','description','acceptance_criteria','progress','outcome','evidence','resource'}
            elif kind == 'project':
                row = self.board._project_member(db,actor,identifier)
                allowed = {'name','description','context','environment','repository_url'}
            elif kind == 'human_request':
                row = db.execute('SELECT * FROM human_requests WHERE id=?',(identifier,)).fetchone()
                if row is None:
                    _fail('Human request not found',404)
                self.board._ticket_member(db,actor,row['ticket_id'])
                allowed = {'question','answer'}
            elif kind == 'legacy_task':
                row = db.execute('SELECT * FROM tasks WHERE id=?',(identifier,)).fetchone()
                if row is None:
                    _fail('Task not found',404)
                self.board._member(db,actor,row['thread_id'])
                allowed = {'title','instructions','requirements','progress','outcome','evidence'}
            elif kind == 'profile':
                profile = self.board._profile_for_actor(db,actor)
                if profile is None or profile['id'] != identifier:
                    _fail('Profile not found',404)
                row = json.loads(profile['fields'])
                allowed = {'name','description','instructions','role','response_style','language','preferred_model','preferred_provider'}
            else:
                _fail('Unsupported text kind')
            if field not in allowed:
                _fail('Unsupported text field')
            text = row[field] or ''
            info = text_info(text)
            if expected_sha256 and expected_sha256 != info['sha256']:
                _fail('Text changed; restart at offset 0 with its new hash',409)
            if offset>len(text):
                _fail('Offset exceeds text length')
            lo,hi = offset,len(text)
            while lo<hi:
                middle=(lo+hi+1)//2
                if len(encoded(text[offset:middle]))<=limit:
                    lo=middle
                else:
                    hi=middle-1
            return {'kind':kind,'id':identifier,'field':field,'text':text[offset:lo],
                    'offset':offset,'next_offset':lo if lo<len(text) else None,**info}

    def tasks(self, actor, thread_id, limit=20, cursor=None):
        with self.board._connection() as db:
            actor=self.board._actor(db,actor,'allow_read')
            self.board._member(db,actor,thread_id)
            tasks=[self.board._task(db,row) for row in db.execute('SELECT * FROM tasks WHERE thread_id=?',(thread_id,))]
        return self.page(actor,'list_thread_tasks',tasks,limit=limit,cursor=cursor,filters={'thread_id':thread_id})
