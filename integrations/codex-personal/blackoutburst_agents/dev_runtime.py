"""Separate sequential Runtime reservations for local broker effects.

The opaque server provider tag distinguishes tools from model turns. Local
tools never report quota/authentication observations from source/test text.
"""
import uuid

from .dev_manifest import canonical, digest
from .runtime_guard import RuntimeControlError, RuntimeGuard
from .runtime_journal import RuntimeJournal


def development_work_id(operation_id):
    try:
        identifier = uuid.UUID(operation_id)
        if str(identifier) != operation_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise RuntimeControlError('runtime_scope_mismatch') from None
    return str(uuid.uuid5(identifier, 'development-model-work'))


def development_agent_id(scope, operation_id=None):
    identity = {key: scope[key] for key in ('board_endpoint', 'board_session_id', 'agent_id')}
    if operation_id is not None:
        development_work_id(operation_id)
        identity['development_operation_id'] = operation_id
    return 'board-agent-' + digest(canonical(identity))


class DevelopmentRuntimeGuard(RuntimeGuard):
    """Map one private operation work to the unchanged, real Board scope."""
    def __init__(self, *args, operation_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.development_operation_id = operation_id
        if operation_id is not None:
            development_work_id(operation_id)

    def configuration(self, work, turn_id):
        if self.development_operation_id is None:
            return super().configuration(work, turn_id)
        if (any(work.get(key) != self.scope[key] for key in ('owner_id', 'project_id', 'ticket_id'))
                or work.get('id') != development_work_id(self.development_operation_id)
                or work.get('agent_id') != development_agent_id(self.scope, self.development_operation_id)
                or work.get('decision_memory') is not None):
            raise RuntimeControlError('runtime_scope_mismatch')
        return RuntimeJournal.configuration(self.scope, work['id'], turn_id, work['model'])


class ToolRuntimeGuard(DevelopmentRuntimeGuard):
    def __init__(self, journal, client, scope, *, action_label, clock=None, operation_id=None):
        options = {} if clock is None else {'clock': clock}
        super().__init__(journal, client, scope, operation_id=operation_id, **options)
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


class ScopePinnedModelRuntimeGuard(DevelopmentRuntimeGuard):
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
