"""Protected Linux transport binding for an existing independent review worker.

The model runs in a qualified empty workspace and receives material in its
prompt. The delivery guard still inspects the separate exact readonly export.
No claim, pairing, qualification, dispatch or publication happens here.
"""
from functools import partial
import threading

from .codex_turn import execute_turn
from .dev_manifest import DevelopmentError
from .git_delivery import LocalGitDeliveryController
from .git_workspace import _operation
from .linux_planner_policy import LinuxPlannerPolicy
from .review_worker import ReviewRuntimeGuard
from .runner import PersonalRunner


def fail():
    raise DevelopmentError('linux_review_binding_unconfirmed')


class LinuxDeliveryReviewBinding:
    """One thread-owned, explicitly prepared review transport.

    Pass ``delivery_guard=binding.guard`` and
    ``runner_factory=binding.runner_factory`` to DeliveryReviewWorker, then use
    its normal run method with this binding's workspace and executable. The
    existing worker continues to enforce its independent live review claim,
    exact material/context, Runtime reservation and cleanup proof.
    """

    def __init__(self, deliveries, *, operation_id, review_operation_id,
                 planner_policy_directory):
        if type(deliveries) is not LocalGitDeliveryController:
            fail()
        self.deliveries = deliveries
        self.operation_id = _operation(operation_id)
        self.review_operation_id = _operation(review_operation_id)
        self.thread = threading.get_ident()
        self.closed = False
        self.policy = LinuxPlannerPolicy(planner_policy_directory)
        try:
            self.fingerprint = self.policy.fingerprint
            self._workspace = self.policy.workspace
            self._executable = self.policy.executable
            self._export = self.policy._plan['checkout']
            self._check()
        except BaseException:
            self.policy.close()
            self.closed = True
            raise

    def __enter__(self):
        try:
            self._check()
            return self
        except BaseException:
            if threading.get_ident() == self.thread:
                self.close()
            raise

    def __exit__(self, *_):
        self.close()

    def __reduce_ex__(self, _):
        raise TypeError('Live Linux review bindings cannot be serialized')

    def _check(self):
        if (self.closed or threading.get_ident() != self.thread
                or type(self.policy) is not LinuxPlannerPolicy
                or self.policy.fingerprint != self.fingerprint
                or self.policy.workspace != self._workspace
                or self.policy.executable != self._executable
                or self.policy._plan.get('checkout') != self._export):
            fail()
        self.policy.validate_for(self._executable, self._workspace)

    @property
    def workspace(self):
        self._check()
        return self._workspace

    @property
    def executable(self):
        self._check()
        return self._executable

    def guard(self, *, scope, selection, context, material, workspace):
        self._check()
        if workspace != self._workspace:
            fail()
        # Both paths are pinned by the qualified policy. The concrete delivery
        # guard must independently confirm that its readonly export is exactly
        # this policy's checkout, with the selected SHA/material/context.
        result = self.deliveries.review_guard(operation_id=self.operation_id,
            review_operation_id=self.review_operation_id, scope=scope,
            selection=selection, context=context, material=material,
            workspace=self._export)
        self._check()
        if result is not True:
            fail()
        return True

    def runner_factory(self, store, *, runtime_guard):
        self._check()
        if type(runtime_guard) is not ReviewRuntimeGuard:
            fail()
        return PersonalRunner(store, partial(execute_turn, planner_policy=self.policy),
                              runtime_guard=runtime_guard)

    def close(self):
        if threading.get_ident() != self.thread:
            fail()
        if not self.closed:
            self.closed = True
            self.policy.close()
