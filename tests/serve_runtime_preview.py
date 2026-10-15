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
    return board.finish_execution(actor, execution_id, execution['revision'], 'blocked', True, 'authentication_failed')


# Put fixture-only controls before the application's static catch-all route.
controls = [route for route in app.router.routes if getattr(route, 'path', '').startswith('/__fixture__/')]
app.router.routes[:] = controls + [route for route in app.router.routes if route not in controls]
print('Runtime preview: 127.0.0.1:18767; isolated synthetic executors only.', flush=True)
uvicorn.run(app, host='127.0.0.1', port=18767, log_level='warning')
