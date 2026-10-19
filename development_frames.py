"""Fixed, bounded observations for one development family authorization window.

Normal Board methods and their per-method permissions remain in use. Calls are
sequential, not one transaction or a durable grant. No controller/CLI import.
"""
from copy import deepcopy
import json
import re

from board import BoardError

SCHEMA = 'blackoutburst-development-frame-v1'
MAX_ARGUMENT_BYTES = 2048
MAX_DATA_BYTES = 384 * 1024
MAX_WIRE_BYTES = 1536 * 1024
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$')


def _fail(code='unavailable'):
    raise BoardError('development_frame_' + code, 413 if code == 'too_large' else 400) from None


def encoded(value):
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8', 'strict')
        return json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail()


def _identifier(value):
    if type(value) is not str or not _ID.fullmatch(value):
        _fail('invalid_arguments')
    try:
        value.encode('utf-8', 'strict')
    except UnicodeError:
        _fail('invalid_arguments')
    return value


def validate_arguments(values):
    if (type(values) is not dict or set(values) != {'session_id', 'ticket_id', 'family_id', 'terminal'}
            or type(values['terminal']) is not bool):
        _fail('invalid_arguments')
    for key in ('session_id', 'ticket_id', 'family_id'):
        _identifier(values[key])
    if len(encoded(values)) > MAX_ARGUMENT_BYTES:
        _fail('invalid_arguments')
    return deepcopy(values)


def _bounded(value):
    if len(encoded(value)) > MAX_DATA_BYTES:
        _fail('too_large')
    return value


def _selection(board, fresh_actor, args):
    actor = fresh_actor(args['session_id'])
    with board._connection() as db:
        actor = board._actor(db, actor, 'allow_read')
        if actor['kind'] != 'agent':
            _fail()
        family = board._family_row(db, args['family_id'])
        profile = board._profile_for_actor(db, actor)
        session = db.execute('SELECT * FROM sessions WHERE id=?', (actor['session_id'],)).fetchone()
        if (any(actor.get(key) != family[key] for key in ('owner_id', 'device_id'))
                or profile['id'] != family['agent_id'] or profile['version'] != family['profile_version']
                or session['profile_version'] != family['profile_version']):
            _fail()
        board._project_member(db, actor, family['project_id'])
        ticket = board._ticket_member(db, actor, args['ticket_id'])
        if ticket['project_id'] != family['project_id']:
            _fail()
        if ticket['id'] == family['parent_ticket_id']:
            if actor['session_id'] != family['session_id']:
                _fail()
        elif not db.execute('SELECT 1 FROM delegation_members WHERE family_id=? AND ticket_id=? AND session_id=?',
                            (family['id'], ticket['id'], actor['session_id'])).fetchone():
            _fail()
        parent_id = family['session_id']
        expected = {key: family[key] for key in ('owner_id', 'agent_id', 'device_id', 'profile_version')}
        project_id, parent_ticket_id = family['project_id'], family['parent_ticket_id']
    # A parent session name in a family is not authentication by itself.
    parent = fresh_actor(parent_id)
    with board._connection() as db:
        parent = board._actor(db, parent, 'allow_read')
        profile = board._profile_for_actor(db, parent)
        session = db.execute('SELECT * FROM sessions WHERE id=?', (parent_id,)).fetchone()
        if (any(parent.get(key) != expected[key] for key in ('owner_id', 'device_id'))
                or profile['id'] != expected['agent_id'] or profile['version'] != expected['profile_version']
                or any(session[key] != expected[key] for key in expected)):
            _fail()
        board._ticket_member(db, parent, parent_ticket_id)
    return {**expected, 'project_id': project_id, 'ticket_id': args['ticket_id'],
            'session_id': args['session_id'], 'family_id': args['family_id'],
            'parent_ticket_id': parent_ticket_id, 'parent_session_id': parent_id,
            'terminal': args['terminal']}


