"""Loopback real-ASGI fixture. Synthetic data only; no provider or worker."""
import argparse
from contextlib import nullcontext
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import signal
import sys
import tempfile
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--result', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    outside = []

    def network_guard(event, values):
        if event == 'socket.connect':
            address = values[1]
            if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1'):
                outside.append('connect'); raise RuntimeError('non_loopback_network_forbidden')
        elif event == 'socket.getaddrinfo' and values[0] not in ('127.0.0.1', '::1', 'localhost', None):
            outside.append('dns'); raise RuntimeError('non_loopback_dns_forbidden')

    sys.addaudithook(network_guard)
    with tempfile.TemporaryDirectory(prefix='board-provision-asgi-') as temporary:
        sock = socket.socket(); sock.bind(('127.0.0.1', 0))
        origin = 'http://127.0.0.1:' + str(sock.getsockname()[1])
        # app.py has a module-level app; confine that initialization too. These
        # are fixture settings, never a HOME override or a real auth source.
        os.environ.update(BOARD_DATA_DIR=str(Path(temporary) / 'unused-default'),
            BOARD_BASE_URL=origin, BOARD_ROOT_PATH='', BLACKOUT_SHARED_AUTH='0',
            BOARD_FILEBROWSER_URL='', BOARD_ALLOW_LOCAL_PASSWORDS='1',
            BOARD_FILEBROWSER_MAP='{"blackout":"blackoutburst","TheScreamer":"thescreamer"}',
            BOARD_TICKET_LEASE_SECONDS='1800')
        from app import create_app
        import httpx
        import uvicorn
        logging.getLogger('mcp.server.streamable_http_manager').setLevel(logging.ERROR)

        def denied(request):
            outside.append('http'); raise AssertionError('external_http_forbidden')

        application = create_app(Path(temporary) / 'board', base_url=origin,
            filebrowser_url='', allow_local_passwords=True, root_path='',
            discord_transport=httpx.MockTransport(denied))
        board = application.state.board
        board.ensure_owner('synthetic-owner', 'Compte de démonstration', 'fixture-password-only')
        human = {'kind': 'human', 'owner_id': 'synthetic-owner'}
        board.set_policy(human, True, True, True, True, [], [])
        device = board.create_device(human, 'Appareil de démonstration', [])
        manager = board.authenticate(device['token'])
        policy = dict(allow_read=True, allow_messages=True, allow_requests=False,
            actions=[], resources=[], autonomy='responses')
        existing = board.create_agent_profile(human, 'Profil déjà hébergé', policy=policy,
            preferred_provider='codex_chatgpt', preferred_model='requested-only')
        existing = board.bind_agent_profile(human, existing['id'], existing['version'], device['id'], 'existing', 'codex')
        host = board.create_server_host(human, device['id'], 'Serveur de démonstration', 'synthetic-host')
        entry = dict(catalog_key='fixture-target', profile_id=existing['id'], profile_version=existing['version'],
            launchspec_sha256='a'*64, mode='messages')
        instance = board.publish_server_catalog(manager, host['id'], 0, [entry], 'synthetic-catalog')['instances'][0]
        command = board.command_server_instance(human, instance['id'], 'stop', instance['version'],
            instance['generation'], instance['launchspec_sha256'], 0, 'synthetic-human-stop')
        accepted = board.ack_instance_command(manager, host['id'], command['id'], command['revision'], 'synthetic-stop-ack')
        i = accepted['instance']
        board.report_instance_state(manager, host['id'], i['id'], i['observation_revision'], i['generation'],
            i['launchspec_sha256'], command['id'], 'stopped', True, '', 'synthetic-stop-report')
        wanted = board.create_agent_profile(human, 'Nouveau profil souhaité', policy=policy,
            preferred_provider='codex_chatgpt', preferred_model='')
        wanted = board.bind_agent_profile(human, wanted['id'], wanted['version'], device['id'], 'wanted', 'codex')
        calls = []

        @application.middleware('http')
        async def metadata_only(request, call_next):
            response = await call_next(request)
            if request.url.path.startswith('/api/'):
                calls.append(dict(method=request.method, path=request.url.path, status=response.status_code))
            return response

        def snapshot():
            tables = ('server_hosts', 'server_instances', 'server_instance_commands',
                'runtime_executions', 'runtime_admission_refusals', 'owner_execution_limits')
            with board._connection() as db:
                rows = {table: [tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')] for table in tables}
                values = dict(protected_rows_sha256=hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                    runtime_rows=len(rows['runtime_executions']), instance_rows=len(rows['server_instances']),
                    command_rows=len(rows['server_instance_commands']),
                    requested=db.execute("SELECT COUNT(*) FROM server_provisioning_requests WHERE state='requested'").fetchone()[0],
                    cancelled=db.execute("SELECT COUNT(*) FROM server_provisioning_requests WHERE state='cancelled'").fetchone()[0],
                    barriers=db.execute('SELECT COUNT(*) FROM server_provisioning_barriers').fetchone()[0],
                    grant_rows=db.execute('SELECT COUNT(*) FROM server_provisioning_grants').fetchone()[0])
            return values

        before = snapshot()

        @application.get('/__synthetic_snapshot')
        async def synthetic_snapshot():
            return snapshot()
        # create_app deliberately mounts MCP at / last. Put only this fixture
        # observation before that catch-all; all product routes are unchanged.
        application.router.routes.insert(0, application.router.routes.pop())

        server = uvicorn.Server(uvicorn.Config(application, host='127.0.0.1', port=sock.getsockname()[1],
            log_level='critical', access_log=False, timeout_keep_alive=2, limit_concurrency=16))
        server.capture_signals = lambda: nullcontext()
        signal.signal(signal.SIGTERM, lambda *_: setattr(server, 'should_exit', True))

        def ready():
            deadline = time.monotonic() + 20
            while not server.started and time.monotonic() < deadline:
                time.sleep(.01)
            if not server.started:
                return
            print(json.dumps(dict(ready=True, origin=origin, host_id=host['id'], instance_id=instance['id'],
                existing_profile=existing['id'], wanted_profile=wanted['id'])), flush=True)

        threading.Thread(target=ready, daemon=True).start()
        try:
            server.run(sockets=[sock])
        finally:
            after = snapshot()
            result = dict(schema='synthetic-asgi-provision-fixture-v1', before=before, after=after,
                protected_rows_unchanged=before['protected_rows_sha256']==after['protected_rows_sha256'],
                calls=calls, outside_network_attempts=len(outside), provider_calls=0, model_calls=0,
                real_accounts=0, manager_metadata_observations_are_synthetic=True)
            Path(args.result).write_text(json.dumps(result, indent=2)+'\n')


if __name__ == '__main__':
    main()
