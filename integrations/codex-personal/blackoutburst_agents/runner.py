"""Explicit local turns with durable state; no board polling or task selection."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .codex_turn import execute_turn
from .run_store import RunStore, StoreError
from .subscription_probe import ProbeSettings
from .runtime_guard import RuntimeAdmissionRefused


class PersonalRunner:
    def __init__(self, store: RunStore, transport: Callable = execute_turn, *, runtime_guard=None):
        self.store = store
        self.transport = transport
        self.runtime_guard = runtime_guard

    def run(self, work_id: str, request_id: str, prompt: str, *,
            timeout: float = 120, should_cancel: Callable[[], bool] | None = None, context_guard=None,
            before_prompt: Callable[[], None] | None = None, provider_attempt=None) -> dict:
        """Process one explicit input. Retrying its key never starts another turn.

        The returned result may contain model text. Callers must treat it as
        private, untrusted content and publish only deliberately selected data.
        """
        with self.store.work_lock(work_id):
            work = self.store.get_work(work_id)
            if provider_attempt is not None:
                from .provider_disposition import _claim_provider_attempt
                _claim_provider_attempt(provider_attempt, work, request_id, self.transport, self.runtime_guard, store=self.store)
            self.store.check_context_guard(work_id,context_guard)
            if context_guard is not None:work={**work,'decision_memory':context_guard}
            settings = ProbeSettings(executable=work['executable'], model=work['model'],
                                     timeout_seconds=timeout)
            entry = self.store.begin_turn(work_id, request_id, prompt)
            turn_id = entry['turn']['id']
            if entry['cached']:
                if self.runtime_guard is not None:
                    self.runtime_guard.confirm_cached(work, turn_id, self.store.turn_result(turn_id))
                return {'work_id': work_id, 'turn_id': turn_id, 'cached': True,
                        'result': self.store.turn_result(turn_id)}

            if provider_attempt is not None:
                from .provider_disposition import _bind_provider_turn
                _bind_provider_turn(provider_attempt, turn_id)

            if self.runtime_guard is not None:
                try:
                    self.runtime_guard.before(work, turn_id)
                except RuntimeAdmissionRefused:
                    self.store.finish_turn(turn_id, {'status':'blocked','reason':'runtime_capacity_exhausted',
                        'text':'','uncertain':False,'verification':{'runtime_not_admitted':True}})
                    raise
                except Exception:
                    self.store.finish_turn(turn_id, {'status': 'blocked', 'reason': 'runtime_start_requires_review',
                        'text': '', 'uncertain': True, 'verification': {'cleanup_confirmed': True}})
                    raise

            def cancelled():
                return bool((should_cancel and should_cancel()) or self.store.is_cancelled(turn_id)
                    or (self.runtime_guard is not None and self.runtime_guard.should_cancel()))

            try:
                # Opt-in only: existing transports retain their original kwargs.
                prompt_guard = {} if before_prompt is None else {'before_prompt': before_prompt}
                observations = []
                if provider_attempt is not None:
                    prompt_guard.update(provider_disposition=True,
                        on_provider_observation=observations.append)
                result = self.transport(
                    settings, prompt, Path(work['workspace']),
                    resume_session_id=work['provider_session_id'],
                    on_started=lambda pid: self.store.on_process(turn_id, pid),
                    on_session=lambda sid: self.store.on_session(turn_id, sid),
                    should_cancel=cancelled,
                    **prompt_guard,
                )
            except Exception:
                # The transport owns process cleanup in its finally block.
                # Preserve an honest interrupted record without raw diagnostics.
                result = {'status': 'blocked', 'reason': 'runner_error',
                          'session_id': None, 'text': ''}
            self.store.finish_turn(turn_id, result)
            if self.runtime_guard is not None:
                self.runtime_guard.finish(work, turn_id, self.store.turn_result(turn_id))
            if provider_attempt is not None:
                from .provider_disposition import _finish_provider_attempt
                # There is no dictionary/cached-result path into this receipt.
                try:
                    _finish_provider_attempt(provider_attempt, self.store, work, turn_id,
                        observations[0] if len(observations) == 1 else None,
                        self.runtime_guard, should_cancel)
                except Exception:
                    raise StoreError('provider_disposition_requires_review') from None
            return {'work_id': work_id, 'turn_id': turn_id, 'cached': False,
                    'result': self.store.turn_result(turn_id)}


def public_result(value: dict) -> dict:
    """CLI receipt suitable for status display, excluding model/private content."""
    result = value['result']
    return {key: value[key] for key in ('work_id', 'turn_id', 'cached')} | {
        'status': result.get('status', 'blocked'),
        'reason': result.get('reason', 'unknown'),
        'has_response': bool(result.get('text')),
    }
