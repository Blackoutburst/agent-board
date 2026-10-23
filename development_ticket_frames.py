"""Bounded sequential reads for one ordinary development ticket.

This is an observation envelope, not an atomic snapshot or an authority grant.
The client still checks the two policies, ticket versions and source hashes.
No registration, lease, claim, queue sweep or execution happens here.
"""
from copy import deepcopy

from board import BoardError
from development_frames import (encoded, _identifier, _source,
    _THREAD_SOURCE, _MESSAGE_SOURCE, MAX_ARGUMENT_BYTES, MAX_DATA_BYTES,
    MAX_WIRE_BYTES)

SCHEMA = 'blackoutburst-development-ticket-frame-v1'


def _fail(code='unavailable'):
    raise BoardError('development_ticket_frame_' + code,
                     413 if code == 'too_large' else 400) from None


def validate_arguments(values):
    if type(values) is not dict or set(values) != {'session_id', 'ticket_id'}:
        _fail('invalid_arguments')
    try:
        for value in values.values():
            _identifier(value)
        if len(encoded(values)) > MAX_ARGUMENT_BYTES:
            _fail('invalid_arguments')
    except BoardError:
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
        profile = board._profile_for_actor(db, actor)
        session = db.execute('SELECT * FROM sessions WHERE id=?',
                             (actor['session_id'],)).fetchone()
        ticket = board._ticket_member(db, actor, args['ticket_id'])
        board._project_member(db, actor, ticket['project_id'])
        # Families have their own existing frame and parent/child authority.
        if board._delegation_ticket_family(db, ticket['id']) is not None:
            _fail()
        if (session['agent_id'] != profile['id']
                or session['profile_version'] != profile['version']):
            _fail()
        return {'owner_id': actor['owner_id'], 'agent_id': profile['id'],
                'device_id': actor['device_id'], 'profile_version': profile['version'],
                'project_id': ticket['project_id'], 'ticket_id': ticket['id'],
                'session_id': actor['session_id']}


def _policy(board, fresh_actor, args):
    # Same effective policy as get_work, without enumerating or sweeping work.
    actor = fresh_actor(args['session_id'])
    with board._connection() as db:
        actor = board._actor(db, actor, 'allow_read')
        ticket = board._ticket_member(db, actor, args['ticket_id'])
        board._project_member(db, actor, ticket['project_id'])
        return {'policy': board._effective_profile_policy(
            db, actor, board._profile_for_actor(db, actor))}


def _history(read, frame):
    selected = frame['selection']
    thread_id = frame['selected']['first']['ticket']['thread_id']
    by_id, thread, before = {}, None, None
    for _ in range(3):
        limit = min(20, max(1, 40 - len(by_id)))
        arguments = {'thread_id': thread_id, 'limit': limit}
        if before is not None:
            arguments['before'] = before
        response = read('get_thread', **arguments)
        current, page, cursor = response['thread'], response['messages'], response['next_before']
        if (type(current) is not dict or current.get('id') != thread_id
                or current.get('ticket', {}).get('id') != selected['ticket_id']
                or current['ticket'].get('project_id') != selected['project_id']
                or selected['owner_id'] not in current['participants']
                or type(page) is not list or len(page) > limit):
            _fail('invalid_pagination')
        current_source = _source(current, _THREAD_SOURCE)
        if thread is not None and encoded(thread) != encoded(current_source):
            _fail('invalid_pagination')
        thread, older = current_source, 0
        for message in page:
            key = _identifier(message['id'])
            if message.get('thread_id') != thread_id:
                _fail('invalid_pagination')
            source = _source(message, _MESSAGE_SOURCE)
            if key in by_id:
                if encoded(by_id[key]) != encoded(source) or before is not None and key != before:
                    _fail('invalid_pagination')
            else:
                by_id[key] = source
                older += 1
        if cursor is not None:
            _identifier(cursor)
            if not page or cursor != page[0]['id']:
                _fail('invalid_pagination')
        frame['selected']['pages'].append({'arguments': arguments, 'response': response})
        _bounded(frame)
        if cursor is None:
            return
        if cursor == before or not older:
            _fail('invalid_pagination')
        if len(by_id) >= 40:
            return
        before = cursor


def get_development_ticket_frame(board, fresh_actor, **values):
    """fresh_actor is supplied by the authenticated server, never by RPC data."""
    args = validate_arguments(values)
    try:
        selection = _selection(board, fresh_actor, args)
        frame = {'schema': SCHEMA, 'selection': selection, 'selected': {'pages': []}}
        def read(method, **arguments):
            if method == 'get_thread':
                arguments = {**arguments, '_expire': False}
            value = getattr(board, method)(fresh_actor(args['session_id']), **arguments)
            return deepcopy(_bounded(value))
        def put(field, value):
            frame['selected'][field] = deepcopy(value)
            _bounded(frame)
        put('policy_first', _policy(board, fresh_actor, args))
        put('first', read('get_ticket', ticket_id=args['ticket_id']))
        _history(read, frame)
        put('last', read('get_ticket', ticket_id=args['ticket_id']))
        put('policy_last', _policy(board, fresh_actor, args))
        if _selection(board, fresh_actor, args) != selection:
            _fail()
        return _bounded(frame)
    except BoardError as error:
        if str(error) in {'development_ticket_frame_too_large',
                         'development_ticket_frame_invalid_pagination'}:
            raise
        _fail()
    except Exception:
        _fail()


def wire_ticket_frame(value):
    from mcp.types import CallToolResult, TextContent
    _bounded(value)
    result = CallToolResult(content=[TextContent(type='text', text=encoded(value).decode('ascii'))],
                            structuredContent=value)
    if len(encoded(result.model_dump(mode='json', exclude_none=True))) + 1024 > MAX_WIRE_BYTES:
        _fail('too_large')
    return result
