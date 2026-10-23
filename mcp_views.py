"""Small MCP views; browser/REST and explicit full views retain original text.

The 6 KB data budget reserves room for MCP's text + structured copies.
Omitted prose is never returned under its original field name. Full text is
available through authorized, hash-checked code-point chunks.
"""
import hashlib
import json
import secrets
import time

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


def _source(value):
    if not isinstance(value, dict) or 'id' not in value:
        return None
    if 'number' in value and 'thread_id' in value: kind = 'ticket'
    elif 'body' in value and 'thread_id' in value: kind = 'message'
    elif 'question' in value and 'ticket_id' in value: kind = 'human_request'
    elif 'instructions' in value and 'destination_kind' in value: kind = 'legacy_task'
    elif 'filename' in value and 'content_type' in value: kind = 'attachment'
    elif 'detail' in value and 'ticket_id' in value: kind = 'activity'
    elif 'participants' in value and 'title' in value: kind = 'thread'
    elif 'context' in value and 'name' in value: kind = 'project'
    elif 'label' in value and 'last_seen_at' in value: kind = 'session'
    elif 'instructions' in value and 'policy' in value: kind = 'profile'
    elif {'name','avatar','status'} <= value.keys(): kind = 'agent'
    else: return None
    return kind, str(value['id'])


def _preview(value):
    end = min(len(value), 80)
    while len(encoded(value[:end])) > 128:
        end -= 1
    return value[:end]


def compact(value, source=None, path=()):
    if isinstance(value, list):
        return [compact(item, source, (*path, str(i))) for i,item in enumerate(value)]
    if not isinstance(value, dict):
        return value
    own_source = _source(value)
    if own_source:
        source, path = own_source, ()
    result, prose = {}, {}
    for key, item in value.items():
        if key=='answer' and isinstance(item,str) and len(encoded(item))<=512:
            result[key]=item
        elif isinstance(item, str) and (key in PROSE or len(encoded(item)) > 1024
                or (source and source[0] == 'human_request' and key in ('summary', 'command', 'label'))):
            info = text_info(item)
            if source:
                field = '/'+ '/'.join(x.replace('~','~0').replace('/','~1') for x in (*path,key))
                info['get_text'] = {'kind':source[0],'identifier':source[1],'field':field}
            else:
                info['retrieval'] = 'Repeat this tool with view=full'
            prose[key] = info
            result[key + '_preview'] = _preview(item)
        else:
            result[key] = compact(item, source, (*path,key))
    if prose:
        result['text_fields'] = prose
        result['text_detail_tool'] = 'get_text' if source else 'view=full'
    # Large metadata collections use exact canonical chunks instead of making
    # an inbox or an owner's work queue permanently unreadable.
    keys = ('attachments','mentions','mention_targets') if own_source and own_source[0]=='message' else ()
    if source and source[0]=='session': keys += ('skills','capabilities')
    if source and source[0] in ('policy','profile'): keys += ('actions','resources')
    for key in keys:
        items = result.get(key)
        if isinstance(items,list) and (len(items)>2 or size(items)>600):
            result.pop(key)
            field = '/'+ '/'.join(x.replace('~','~0').replace('/','~1') for x in (*path,key))
            result[key+'_count'] = len(items)
            result[key+'_detail'] = {'tool':'get_text','kind':source[0],
                'identifier':source[1],'field':field,
                'encoding':'canonical JSON array text'}
    return result