def _history(read, role, frame, session_id):
    selected = frame['selection']
    ticket_id = selected['ticket_id'] if role == 'selected' else selected['parent_ticket_id']
    thread_id = frame[role]['first']['ticket']['thread_id']
    by_id, thread, before = {}, None, None
    for _ in range(3):
        limit = min(20, max(1, 40 - len(by_id)))
        arguments = {'thread_id': thread_id, 'limit': limit}
        if before is not None:
            arguments['before'] = before
        response = read('get_thread', session_id, **arguments)
        current, page, cursor = response['thread'], response['messages'], response['next_before']
        if (type(current) is not dict or current.get('id') != thread_id
                or current.get('ticket', {}).get('id') != ticket_id
                or current['ticket'].get('project_id') != selected['project_id']
                or selected['owner_id'] not in current['participants']
                or type(page) is not list or len(page) > limit):
            _fail('invalid_pagination')
        if thread is not None and encoded(thread) != encoded(current):
            _fail('invalid_pagination')
        thread, older = current, 0
        for message in page:
            key = _identifier(message['id'])
            if message.get('thread_id') != thread_id:
                _fail('invalid_pagination')
            if key in by_id:
                if encoded(by_id[key]) != encoded(message) or before is not None and key != before:
                    _fail('invalid_pagination')
            else:
                by_id[key] = message
                older += 1
        if cursor is not None:
            _identifier(cursor)
            if not page or cursor != page[0]['id']:
                _fail('invalid_pagination')
        frame[role]['pages'].append({'arguments': arguments, 'response': response})
        _bounded(frame)
        if cursor is None:
            return
        if cursor == before or not older:
            _fail('invalid_pagination')
        if len(by_id) >= 40:
            return
        before = cursor


def get_development_frame(board, fresh_actor, **values):
    """One fixed read algorithm. fresh_actor is server-owned, never an RPC arg."""
    args = validate_arguments(values)
    try:
        selection = _selection(board, fresh_actor, args)
        frame = {'schema': SCHEMA, 'selection': selection,
                 'selected': {'pages': []}, 'parent': {'pages': []}, 'family': {}}
        selected, parent = selection['session_id'], selection['parent_session_id']
        def read(method, session_id, **arguments):
            # Exactly the normal actor_session + Board method boundaries.
            result = getattr(board, method)(fresh_actor(session_id), **arguments)
            return deepcopy(_bounded(result))
        def put(role, field, method, session_id, **arguments):
            frame[role][field] = read(method, session_id, **arguments)
            _bounded(frame)
        for role, session in [('selected', selected), ('parent', parent)]:
            put(role, 'policy_first', 'get_work', session, project_id=selection['project_id'], limit=1)
        put('selected', 'first', 'get_ticket', selected, ticket_id=selection['ticket_id'])
        put('parent', 'first', 'get_ticket', parent, ticket_id=selection['parent_ticket_id'])
        put('family', 'first', 'get_delegation_family', parent, family_id=selection['family_id'])
        _history(read, 'selected', frame, selected)
        _history(read, 'parent', frame, parent)
        put('selected', 'last', 'get_ticket', selected, ticket_id=selection['ticket_id'])
        put('parent', 'last', 'get_ticket', parent, ticket_id=selection['parent_ticket_id'])
        put('family', 'last', 'get_delegation_family', parent, family_id=selection['family_id'])
        for role, session in [('selected', selected), ('parent', parent)]:
            put(role, 'policy_last', 'get_work', session, project_id=selection['project_id'], limit=1)
        if _selection(board, fresh_actor, args) != selection:
            _fail()
        return _bounded(frame)
    except BoardError as error:
        if str(error) in {'development_frame_too_large', 'development_frame_invalid_pagination'}:
            raise
        _fail()
    except Exception:
        # Storage/SDK failures are not a diagnostic or source-text channel.
        _fail()


def wire_frame(value):
    """Bound both copies of the real MCP result, including escaping overhead."""
    from mcp.types import CallToolResult, TextContent
    _bounded(value)
    result = CallToolResult(content=[TextContent(type='text', text=encoded(value).decode('ascii'))],
                            structuredContent=value)
    if len(encoded(result.model_dump(mode='json', exclude_none=True))) + 1024 > MAX_WIRE_BYTES:
        _fail('too_large')
    return result
