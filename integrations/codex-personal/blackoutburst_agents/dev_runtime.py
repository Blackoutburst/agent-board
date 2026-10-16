"""Separate sequential Runtime reservations for local broker effects.

The opaque server provider tag distinguishes tools from model turns. Local
tools never report quota/authentication observations from source/test text.
"""
from .runtime_guard import RuntimeControlError, RuntimeGuard
from .runtime_journal import RuntimeJournal


class ToolRuntimeGuard(RuntimeGuard):
    def __init__(self, journal, client, scope, *, action_label, clock=None):
        options = {} if clock is None else {'clock': clock}
        super().__init__(journal, client, scope, **options)
        self.action_label = action_label

    def configuration(self, work, turn_id):
        base = super().configuration(work, turn_id)
        if base['model'] != self.action_label:
            raise RuntimeControlError('runtime_scope_mismatch')
        return RuntimeJournal.configuration(self.scope, base['work_id'], base['turn_id'],
                                            self.action_label, provider='local_broker')

    def finish_action(self, work, turn_id, *, status, cleanup_confirmed):
        if status not in ('completed', 'blocked', 'cancelled') or type(cleanup_confirmed) is not bool:
            raise RuntimeControlError('runtime_result_invalid')
        result = {'status': status, 'reason': ('completed' if status == 'completed' else
            'cancelled' if status == 'cancelled' else 'execution_failed'),
            'verification': {'cleanup_confirmed': cleanup_confirmed}}
        return super().finish(work, turn_id, result)

    def finish(self, work, turn_id, result):
        # This general adapter entrypoint accepts only controller-created
        # results. Never let a local error label become a quota observation.
        if type(result) is not dict:
            raise RuntimeControlError('runtime_result_invalid')
        if result.get('reason') in ('quota_or_rate_limit', 'authentication_failed'):
            self.review()
            raise RuntimeControlError('local_runtime_reason_forbidden')
        return super().finish(work, turn_id, result)


class ScopePinnedModelRuntimeGuard(RuntimeGuard):
    """Cross-check the admitted device/profile before PersonalRunner transport."""
    def __init__(self, *args, validate_reservation, **kwargs):
        super().__init__(*args, **kwargs)
        self.validate_reservation = validate_reservation

    def before(self, work, turn_id):
        super().before(work, turn_id)
        try:
            self.validate_reservation(self)
        except Exception:
            self.review()
            raise
