"""Explicit Mac synthetic hard-crash proof; no model, account or production."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

from blackoutburst_agents import dev_test_backend, dev_test_supervisor
from blackoutburst_agents.dev_broker import DevelopmentBroker
from blackoutburst_agents.dev_manifest import DevelopmentError, NamedTest, digest, prepare_manifest
from blackoutburst_agents.dev_test_backend import backend_id, pin_test_plan, prepare_backend
from blackoutburst_agents.dev_test_supervisor import SupervisedNativeNamedPythonBackend, prepare_supervision


CONTROLLER = r'''
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from blackoutburst_agents.dev_test_supervisor import SupervisedNativeNamedPythonBackend
from blackoutburst_agents.dev_broker import DevelopmentBroker
folder=Path(sys.argv[2])
manifest=json.loads((folder/'broker/manifest.json').read_text())
backend=SupervisedNativeNamedPythonBackend(folder/'backend',manifest=manifest,authorize_scope=lambda *_:True)
broker=DevelopmentBroker(folder/'broker',enabled=True,authorize_scope=lambda *_:True,test_backend=backend)
broker.run_test('hard-controller-crash','crash')
'''


def gone(pid):
    try:
        os.kill(pid, 0)  # Observation only; never signal a recovered PID.
        return False
    except ProcessLookupError:
        return True


def prepare_fixture(root):
    """Only data counters enter model/broker file caps, never trusted scripts."""
    fixtures = {
        'passed': ("from pathlib import Path\np=Path('count.txt')\np.write_text(str(int(p.read_text())+1))\nprint('bounded fixture')\n", 5),
        'timeout': ("import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(20)\n", 1),
        'crash': ("import os,signal,time\nfrom pathlib import Path\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\np=Path('crash-count.txt')\np.write_text(str(int(p.read_text())+1))\nPath('crash-started.txt').write_text(str(os.getpid()))\ntime.sleep(20)\n", 10),
    }
    for data in ('count.txt', 'crash-count.txt'):
        (root / data).write_text('0')
    scripts = {}
    for name, (source, timeout) in fixtures.items():
        (root / (name + '.py')).write_text(source)
        scripts[name] = {'script': name + '.py', 'timeout_seconds': timeout, 'max_output_bytes': 128}
    return scripts, ['count.txt', 'crash-count.txt']


def script_patch_refused(broker, root, script):
    before = (root / script).read_bytes()
    try:
        broker.patch_file('trusted-script-patch-denied', script, digest(before), before.decode())
    except DevelopmentError as error:
        return error.code == 'path_not_allowed' and (root / script).read_bytes() == before
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--codex', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix='named-supervisor-proof-', dir='/private/tmp')).resolve()
    report = {'schema': 1, 'observed_on': datetime.now(timezone.utc).date().isoformat(), 'model_calls': 0,
        'controller_authentication_files_read': False, 'auth_rpc_calls_requested': 0,
        'production_enabled': False, 'checks': {}, 'result': 'not_finished',
        'backend_source_sha256': digest(Path(dev_test_backend.__file__).read_bytes()),
        'supervisor_source_sha256': digest(Path(dev_test_supervisor.__file__).read_bytes())}
    backend = broker = controller = None
    helper_pids = []
    try:
        root = directory / 'workspace'
        root.mkdir(mode=0o700)
        scripts, data_paths = prepare_fixture(root)
        env = {key: os.environ[key] for key in ('HOME', 'CODEX_HOME', 'TMPDIR') if key in os.environ}
        env.update({'LANG': 'C', 'PATH': '/usr/bin:/bin'})
        version = subprocess.run([args.codex, '--version'], capture_output=True, timeout=5,
            env=env, stdin=subprocess.DEVNULL, close_fds=True).stdout.decode().strip()
        plan = pin_test_plan(root=str(root), codex=args.codex, cli_version=version, scripts=scripts)
        report.update({'host': plan['host'], 'cli_version': version, 'cli_sha256': plan['codex_sha256'],
            'wrapper_sha256': plan['wrapper_sha256'], 'profile_version': plan['profile_version'],
            'test_plan_schema': plan['schema'],
            'trusted_script_pins': {name: {key: test['script_pin'][key] for key in ('sha256', 'bytes')}
                for name, test in plan['tests'].items()}})
        manifest_dir = directory / 'broker'
        scope = dict(owner_id='synthetic-owner', agent_id='synthetic-agent', device_id='synthetic-device',
            session_id='synthetic-session', project_id='synthetic-project', ticket_id='synthetic-ticket', profile_version=1)
        authorize = lambda given, action, resource: given == scope and resource == 'repo:synthetic'
        prepare_manifest(manifest_dir, root=str(root), base_sha='a' * 40, scope=scope,
            paths=data_paths, resource='repo:synthetic', authorize_scope=authorize,
            tests=[NamedTest(name, backend_id=backend_id(plan), timeout_seconds=test['timeout_seconds'],
                max_output_bytes=128) for name, test in scripts.items()])
        manifest = json.loads((manifest_dir / 'manifest.json').read_text())
        report['checks']['scripts_outside_patch_caps'] = not set(manifest['files']).intersection(
            suite['script'] for suite in scripts.values())
        backend_dir = directory / 'backend'
        prepare_backend(backend_dir, manifest=manifest, plan=plan)
        prepare_supervision(backend_dir)
        backend = SupervisedNativeNamedPythonBackend(backend_dir, manifest=manifest, authorize_scope=authorize)
        report['qualification'] = backend.qualify()
        if report['qualification']['result'] != 'qualified_supervised_fixture':
            report['result'] = 'blocked_supervisor_qualification'
            return
        broker = DevelopmentBroker(manifest_dir, enabled=True, authorize_scope=authorize, test_backend=backend)
        report['checks']['trusted_script_patch_refused'] = script_patch_refused(broker, root, scripts['passed']['script'])
        for name in ('passed', 'timeout'):
            receipt = broker.run_test('supervisor-' + name, name)
            report['checks'][name] = {'result': receipt['result'], 'cleanup_confirmed': receipt.get('cleanup_confirmed')}
        package_root = str(Path(dev_test_supervisor.__file__).parent.parent)
        controller = subprocess.Popen(['/usr/bin/python3', '-I', '-S', '-c', CONTROLLER, package_root, str(directory)],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True)
        deadline = time.monotonic() + 20
        while not (root / 'crash-started.txt').exists() and time.monotonic() < deadline and controller.poll() is None:
            time.sleep(0.05)
        if not (root / 'crash-started.txt').exists():
            report['result'] = 'controller_test_did_not_start'
            return
        test_pid = int((root / 'crash-started.txt').read_text())
        os.kill(controller.pid, signal.SIGKILL)  # Exact owned synthetic controller.
        controller.wait(timeout=2)
        report['checks']['controller_hard_killed'] = controller.returncode == -signal.SIGKILL
        deadline = time.monotonic() + 10
        operation_id = digest(b'hard-controller-crash')
        state_path = backend_dir / ('supervision-' + operation_id + '.state.json')
        while time.monotonic() < deadline:
            state = json.loads(state_path.read_text())
            if state['phase'] == 'complete':
                break
            time.sleep(0.05)
        report['checks']['durable_helper_completion'] = state['phase'] == 'complete'
        if state['supervisor_identity'] is not None:
            helper_pids.append(state['supervisor_identity']['pid'])
        receipt = broker.reconcile('hard-controller-crash')
        report['checks']['crash_recovery'] = {'result': receipt['result'], 'cleanup_confirmed': receipt.get('cleanup_confirmed')}
        old = (root / 'crash-count.txt').read_text()
        replay = broker.run_test('hard-controller-crash', 'crash')
        report['checks']['crash_replay_no_execution'] = (old == '1'
            and (root / 'crash-count.txt').read_text() == '1' and replay == receipt)
        deadline = time.monotonic() + 3
        while (not gone(test_pid) or any(not gone(pid) for pid in helper_pids)) and time.monotonic() < deadline:
            time.sleep(0.05)
        report['checks']['test_and_helper_gone'] = gone(test_pid) and all(gone(pid) for pid in helper_pids)
        report['result'] = ('passed_supervisor_fixture' if report['checks']['controller_hard_killed']
            and report['checks']['scripts_outside_patch_caps']
            and report['checks']['trusted_script_patch_refused']
            and report['checks']['durable_helper_completion'] and report['checks']['crash_replay_no_execution']
            and report['checks']['test_and_helper_gone']
            and report['checks']['passed'] == {'result': 'passed', 'cleanup_confirmed': True}
            and report['checks']['timeout'] == {'result': 'failed', 'cleanup_confirmed': True}
            and report['checks']['crash_recovery'] == {'result': 'cancelled', 'cleanup_confirmed': True}
            else 'failed_supervisor_fixture')
    finally:
        if controller is not None and controller.poll() is None:
            controller.kill()
            controller.wait(timeout=2)
        if broker is not None:
            broker.close()
        if backend is not None:
            backend.close()
        active = []
        for path in (directory / 'backend').glob('supervision-*.state.json'):
            state = json.loads(path.read_text())
            identity = state.get('supervisor_identity')
            if identity is not None and not gone(identity['pid']):
                active.append(identity['pid'])
        if not active:
            shutil.rmtree(directory)
        report['synthetic_files_removed'] = not directory.exists()
        Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
        print(json.dumps({'result': report['result'], 'checks': report['checks'],
            'qualification': report.get('qualification'), 'synthetic_files_removed': report['synthetic_files_removed']}, sort_keys=True))


if __name__ == '__main__':
    main()
