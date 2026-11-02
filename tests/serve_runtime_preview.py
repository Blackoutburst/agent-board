"""Loopback-only runtime browser fixture; no real process or provider is used."""
import os
import tempfile
import time

os.environ['BOARD_DATA_DIR'] = tempfile.mkdtemp(prefix='board-runtime-ui-')
os.environ['BOARD_ALLOW_LOCAL_PASSWORDS'] = '1'
os.environ['BLACKOUT_SHARED_AUTH'] = '0'
os.environ['BOARD_BASE_URL'] = 'http://127.0.0.1:18767'

from app import create_app
from fastapi import HTTPException, Request
from usage_observations import connection_identifier
import uvicorn

app = create_app(os.environ['BOARD_DATA_DIR'], base_url=os.environ['BOARD_BASE_URL'],
                 filebrowser_url='', allow_local_passwords=True)
board = app.state.board
humans = {owner: {'kind': 'human', 'owner_id': owner} for owner in ('blackoutburst', 'thescreamer')}
for owner, human in humans.items():
    board.ensure_owner(owner, owner, 'local-ui-fixture-only')
    board.set_policy(human, True, True, True, True, [], [])
project = board.create_project(humans['blackoutburst'], 'Runtime browser fixture', list(humans),
                               idempotency_key='runtime-ui-project')
executors = {}
ids = {}
for owner, human in humans.items():
    device = board.create_device(human, 'Synthetic browser executor', [])
    raw = board.authenticate(device['token'])
    name = 'Runtime reviewer <img src=x onerror=alert(1)>' if owner == 'blackoutburst' else 'PRIVATE OTHER OWNER EXECUTOR'
    board.register_agent(raw, owner + '-runtime-ui', 'Runtime fixture', [], 'codex', False, 'runtime-ui', name)
    actor = board.actor_session(raw, owner + '-runtime-ui')
    board.update_runtime_limits(human, 1, 4, False)
    variants = ('running', 'reserved', 'needs_review', 'finished') if owner == 'blackoutburst' else ('other',)
    for variant in variants:
        ticket = board.create_ticket(human, project['id'], 'Synthetic ' + variant,
                                      idempotency_key=owner + '-' + variant)
        board.claim_ticket(actor, ticket['id'], ticket['version'])
        execution = board.reserve_execution(actor, 'selection-' + variant, project['id'], ticket['id'],
            'work-' + variant, 'turn-' + variant, 'codex-personal', 'fixture-model')['execution']
        if variant != 'reserved':
            execution = board.start_execution(actor, execution['id'], execution['revision'])
        if variant == 'needs_review':
            execution = board.finish_execution(actor, execution['id'], execution['revision'],
                'blocked', False, 'cleanup_unconfirmed')
        elif variant == 'finished':
            execution = board.finish_execution(actor, execution['id'], execution['revision'],
                'cancelled', True, 'cancelled')
        ids[variant] = execution['id']
        executors[execution['id']] = actor

actor = executors[ids['running']]
thread = board.create_thread(humans['blackoutburst'], 'Message reply fixture', ['blackoutburst'], 'message-runtime-thread')
with board._connection() as db:
    profile_id = board._profile_for_actor(db, actor)['id']
message = board.post_message(humans['blackoutburst'], thread['id'], 'Synthetic addressed message',
                             mentions=['agent:' + profile_id], idempotency_key='message-runtime-source')
digest = board.get_message(actor, thread['id'], message['id'])['source_sha256']
execution = board.reserve_message_execution(actor, 'message-ui', thread['id'], message['id'], digest,
                                             'message-ui-work', 'message-ui-turn', 'codex_chatgpt', 'fixture-model')['execution']
board.finish_execution(actor, execution['id'], execution['revision'], 'cancelled', True, 'cancelled')
ids.update(message=execution['id'], message_source=message['id'], message_thread=thread['id'])
executors[execution['id']] = actor
with board._tx() as db:
    db.execute('UPDATE runtime_executions SET last_heartbeat_at=? WHERE id=?', (time.time() - 120, ids['running']))
    # Deliberately malformed metadata exercises text rendering independently of
    # the server's normal model-name validation.
    db.execute('UPDATE runtime_executions SET model=? WHERE id=?',
               ('<svg onload=alert(2)>' + 'verylongmodel' * 15, ids['running']))


def fixture_request(request):
    if request.client.host not in ('127.0.0.1', '::1') or request.headers.get('X-Runtime-Fixture') != 'local-only':
        raise HTTPException(403)


@app.get('/__fixture__/ids')
async def fixture_ids(request: Request):
    fixture_request(request)
    return ids