def summary(value):
    if not isinstance(value,dict):
        return compact(value)
    if 'number' in value and 'thread_id' in value:
        keep = {'id','project_id','number','thread_id','title','status','priority',
            'version','assignee_owner_id','assignee_agent_id','claimant',
            'lease_expires_at','claim_expired','ready','work_state','waiting_on_human',
            'assignee_presence','assignment_stale'}
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

    def message(self, actor, thread_id, message_id):
        if any(type(x) is not str or not 1<=len(x)<=200 for x in (thread_id,message_id)):
            _fail('Message not found',404)
        with self.board._connection() as db:
            actor=self.board._actor(db,actor,'allow_read');self.board._member(db,actor,thread_id)
            row=db.execute('SELECT * FROM messages WHERE id=? AND thread_id=?',(message_id,thread_id)).fetchone()
            if row is None:_fail('Message not found',404)
            return {'message':compact(self.board._message(db,row)),
                    'source_sha256':self.board._message_source_hash(row)}

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
        db.execute('DELETE FROM mcp_page_cursors WHERE expires_at<=?',(time.time(),))
        if not cursor:
            return {}
        if not isinstance(cursor, str) or len(cursor) > 100:
            _fail('Invalid MCP page cursor')
        row = db.execute('SELECT position FROM mcp_page_cursors WHERE token_hash=? AND reader=? AND scope=?',
            (hashlib.sha256(cursor.encode()).hexdigest(), self.board._actor_key(actor), scope)).fetchone()
        if row is None:
            _fail('MCP cursor expired or was not issued for this session and query; restart scan')
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
            rows = groups
            if kind=='list_activity':
                high = position.get('high', max((x['id'] for x in value),default=0))
                position['high']=high
                rows={'items':[x for x in value if x['id']<=high]}
            # Preserve the authoritative queue/recency/name order. If mutable
            # ordering/membership changes, fail explicitly instead of skipping
            # unseen rows. No ID snapshot or ever-growing seen-ID list is kept.
            identities = {k:[x.get('id',x.get('name',x)) if isinstance(x,dict) else x for x in v] for k,v in rows.items()}
            digest=hashlib.sha256(encoded(identities)).hexdigest()
            if position.get('order_hash',digest)!=digest:
                _fail('Collection order or membership changed; restart scan and deduplicate IDs',409)
            position['order_hash']=digest
            offsets=position.setdefault('offsets',{})
            if position.get('next_group') in rows:
                keys=list(rows);start=keys.index(position['next_group']);rows={k:rows[k] for k in keys[start:]+keys[:start]}
            fixed = compact(fixed)
            if kind=='get_work' and 'policy' in value:
                fixed['policy']=compact(value['policy'],('policy',actor['owner_id']))
            result = {**fixed, **{k:[] for k in rows},
                      'totals': {k:len(v) for k,v in rows.items()}, 'next_cursor':None}
            blocked=set()
            # limit is a total item bound, including get_work groups.
            for _ in range(limit):
                advanced=False
                for group,candidates in rows.items():
                    index=offsets.get(group,0)
                    if group in blocked or index>=len(candidates):continue
                    result[group].append(summary(candidates[index]))
                    if size(result)>BUDGET-200:
                        result[group].pop();blocked.add(group);continue
                    offsets[group]=index+1;advanced=True
                    break
                if not advanced:break
                # Rotate groups so assigned work and human requests get turns.
                rows={k:rows[k] for k in [*list(rows)[1:],next(iter(rows))]}
                position['next_group']=next(iter(rows))
            more=any(offsets.get(k,0)<len(v) for k,v in rows.items())
            if more:
                if not any(result[k] for k in groups):
                    _fail('One summary or fixed metadata exceeds the MCP budget; request explicit full detail',413)
                token=secrets.token_urlsafe(24);now=time.time();reader=self.board._actor_key(actor)
                db.execute('INSERT INTO mcp_page_cursors VALUES(?,?,?,?,?,?)',
                    (hashlib.sha256(token.encode()).hexdigest(),reader,scope,json.dumps(position),now,now+3600))
                db.execute('''DELETE FROM mcp_page_cursors WHERE token_hash IN
                    (SELECT token_hash FROM mcp_page_cursors WHERE reader=? ORDER BY created_at DESC,token_hash DESC LIMIT -1 OFFSET 128)''',(reader,))
                result['next_cursor']=token
            return result

    def ticket(self, actor, ticket_id, limit=20, cursor=None):
        data=self.board.get_ticket(actor,ticket_id)
        ticket=dict(data['ticket'])
        # Project detail has its own read and text locator; do not spend the
        # ticket's control-list budget on an unrelated large project brief.
        project={k:data['project'][k] for k in ('id','name','version')}
        page=self.page(actor,'get_ticket',ticket,limit=limit,cursor=cursor,filters={'ticket_id':ticket_id})
        next_cursor=page.pop('next_cursor');totals=page.pop('totals')
        return {'ticket':page,'project':compact(project,('project',project['id'])),'totals':totals,'next_cursor':next_cursor}

    def get_text(self, actor, kind, identifier, field, offset=0, limit=2048, expected_sha256=''):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 64<=limit<=4096:
            _fail('Use a nonnegative code-point offset and 64..4096 encoded bytes')
        with self.board._connection() as db:
            actor = self.board._actor(db,actor,'allow_read')
            if kind == 'message':
                row=db.execute('SELECT * FROM messages WHERE id=?',(identifier,)).fetchone()
                if row is None:_fail('Message not found',404)
                self.board._member(db,actor,row['thread_id']);data=self.board._message(db,row)
            elif kind == 'ticket':
                row=self.board._ticket_member(db,actor,identifier);data=self.board._ticket(row,db)
                project=self.board._project_member(db,actor,row['project_id'])
                data['project_name']=project['name']
            elif kind == 'project':
                data=self.board._project(db,self.board._project_member(db,actor,identifier))
            elif kind == 'human_request':
                row=db.execute('SELECT * FROM human_requests WHERE id=?',(identifier,)).fetchone()
                if row is None:_fail('Human request not found',404)
                self.board._ticket_member(db,actor,row['ticket_id']);data=self.board._request(row,db)
            elif kind == 'legacy_task':
                row=db.execute('SELECT * FROM tasks WHERE id=?',(identifier,)).fetchone()
                if row is None:_fail('Task not found',404)
                self.board._member(db,actor,row['thread_id']);data=self.board._task(db,row)
            elif kind == 'profile':
                profile=self.board._profile_for_actor(db,actor)
                if profile is None or profile['id']!=identifier:_fail('Profile not found',404)
                data=self.board._profile_view(db,profile)
            elif kind == 'thread':
                row=self.board._member(db,actor,identifier);data=self.board._thread(db,row,actor['owner_id'])
            elif kind == 'attachment':
                row=db.execute('SELECT * FROM attachments WHERE id=?',(identifier,)).fetchone()
                if row is None:_fail('Attachment not found',404)
                self.board._member(db,actor,row['thread_id']);data=self.board._attachment(row)
            elif kind == 'policy':
                if identifier!=actor['owner_id']:_fail('Policy not found',404)
                data=(self.board._effective_profile_policy(db,actor,self.board._profile_for_actor(db,actor))
                      if actor['kind']=='agent' else json.loads(self.board._owner(db,actor['owner_id'])['policy']))
            elif kind == 'session':
                if identifier!=actor.get('session_id'):_fail('Session not found',404)
                data=self.board._session(db.execute('SELECT * FROM sessions WHERE id=?',(identifier,)).fetchone())
            elif kind == 'agent':
                data=next((x for x in self.board.list_agents(actor) if x['id']==identifier),None)
                if data is None:_fail('Agent not found',404)
            elif kind == 'activity':
                if not identifier.isdigit():_fail('Activity not found',404)
                rows=self.board.list_activity(actor,after=int(identifier)-1,limit=1,order='asc')
                if not rows or str(rows[0]['id'])!=identifier:_fail('Activity not found',404)
                data=rows[0]
            else:_fail('Unsupported text kind')
            parts=([x.replace('~1','/').replace('~0','~') for x in field[1:].split('/')]
                   if field.startswith('/') else [field])
            text=data
            for part in parts:
                if isinstance(text,dict) and part in text:text=text[part]
                elif isinstance(text,list) and part.isdigit() and int(part)<len(text):text=text[int(part)]
                else:_fail('Unsupported text field')
            # Large metadata arrays can be recovered as canonical JSON chunks,
            # without an unbounded get_message result or a false empty list.
            if isinstance(text,(dict,list)):text=encoded(text).decode()
            if not isinstance(text,str):_fail('Unsupported text field')
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
            tasks=[self.board._task(db,row) for row in db.execute('SELECT * FROM tasks WHERE thread_id=? ORDER BY created_at,id',(thread_id,))]
        return self.page(actor,'list_thread_tasks',tasks,limit=limit,cursor=cursor,filters={'thread_id':thread_id})
