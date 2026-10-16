"""Explicit synthetic planner-policy qualification, without model/auth RPC."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile

from blackoutburst_agents.planner_policy import PlannerPolicy, prepare_planner_policy
from blackoutburst_agents.subscription_probe import _run_bounded, sanitized_environment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--codex', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    codex = args.codex.resolve(strict=True)
    receipt = {'schema': 1, 'model_calls': 0, 'auth_rpc_calls_requested': 0,
               'production_enabled': False, 'result': 'blocked',
               'limitations': ['Darwin only; other operating systems unqualified',
                   'Measures the sandbox command using default_permissions without -P',
                   'No exec model turn or client service traffic is qualified',
                   'Trusted controller/toolchain and exclusive empty workspace required']}
    with tempfile.TemporaryDirectory(prefix='planner-policy-proof-') as temporary:
        root = Path(temporary).resolve()
        workspace, checkout, state = (root / name for name in ('planner','checkout','state'))
        workspace.mkdir(mode=0o700);checkout.mkdir(mode=0o700)
        version = _run_bounded([str(codex),'--version'], str(root), 5, 4096, sanitized_environment())
        if version.failure or version.returncode != 0:
            raise RuntimeError('planner_cli_metadata_unavailable')
        fingerprint = prepare_planner_policy(state, workspace=workspace, checkout=checkout,
            executable=codex, cli_version=version.stdout.decode('utf-8','strict').strip())
        with PlannerPolicy(state) as policy:
            try:
                qualification = policy.qualify()
                policy.validate_for(codex, workspace)
                receipt.update(result='passed_synthetic_planner_policy', checks=qualification['checks'],
                               cleanup_confirmed=qualification['cleanup_confirmed'])
            except Exception as error:
                receipt['reason'] = getattr(error, 'code', 'planner_qualification_failed')
            receipt.update(fingerprint=fingerprint, cli_sha256=policy.cli_sha256,
                cli_version=policy._plan['cli_version'], host=policy._plan['host'],
                policy_template={'filesystem':{':root':'deny',':minimal':'read',':tmpdir':'deny',
                    ':slash_tmp':'deny','<synthetic planner workspace>':'read'},'network':{'enabled':False}},
                profile_version=policy._plan['profile_version'])
    receipt['synthetic_files_removed'] = not root.exists()
    # No host paths, secrets, raw output or private conversation are collected.
    receipt['module_sha256'] = hashlib.sha256(Path(__import__(
        'blackoutburst_agents.planner_policy', fromlist=['__file__']).__file__).read_bytes()).hexdigest()
    args.output.write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n')
    args.output.chmod(0o600)
    print(json.dumps({'result':receipt['result'], 'reason':receipt.get('reason'),
                      'synthetic_files_removed':receipt['synthetic_files_removed']}))
    return 0 if receipt['result'].startswith('passed') else 2


if __name__ == '__main__':
    raise SystemExit(main())
