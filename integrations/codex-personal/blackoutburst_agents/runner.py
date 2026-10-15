"""Explicit local turns with durable state; no board polling or task selection."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .codex_turn import execute_turn
from .run_store import RunStore
from .subscription_probe import ProbeSettings


class PersonalRunner:
    def __init__(self, store: RunStore, transport: Callable = execute_turn):
        self.store = store
        self.transport = transport

    def run(self, work_id: str, request_id: str, prompt: str, *,
            timeout: float = 120, should_cancel: Callable[[], bool] | None = None) -> dict:
        """Process one explicit input. Retrying its key never starts another turn.

        The returned result may contain model text. Callers must treat it as
        private, untrusted content and publish only deliberately selected data.
        """
        with self.store.work_lock(work_id):
            work = self.store.get_work(work_id)
            settings = ProbeSettings(executable=work['executable'], model=work['model'],
                                     timeout_seconds=timeout)
            entry = self.store.begin_turn(work_id, request_id, prompt)
            turn_id = entry['turn']['id']
            if entry['cached']:
                return {'work_id': work_id, 'turn_id': turn_id, 'cached': True,
                        'result': self.store.turn_result(turn_id)}

            def cancelled():
                return bool((should_cancel and should_cancel()) or self.store.is_cancelled(turn_id))

            try:
                result = self.transport(
                    settings, prompt, Path(work['workspace']),
                    resume_session_id=work['provider_session_id'],
                    on_started=lambda pid: self.store.on_process(turn_id, pid),
                    on_session=lambda sid: self.store.on_session(turn_id, sid),
                    should_cancel=cancelled,
                )
            except Exception:
                # The transport owns process cleanup in its finally block.
                # Preserve an honest interrupted record without raw diagnostics.
                result = {'status': 'blocked', 'reason': 'runner_error',
                          'session_id': None, 'text': ''}
            self.store.finish_turn(turn_id, result)
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
