"""Deployment failures expose exact public codes, never exception text."""
import json

import pytest

from deploy.worker_pool import manage, package as p


CODES = (
    'pool_system_command_failed',
    'pool_unit_not_stopped_disabled',
    'pool_unit_not_inactive_disabled',
    'pool_unit_changed',
    'pool_unit_state_changed',
    'pool_previous_receipt_invalid',
    'pool_continuation_previous_receipt_invalid',
    'pool_continuation_install_exists',
    'pool_upgrade_scope_changed',
    'pool_upgrade_completion_unconfirmed',
    'pool_continuation_upgrade_completion_unconfirmed',
    'pool_continuation_upgrade_units_unconfirmed',
)


def refusal(monkeypatch, capsys, error):
    def rejected(*args, **kwargs):
        raise error
    monkeypatch.setattr(manage, 'upgrade_continuation', rejected)
    assert manage.main(['upgrade-continuation', '/public/reviewed-release',
        '--previous-bundle-id', 'a' * 64, '--expected-bundle-id', 'b' * 64, '--apply']) == 2
    captured = capsys.readouterr()
    assert 'PRIVATE' not in captured.out and captured.err == ''
    return json.loads(captured.out)


@pytest.mark.parametrize('code', CODES)
def test_known_upgrade_code_survives_without_private_exception_args(monkeypatch, capsys, code):
    error = p.PackageError(code)
    # str(error) would now expose this private text; the CLI must only use .code.
    error.args = ('PRIVATE stderr path /private/owner/token',)
    assert refusal(monkeypatch, capsys, error) == {'status': 'refused', 'reason': code}


@pytest.mark.parametrize('code', [
    'PRIVATE server traceback',
    'pool_system_command_failed_PRIVATE',
    'pool_continuation_upgrade_PRIVATE',
    {'PRIVATE': 'unhashable'},
    None,
])
def test_unknown_or_non_string_codes_remain_opaque(monkeypatch, capsys, code):
    assert refusal(monkeypatch, capsys, p.PackageError(code)) == {'status': 'refused'}


def test_subclass_cannot_publish_even_an_allowed_code(monkeypatch, capsys):
    class PrivatePackageError(p.PackageError):
        pass
    error = PrivatePackageError('pool_system_command_failed')
    error.args = ('PRIVATE extension details',)
    assert refusal(monkeypatch, capsys, error) == {'status': 'refused'}


@pytest.mark.parametrize('error', [OSError('PRIVATE filesystem error'),
                                 ValueError('PRIVATE parsing input'),
                                 TypeError('PRIVATE input shape')])
def test_other_exception_types_retain_historical_opaque_output(monkeypatch, capsys, error):
    assert refusal(monkeypatch, capsys, error) == {'status': 'refused'}