@app.post('/__fixture__/finish/{execution_id}')
async def fixture_finish(execution_id: str, request: Request):
    fixture_request(request)
    actor = executors[execution_id]
    execution = board.get_execution_control(actor, execution_id)
    return board.finish_execution(actor, execution_id, execution['revision'], 'cancelled', True, 'cancelled')


@app.post('/__fixture__/block/{execution_id}')
async def fixture_block(execution_id: str, request: Request):
    fixture_request(request)
    actor = executors[execution_id]
    execution = board.get_execution_control(actor, execution_id)
    return board.finish_execution(actor, execution_id, execution['revision'], 'blocked', True, 'quota_or_rate_limit')


@app.post('/__fixture__/auth-block/{execution_id}')
async def fixture_auth_block(execution_id: str, request: Request):
    fixture_request(request)
    actor = executors[execution_id]
    execution = board.get_execution_control(actor, execution_id)
    # Confirming old cleanup is not another provider observation. Simulate a
    # genuinely new failed turn, after releasing this old uncertain process.
    board.finish_execution(actor, execution_id, execution['revision'], 'blocked', True, execution['reason'])
    human = humans[actor['owner_id']]
    ticket = board.create_ticket(human, project['id'], 'Synthetic new authentication failure', idempotency_key='auth-failure')
    board.claim_ticket(actor, ticket['id'], ticket['version'])
    fresh = board.reserve_execution(actor, 'auth-failure', project['id'], ticket['id'],
        'work-auth-failure', 'turn-auth-failure', 'codex_chatgpt', 'fixture-model')['execution']
    started = board.start_execution(actor, fresh['id'], fresh['revision'])
    return board.finish_execution(actor, started['id'], started['revision'], 'blocked', True, 'authentication_failed')


@app.post('/__fixture__/usage')
async def fixture_usage(request: Request):
    fixture_request(request)
    now = time.time()
    connections = {}
    specs = [
        ('fresh', 'blackoutburst', 'Usage desktop <img src=x onerror=alert(3)>', now),
        ('second', 'blackoutburst', 'Usage laptop', now),
        ('stale', 'blackoutburst', 'Usage old host', now - 180),
        ('unavailable', 'blackoutburst', 'Usage offline host', now),
        ('other', 'thescreamer', 'PRIVATE OTHER OWNER USAGE', now),
    ]
    for variant, owner, label, observed in specs:
        human = humans[owner]
        device = board.create_device(human, label, [])
        raw = board.authenticate(device['token'])
        registered = board.register_agent(raw, owner + '-usage-' + variant, 'Synthetic usage', [], 'codex', False, 'usage', 'Usage agent ' + variant)
        actor = board.actor_session(raw, owner + '-usage-' + variant)
        connection = connection_identifier(board.base_url + '/mcp', owner, registered['identity']['id'], device['id'])
        def bucket(limit_id, used, minutes=None, resets=None):
            return {'limit_id': limit_id, 'source_view': 'multi_bucket',
                'primary': {'used_percent': used, 'window_minutes': minutes, 'resets_at': resets},
                'secondary': None, 'reached_reason': None, 'spend_control_reached': None, 'normal_model_slug': 'fixture-model'}
        buckets = [bucket('fixture-standard', 28, 300, int(now + 3600))]
        if variant == 'fresh':
            buckets.append(bucket('fixture-review', 65, 10080, int(now + 604800)))
            buckets[1]['secondary'] = {'used_percent': 0, 'window_minutes': None, 'resets_at': None}
        elif variant == 'second':
            buckets = [bucket('fixture-standard', 90, 300, int(now + 3600))]
        elif variant == 'stale':
            buckets = [bucket(None, 55, None, int(now - 60))]
            buckets[0]['source_view'] = 'legacy'
        unavailable = variant == 'unavailable'
        snapshot = {'schema_version': 1, 'provider': 'codex_chatgpt', 'source': 'codex_app_server_account_rate_limits',
            'cli_version': 'fixture-1.0', 'data_status': 'unavailable' if unavailable else 'observed',
            'unavailable_reason': 'auth_unavailable' if unavailable else None,
            'ordinary_usage_allowed': None if unavailable else True, 'buckets': [] if unavailable else buckets}
        board.report_usage_observation(actor, connection, observed, snapshot)
        connections[variant] = connection
    return connections


# Put fixture-only controls before the application's static catch-all route.
controls = [route for route in app.router.routes if getattr(route, 'path', '').startswith('/__fixture__/')]
app.router.routes[:] = controls + [route for route in app.router.routes if route not in controls]
print('Runtime preview: 127.0.0.1:18767; isolated synthetic executors only.', flush=True)
uvicorn.run(app, host='127.0.0.1', port=18767, log_level='warning')
