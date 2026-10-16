"""Explicit synthetic, no-model qualification; not part of test discovery.

Run only on an authorized host. It uses a temporary fixture and never opens
authentication files, calls a model, uses SSH, or enables a production backend.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile

from blackoutburst_agents.dev_broker import DevelopmentBroker
from blackoutburst_agents.dev_manifest import DevelopmentError, NamedTest, digest, prepare_manifest
from blackoutburst_agents.dev_test_backend import (NativeNamedPythonBackend, backend_id,
    pin_test_plan, prepare_backend)
from blackoutburst_agents import dev_test_backend


def prepare_fixture(root):
    """Trusted controller scripts are separate from patchable fixture data."""
    fixtures = {
        'passed': ("from pathlib import Path\np=Path('count.txt')\np.write_text(str(int(p.read_text())+1))\nprint('bounded fixture')\n", 5, 128),
        'failed': ('raise SystemExit(9)\n', 5, 128),
        'timeout': ('import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(20)\n', 1, 128),
        'output': ("print('x'*65536)\n", 5, 16),
        'cancelled': ("from pathlib import Path\nimport time\nPath('cancel-started.txt').write_text('synthetic')\ntime.sleep(20)\n", 5, 128),
        'gate': ("from pathlib import Path\nPath('gate-started.txt').write_text('unexpected')\n", 5, 128),
    }
    (root / 'count.txt').write_text('0')
    scripts = {}
    for name, (source, timeout, maximum) in fixtures.items():
        (root / (name + '.py')).write_text(source)
        scripts[name] = {'script': name + '.py', 'timeout_seconds': timeout, 'max_output_bytes': maximum}
    return scripts, ['count.txt']


def script_patch_refused(broker, root, script):
    before = (root / script).read_bytes()
    try:
        broker.patch_file('trusted-script-patch-denied', script, digest(before), before.decode())
    except DevelopmentError as error:
        return error.code == 'path_not_allowed' and (root / script).read_bytes() == before
    return False


def measure_ack_gate(backend, root, directory):
    """Real EOF guard: the synthetic source must never start without ACK."""
    inside = root / 'gate-canary.txt'
    outside = directory / 'outside-gate-canary.txt'
    marker = root / 'gate-started.txt'
    inside.write_text('synthetic canary')
    outside.write_text('synthetic outside canary')
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        payload = {'inside': str(inside), 'inside_write': str(inside) + '-write',
            'outside': str(outside), 'outside_write': str(outside) + '-write',
            'port': listener.getsockname()[1], 'timeout': 5, 'qualify': False,
            'executable': backend.plan['executable'],
            'environment': backend.plan['environment']}
        payload.update(backend._script_payload('gate'))
        env = {key: os.environ[key] for key in ('HOME', 'CODEX_HOME', 'TMPDIR') if key in os.environ}
        env.update({'PATH': '/usr/bin:/bin', 'LANG': 'C'})
        child = subprocess.run(backend._command(payload), cwd=root, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=20, close_fds=True, start_new_session=True)
        header = child.stdout.splitlines()[0] if child.stdout else b''
        if not header.startswith(dev_test_backend.HEADER):
            return False
        guard = json.loads(header[len(dev_test_backend.HEADER):])
        return child.returncode == 79 and guard['ok'] is True and not marker.exists()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--codex', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix='named-test-qualification-', dir='/private/tmp')).resolve()
    report = {'schema': 1, 'model_calls': 0, 'controller_authentication_files_read': False,
              'auth_rpc_calls_requested': 0,
              'real_repository_tests': False, 'production_enabled': False, 'checks': {},
              'observed_on': '2026-10-16',
              'backend_source_sha256': digest(Path(dev_test_backend.__file__).read_bytes())}
    backend = broker = None
    try:
        root = directory / 'workspace'
        root.mkdir(mode=0o700)
        scripts, data_paths = prepare_fixture(root)
        env = {key: os.environ[key] for key in ('HOME', 'CODEX_HOME', 'TMPDIR') if key in os.environ}
        env.update({'PATH': '/usr/bin:/bin', 'LANG': 'C'})
        version = subprocess.run([args.codex, '--version'], stdin=subprocess.DEVNULL, capture_output=True,
                                 timeout=5, env=env, close_fds=True).stdout.decode().strip()
        plan = pin_test_plan(root=str(root), codex=args.codex, cli_version=version, scripts=scripts)
        report.update({'cli_version': version, 'cli_sha256': plan['codex_sha256'],
            'executable_sha256': plan['executable_sha256'], 'host': plan['host'],
            'profile_version': plan['profile_version'], 'wrapper_sha256': plan['wrapper_sha256'],
            'test_plan_schema': plan['schema'],
            'trusted_script_pins': {name: {key: test['script_pin'][key] for key in ('sha256', 'bytes')}
                for name, test in plan['tests'].items()},
            'policy': {'filesystem': {':root': 'deny', ':minimal': 'read', ':tmpdir': 'deny',
                       ':slash_tmp': 'deny', '<synthetic-workspace>': 'write'}, 'network': {'enabled': False}}})
        manifest_dir = directory / 'broker'
        scope = dict(owner_id='synthetic-owner', agent_id='synthetic-agent', device_id='synthetic-device',
                     session_id='synthetic-session', project_id='synthetic-project', ticket_id='synthetic-ticket',
                     profile_version=1)
        authorization = {'revocation_enabled': False}
        def authorize(given, action, resource):
            return (given == scope and resource == 'repo:synthetic'
                and not (authorization['revocation_enabled'] and (root / 'cancel-started.txt').exists()))
        prepare_manifest(manifest_dir, root=str(root), base_sha='a' * 40, scope=scope,
            paths=data_paths, resource='repo:synthetic', authorize_scope=authorize,
            tests=[NamedTest(name, backend_id=backend_id(plan), timeout_seconds=suite['timeout_seconds'],
                max_output_bytes=suite['max_output_bytes']) for name, suite in scripts.items()])
        manifest = json.loads((manifest_dir / 'manifest.json').read_text())
        report['checks']['scripts_outside_patch_caps'] = not set(manifest['files']).intersection(
            suite['script'] for suite in scripts.values())
        backend_dir = directory / 'backend'
        prepare_backend(backend_dir, manifest=manifest, plan=plan)
        backend = NativeNamedPythonBackend(backend_dir, manifest=manifest, authorize_scope=authorize)
        qualification = backend.qualify()
        report['qualification'] = qualification
        if qualification['result'] != 'qualified_single_process_fixture':
            report['result'] = 'blocked_native_qualification'
            return
        report['checks']['ack_required_before_source'] = measure_ack_gate(backend, root, directory)
        broker = DevelopmentBroker(manifest_dir, enabled=True, authorize_scope=authorize, test_backend=backend)
        report['checks']['trusted_script_patch_refused'] = script_patch_refused(broker, root, scripts['passed']['script'])
        for name in ('passed', 'failed', 'timeout', 'output'):
            receipt = broker.run_test('qualification-' + name, name)
            report['checks'][name] = {'result': receipt['result'], 'status': receipt['status'],
                'cleanup_confirmed': receipt.get('cleanup_confirmed'), 'output_bytes': receipt.get('output_bytes')}
        first = broker.run_test('qualification-passed', 'passed')
        report['checks']['cached_replay'] = (first['result'] == 'passed' and (root / 'count.txt').read_text() == '1')
        authorization['revocation_enabled'] = True
        receipt = broker.run_test('qualification-cancelled', 'cancelled')
        report['checks']['cancelled'] = {'result': receipt['result'], 'status': receipt['status'],
            'cleanup_confirmed': receipt.get('cleanup_confirmed'), 'output_bytes': receipt.get('output_bytes')}
        expected = {'passed': 'passed', 'failed': 'failed', 'timeout': 'failed', 'output': 'failed', 'cancelled': 'cancelled'}
        report['result'] = ('passed_fixture_qualification' if report['checks']['cached_replay']
            and report['checks']['ack_required_before_source']
            and report['checks']['scripts_outside_patch_caps']
            and report['checks']['trusted_script_patch_refused']
            and all(report['checks'][name]['result'] == result
                and report['checks'][name]['cleanup_confirmed'] is True for name, result in expected.items())
            else 'failed_fixture_qualification')
    finally:
        if broker is not None:
            broker.close()
        if backend is not None:
            backend.close()
        shutil.rmtree(directory)
        report['synthetic_files_removed'] = not directory.exists()
        Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
        print(json.dumps({'result': report.get('result'), 'synthetic_files_removed': report['synthetic_files_removed'],
                          'checks': report['checks'], 'qualification': report.get('qualification')}, sort_keys=True))


if __name__ == '__main__':
    main()
