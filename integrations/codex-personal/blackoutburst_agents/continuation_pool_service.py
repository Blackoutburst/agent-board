"""Explicit once-pass logical development continuation.

Only operator-selected tasks are visited. Pool jobs are preparation registries,
never dispatched here: each selected task owns its concrete handles in its own
thread, and actual model/tool Runtime guards still admit every execution.
An unfinished pass intent is evidence of uncertainty, never a replay request.
"""
from contextlib import ExitStack, closing, contextmanager
from contextvars import ContextVar
from copy import deepcopy
import fcntl
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .continuation_pool_configuration import load_continuation, MAX_CONFIGURATION
from .continuation import ContinuationTask, ContinuationChecks, PHASES
from .continuation_journal import ContinuationJournal, ContinuationError
from .board_reader import BoardReadError
from .context import ContextError
from .dev_journal import PrivateDirectory, decode, DevelopmentJournal
from .dev_manifest import Budgets, DevelopmentError, canonical, digest, identity, opaque, validate_manifest
from .dev_planner import _directory_pin, _reasoning_configuration
from .dev_planner_journal import PlannerBudgets
from .development_authorization import DevelopmentAuthorization
from .development_client import FAILURE_NOTICE_CODES
from .development_cycle_driver import DevelopmentCycleDriver, _test_caps
from .development_pool import DevelopmentPoolDriver
from .development_pool_selection import file_document
from .dev_runtime import development_work_id
from .git_workspace import LocalRepositoryGrant, LocalGitWorkspaceController
from .linux_input_snapshot import _path
from .linux_planner_policy import _executable_pin, linux_source_pins
from .linux_pool_preparation import LinuxPoolJobPreparer
from .linux_execution_qualification import qualification_diagnostic
from .planner_policy import PlannerPolicyError
from .pool_journal import uuid_value, PoolError
from .private_development_checkpoint import PrivateDevelopmentCheckpointController, checkpoint_id_for
from .run_store import StoreError
from .runtime_guard import RuntimeGuard, RuntimeControlError, RuntimeAdmissionRefused
from .work_journal import JournalError
from .worker_pool import WorkerPool

MAX_RECORD=512*1024
JOIN_SECONDS=30
STATES=frozenset(PHASES)|{'awaiting_independent_review','independent_review_published','backoff','not_started'}
REASONS=frozenset({'','continuation_pool_configuration_changed','continuation_pool_source_changed',
    'continuation_pool_tools_changed','continuation_pool_owner_busy','continuation_pool_pending',
    'continuation_pool_stopped','continuation_pool_cleanup_unknown','continuation_pool_target_failed',
    'continuation_pool_binding_changed','continuation_pool_api_unavailable','continuation_pool_clock_changed'})
PERMISSION_FAILURES=frozenset({'development_grants_unavailable','development_ticket_unavailable',
    'development_registration_mismatch'})

# Closed public error producers, frozen at development time. The provenance
# test scans public source; no source or owner file is read to classify a failure.
TARGET_FAILURE_CODES=frozenset({
    'abandonment_conflict', 'absent', 'action_not_allowed', 'aggregate_review_attachment_uncertain',
    'aggregate_review_command_refused', 'aggregate_review_concrete_source_required',
    'aggregate_review_fence_changed', 'aggregate_review_handoff_required',
    'aggregate_review_published_source_required', 'aggregate_review_reader_changed',
    'aggregate_review_reader_required', 'aggregate_review_source_changed',
    'aggregate_review_source_stopped', 'aggregate_review_writer_pending', 'availability_expired',
    'availability_in_future', 'availability_scope_mismatch', 'availability_unverified',
    'backend_controller_changed', 'backend_executable_changed', 'backend_executable_unavailable',
    'bounds', 'broker_busy', 'broker_disabled', 'canonical_checkout_busy', 'canonical_family_changed',
    'canonical_family_invalid', 'canonical_family_required', 'canonical_fence_changed',
    'canonical_operation_conflict', 'canonical_parent_conflict', 'catalog_changed', 'changed',
    'children', 'children_identity', 'cleanup_unconfirmed', 'cloud_workflow_already_bound',
    'cloud_workflow_cleanup_unconfirmed', 'cloud_workflow_configuration_changed',
    'cloud_workflow_connection_changed', 'cloud_workflow_delivery_history_changed',
    'cloud_workflow_distinct_reviewer_required', 'cloud_workflow_driver_collision',
    'cloud_workflow_no_selected_change', 'cloud_workflow_paths_outside_manifest',
    'cloud_workflow_pool_history_changed', 'cloud_workflow_publication_unconfirmed',
    'cloud_workflow_review_history_changed', 'cloud_workflow_review_material_too_large',
    'cloud_workflow_review_preparation_unconfirmed', 'cloud_workflow_review_publication_unconfirmed',
    'cloud_workflow_selected_job_changed', 'cloud_workflow_stop_unconfirmed', 'cloud_workflow_stopped',
    'cloud_workflow_tool_changed', 'conflict', 'connection_closed', 'connection_failed',
    'connection_too_large', 'connection_unavailable', 'context_changed', 'context_guard_capacity',
    'context_guard_changed', 'context_guard_invalid', 'context_guard_required',
    'context_guard_session_unbound', 'context_profile_mismatch', 'context_read_denied',
    'context_read_failed', 'context_scope_mismatch', 'context_too_large',
    'continuation_boundary_mismatch', 'continuation_boundary_required', 'continuation_boundary_unknown',
    'continuation_busy', 'continuation_check_result_invalid', 'continuation_checkpoint_refused',
    'continuation_checks_failed', 'continuation_clock_changed', 'continuation_clock_invalid',
    'continuation_closure_conflict', 'continuation_closure_invalid',
    'continuation_configuration_changed', 'continuation_cycle_limit', 'continuation_enrollment_conflict',
    'continuation_enrollment_invalid', 'continuation_enrollment_required',
    'continuation_fresh_directory_required', 'continuation_fresh_state_required',
    'continuation_fresh_work_required', 'continuation_human_channel_unavailable',
    'continuation_human_handoff_unconfirmed', 'continuation_invalid', 'continuation_journal_changed',
    'continuation_journal_corrupt', 'continuation_journal_schema', 'continuation_journal_unavailable',
    'continuation_permit_stale', 'continuation_pool_api_unavailable',
    'continuation_pool_binding_changed', 'continuation_pool_cleanup_unknown',
    'continuation_pool_clock_changed', 'continuation_pool_configuration_changed',
    'continuation_pool_configuration_invalid', 'continuation_pool_cycle_exceeds_batch_timeout',
    'continuation_pool_cycle_limits_invalid', 'continuation_pool_explicit_batch_required',
    'continuation_pool_owner_busy', 'continuation_pool_pending', 'continuation_pool_source_changed',
    'continuation_pool_stopped', 'continuation_pool_target_failed', 'continuation_pool_tools_changed',
    'continuation_postcondition', 'continuation_predecessor_invalid', 'continuation_process_changed',
    'continuation_provider_receipt_invalid', 'continuation_provider_unconfirmed',
    'continuation_quota_unconfirmed', 'continuation_request_conflict', 'continuation_review_changed',
    'continuation_review_cleanup_unknown', 'continuation_review_configuration_changed',
    'continuation_review_directory_exists', 'continuation_review_history_changed',
    'continuation_review_material_too_large', 'continuation_review_owner_changed',
    'continuation_review_paths_changed', 'continuation_review_pending',
    'continuation_review_preparation_unknown', 'continuation_review_publication_changed',
    'continuation_review_registry_conflict', 'continuation_review_roots_overlap',
    'continuation_review_selection_changed', 'continuation_review_source_required',
    'continuation_review_stopped', 'continuation_review_tools_changed', 'continuation_revision_conflict',
    'continuation_runner_requires_review', 'continuation_runtime_unknown', 'continuation_scope_changed',
    'continuation_start_refused', 'continuation_state_conflict', 'continuation_state_invalid',
    'continuation_state_too_large', 'continuation_stopped', 'continuation_store_opt_in_required',
    'continuation_store_optin_required', 'continuation_usage_regressed', 'continuation_wake_refused',
    'continuation_wake_required', 'controller_review', 'credential_fields_forbidden',
    'decision_hash_mismatch', 'decision_in_future', 'decision_memory_required',
    'decision_scope_mismatch', 'development_aggregate_authority_changed',
    'development_aggregate_authority_required', 'development_aggregate_board_publication_unconfirmed',
    'development_aggregate_caps_changed', 'development_aggregate_changed',
    'development_aggregate_completion_refused', 'development_aggregate_completion_required',
    'development_aggregate_completion_uncertain', 'development_aggregate_concrete_handles_required',
    'development_aggregate_controller_changed', 'development_aggregate_controller_conflict',
    'development_aggregate_fence_changed', 'development_aggregate_final_mode_refused',
    'development_aggregate_handoff_unconfirmed', 'development_aggregate_logical_stop',
    'development_aggregate_mode_changed', 'development_aggregate_not_sealed',
    'development_aggregate_parent_changed', 'development_aggregate_parent_required',
    'development_aggregate_public_base_changed', 'development_aggregate_publisher_required',
    'development_aggregate_result_limit', 'development_aggregate_roots_overlap',
    'development_aggregate_source_capture_changed', 'development_aggregate_source_capture_uncertain',
    'development_aggregate_tree_changed', 'development_aggregate_uncertain',
    'development_aggregate_unselected_change', 'development_backend_policy_mismatch',
    'development_backend_unqualified', 'development_checkpoint_lock_required',
    'development_checkpoint_request_invalid', 'development_checkpoint_unconfirmed',
    'development_checkpoint_work_busy', 'development_child_scope_changed', 'development_claim_changed',
    'development_claim_invalid', 'development_claim_lost', 'development_claim_too_short',
    'development_cleanup_identity_changed', 'development_cleanup_receipt_missing',
    'development_configuration_changed', 'development_connection_changed',
    'development_connection_overlap', 'development_connection_scope_mismatch',
    'development_connection_unavailable', 'development_context_already_sealed',
    'development_context_changed', 'development_context_changed_during_claim',
    'development_controller_required', 'development_controller_stopped', 'development_cycle_busy',
    'development_cycle_caps_changed', 'development_cycle_capture_uncertain',
    'development_cycle_check_result_invalid', 'development_cycle_claim_changed',
    'development_cycle_cleanup_unconfirmed', 'development_cycle_concrete_handles_required',
    'development_cycle_context_changed', 'development_cycle_execution_changed',
    'development_cycle_identity_reused', 'development_cycle_linux_preparation_required',
    'development_cycle_live_permit_required', 'development_cycle_logical_state_changed',
    'development_cycle_logical_stop', 'development_cycle_operation_changed',
    'development_cycle_parameters_changed', 'development_cycle_parent_changed',
    'development_cycle_preparation_changed', 'development_cycle_preparation_conflict',
    'development_cycle_preparation_uncertain', 'development_cycle_preparation_unconfirmed',
    'development_cycle_release_uncertain', 'development_cycle_request_conflict',
    'development_cycle_retirement_changed', 'development_cycle_retirement_required',
    'development_cycle_roots_reused', 'development_cycle_stage_refused',
    'development_cycle_stop_without_retirement', 'development_cycle_tools_changed',
    'development_cycle_unconfirmed', 'development_cycle_usage_unknown', 'development_cycle_wall_limit',
    'development_cycle_work_changed', 'development_delivery_controller_required',
    'development_delivery_linux_proof_required', 'development_delivery_proof_changed',
    'development_delivery_publication_incomplete', 'development_delivery_publication_required',
    'development_delivery_work_busy', 'development_directory_changed',
    'development_execution_profile_changed', 'development_execution_profile_invalid',
    'development_family_reservation_conflict', 'development_family_reservation_missing',
    'development_family_target_incomplete', 'development_fence_overlap',
    'development_foreign_or_expired_claim', 'development_frame_invalid_protocol',
    'development_frame_invalid_response', 'development_frame_unavailable',
    'development_git_authority_changed', 'development_git_authority_controller_required',
    'development_git_authority_unavailable', 'development_grants_unavailable',
    'development_implicit_registration_forbidden', 'development_input_preparation_is_not_cleanup_proof',
    'development_linux_configuration_changed', 'development_linux_journal_required',
    'development_linux_pair_mismatch', 'development_not_ready_for_publication',
    'development_notice_receipt_conflict', 'development_output_inside_checkout',
    'development_output_inside_planner', 'development_parent_changed',
    'development_planner_scope_mismatch', 'development_pool_absolute_path_required',
    'development_pool_cleanup_unconfirmed', 'development_pool_connection_changed',
    'development_pool_delivery_author_assignment_required',
    'development_pool_delivery_board_unconfirmed', 'development_pool_delivery_busy_or_incomplete',
    'development_pool_delivery_changed', 'development_pool_delivery_concrete_handles_required',
    'development_pool_delivery_existing_files_only', 'development_pool_delivery_fence_changed',
    'development_pool_delivery_fence_unavailable', 'development_pool_delivery_handoff_unconfirmed',
    'development_pool_delivery_incomplete', 'development_pool_delivery_intent_conflict',
    'development_pool_delivery_journal_pending', 'development_pool_delivery_limit',
    'development_pool_delivery_operation_missing', 'development_pool_delivery_private_receipt_changed',
    'development_pool_delivery_private_result_required',
    'development_pool_delivery_publication_unconfirmed', 'development_pool_delivery_receipt_changed',
    'development_pool_delivery_record_changed', 'development_pool_delivery_record_unavailable',
    'development_pool_delivery_roots_overlap', 'development_pool_delivery_runtime_unconfirmed',
    'development_pool_delivery_scope_mismatch', 'development_pool_delivery_text_refused',
    'development_pool_delivery_unavailable', 'development_pool_file_changed',
    'development_pool_linux_profile_required', 'development_pool_registry_changed',
    'development_pool_runtime_scope_mismatch', 'development_pool_runtime_unconfirmed',
    'development_pool_selection_already_bound', 'development_pool_selection_mismatch',
    'development_pool_stop_unconfirmed', 'development_pool_stopped', 'development_pool_unqualified',
    'development_pool_work_scope_mismatch', 'development_prepare_conflict',
    'development_private_overlap', 'development_provider_busy',
    'development_provider_new_generation_required', 'development_provider_process_changed',
    'development_provider_request_conflict', 'development_provider_retirement_required',
    'development_provider_run_uncertain', 'development_provider_session_required',
    'development_provider_unconfirmed', 'development_provider_wake_changed',
    'development_provider_wake_conflict', 'development_provider_wake_required',
    'development_provider_wake_uncertain', 'development_provider_wake_unconfirmed',
    'development_publication_conflict', 'development_publication_unconfirmed',
    'development_registration_already_pinned', 'development_registration_mismatch',
    'development_registration_required', 'development_renewal_unconfirmed',
    'development_review_handoff_author_assignment_required', 'development_review_handoff_changed',
    'development_review_handoff_context_changed', 'development_review_handoff_controller_required',
    'development_review_handoff_intent_changed', 'development_review_handoff_permission_denied',
    'development_review_handoff_publication_required', 'development_review_handoff_unavailable',
    'development_review_handoff_unconfirmed', 'development_stage_requires_review',
    'development_stop_corrupt', 'development_stop_requested',
    'development_ticket_frame_invalid_response', 'development_ticket_frame_unavailable',
    'development_ticket_unavailable', 'development_tool_not_allowed', 'directory_access_refused',
    'directory_not_exclusive', 'duplicate_document_key', 'executed_disposition_clock_invalid',
    'executed_disposition_postcondition', 'executed_disposition_predecessor_invalid',
    'explicit_runtime_abandonment_required', 'failure_notice_unconfirmed', 'family_accept_audit_changed',
    'family_accept_unconfirmed', 'family_audit_budget_exhausted', 'family_audit_receipt_changed',
    'family_authorization_changed', 'family_authorization_scope_changed', 'family_bindings_required',
    'family_budget_overallocated', 'family_busy', 'family_candidate_not_utf8',
    'family_candidate_too_large', 'family_child_publication_unconfirmed', 'family_child_requires_review',
    'family_child_scope_changed', 'family_cleanup_unconfirmed', 'family_clock_invalid',
    'family_configuration_changed', 'family_configuration_conflict', 'family_connection_scope_changed',
    'family_controller_overlap', 'family_controller_sources_changed',
    'family_controller_sources_too_large', 'family_corrupt', 'family_deadline',
    'family_directory_changed', 'family_document_too_large', 'family_execution_profile_mismatch',
    'family_fence_incomplete', 'family_file_caps_overlap', 'family_finalize_audit_changed',
    'family_finalize_unconfirmed', 'family_initial_files_changed',
    'family_input_preparation_is_not_test_proof', 'family_intent_conflict', 'family_lock_changed',
    'family_lock_required', 'family_lock_unavailable', 'family_manifest_caps_changed',
    'family_manifest_changed', 'family_not_open', 'family_open_unconfirmed',
    'family_parent_instruction_too_large', 'family_parent_not_ready',
    'family_parent_publication_unconfirmed', 'family_parent_requires_review', 'family_private_overlap',
    'family_qualification_changed', 'family_qualification_scope_invalid', 'family_read_protocol_changed',
    'family_registrations_required', 'family_revision_conflict', 'family_roles_ambiguous',
    'family_root_already_delegated', 'family_root_authorization_changed', 'family_root_changed',
    'family_root_overlap', 'family_root_source_changed', 'family_scope_mismatch',
    'family_server_plan_changed', 'family_session_overlap', 'family_source_snapshot_changed',
    'family_stop_corrupt', 'family_stop_requested', 'family_stop_unconfirmed', 'family_stopped',
    'family_target_conflict', 'family_test_proof_changed', 'family_test_proof_missing',
    'family_work_identity_changed', 'family_workflow_intent_changed', 'family_workflow_not_review_ready',
    'fence_busy', 'fence_corrupt', 'fence_directory_unavailable', 'fence_full', 'fence_lock_changed',
    'fence_wait_context_invalid', 'fence_wait_family_forbidden', 'file_access_refused', 'file_changed',
    'file_not_text', 'file_not_utf8', 'file_too_large', 'file_type_not_allowed',
    'final_test_proof_missing', 'final_test_receipt_changed', 'finite_pool_batch_busy',
    'finite_pool_batch_conflict', 'finite_pool_batch_incomplete', 'finite_pool_batch_invalid',
    'finite_pool_concrete_workflow_required', 'finite_pool_configuration_changed',
    'finite_pool_connection_changed', 'finite_pool_jobs_not_distinct', 'finite_pool_path_refused',
    'finite_pool_preparation_unconfirmed', 'finite_pool_record_directory_closed',
    'finite_pool_record_limit', 'finite_pool_root_changed', 'finite_pool_source_changed',
    'finite_pool_stop_unconfirmed', 'finite_pool_stopped', 'finite_pool_tool_changed',
    'finite_pool_workflow_history_changed', 'git_delivery_base_refused', 'git_delivery_busy',
    'git_delivery_changes_refused', 'git_delivery_commit_changed', 'git_delivery_controller_required',
    'git_delivery_distinct_reviewer_required', 'git_delivery_hooks_refused',
    'git_delivery_index_changed', 'git_delivery_interrupted_or_changed', 'git_delivery_invalid',
    'git_delivery_material_refused', 'git_delivery_message_refused', 'git_delivery_not_readonly',
    'git_delivery_not_sealed', 'git_delivery_object_changed', 'git_delivery_path_alias',
    'git_delivery_payload_changed', 'git_delivery_postimage_changed', 'git_delivery_record_refused',
    'git_delivery_request_conflict', 'git_delivery_review_changed',
    'git_delivery_review_interrupted_or_changed', 'git_delivery_review_selection_changed',
    'git_delivery_root_changed', 'git_delivery_roots_overlap', 'git_delivery_scope_changed',
    'git_delivery_snapshot_changed', 'git_delivery_test_evidence_required',
    'git_delivery_test_evidence_unconfirmed', 'git_delivery_tree_changed', 'git_delivery_tree_refused',
    'git_delivery_tree_too_large', 'git_delivery_undeclared_change', 'git_delivery_workspace_not_ready',
    'git_publication_authentication_changed', 'git_publication_authentication_location_refused',
    'git_publication_authentication_unavailable', 'git_publication_authority_changed',
    'git_publication_authority_refused', 'git_publication_authority_required',
    'git_publication_authority_unconfirmed', 'git_publication_base_changed',
    'git_publication_base_refused', 'git_publication_branch_occupied',
    'git_publication_branch_unconfirmed', 'git_publication_command_unconfirmed',
    'git_publication_controller_required', 'git_publication_copy_retained',
    'git_publication_create_retained', 'git_publication_destination_refused',
    'git_publication_input_changed', 'git_publication_invalid', 'git_publication_pin_changed',
    'git_publication_pr_ambiguous', 'git_publication_pr_mismatch', 'git_publication_push_retained',
    'git_publication_readonly_view', 'git_publication_record_changed', 'git_publication_record_refused',
    'git_publication_remote_invalid', 'git_publication_remote_unavailable',
    'git_publication_repository_unavailable', 'git_publication_request_conflict',
    'git_publication_roots_overlap', 'git_publication_scope_changed', 'git_publication_sha_refused',
    'git_publication_text_refused', 'git_publication_timeout_refused',
    'git_publication_tool_location_refused', 'git_publication_tool_mismatch',
    'git_publication_tool_unavailable', 'git_publication_transport_changed',
    'git_workspace_base_refused', 'git_workspace_busy', 'git_workspace_checkout_changed',
    'git_workspace_command_failed', 'git_workspace_command_timeout', 'git_workspace_copy_layout_refused',
    'git_workspace_directory_refused', 'git_workspace_file_changed', 'git_workspace_file_refused',
    'git_workspace_git_refused', 'git_workspace_grant_refused',
    'git_workspace_interrupted_requires_review', 'git_workspace_invalid', 'git_workspace_not_ready',
    'git_workspace_operation_refused', 'git_workspace_output_limit', 'git_workspace_owner_refused',
    'git_workspace_path_refused', 'git_workspace_pin_changed', 'git_workspace_record_refused',
    'git_workspace_request_conflict', 'git_workspace_roots_overlap', 'git_workspace_scope_refused',
    'git_workspace_source_layout_refused', 'git_workspace_source_too_large',
    'git_workspace_timeout_refused', 'git_workspace_tree_refused', 'git_workspace_tree_too_large',
    'guard', 'guard_required', 'history', 'host_authorization_changed', 'host_stopped',
    'human_stop_required', 'idempotency_conflict', 'identifier', 'identity', 'immutable_queue_identity',
    'insecure_connection_file', 'insecure_fence_ancestor', 'insecure_fence_directory',
    'insecure_journal_directory', 'insecure_journal_file', 'insecure_policy_file',
    'insecure_private_directory', 'insecure_private_file', 'insecure_runtime_directory',
    'insecure_state_directory', 'insecure_state_file', 'instance', 'interruption_state_conflict',
    'invalid', 'invalid_actions', 'invalid_arguments', 'invalid_backend_executable',
    'invalid_backend_nonlaunch_receipt', 'invalid_board_origin', 'invalid_budget',
    'invalid_configuration', 'invalid_connection', 'invalid_context_hash', 'invalid_context_limits',
    'invalid_context_pagination', 'invalid_context_source', 'invalid_continuation_configuration',
    'invalid_controller', 'invalid_decision', 'invalid_development_controller',
    'invalid_development_publication', 'invalid_directory', 'invalid_document', 'invalid_existing_only',
    'invalid_failure_notice', 'invalid_family_budget', 'invalid_family_configuration',
    'invalid_family_directory', 'invalid_family_files', 'invalid_family_plan',
    'invalid_family_workflows', 'invalid_fence_directory', 'invalid_files', 'invalid_final_summary',
    'invalid_journal_file', 'invalid_linux_test_protocol', 'invalid_lock_mode', 'invalid_manifest',
    'invalid_operation_id', 'invalid_patch', 'invalid_path', 'invalid_payload', 'invalid_planner_action',
    'invalid_planner_budget', 'invalid_planner_budget_charge', 'invalid_planner_clock',
    'invalid_planner_context', 'invalid_planner_controller', 'invalid_planner_instruction',
    'invalid_planner_parent', 'invalid_planner_reasoning_effort', 'invalid_policy', 'invalid_process_id',
    'invalid_prompt', 'invalid_resolution', 'invalid_response', 'invalid_result',
    'invalid_reviewer_registration', 'invalid_revision', 'invalid_routing_configuration',
    'invalid_runtime_identity', 'invalid_runtime_phase', 'invalid_runtime_provider', 'invalid_scope',
    'invalid_session', 'invalid_session_id', 'invalid_session_or_timeout', 'invalid_sha256',
    'invalid_signal_canary', 'invalid_stage', 'invalid_stage_transition', 'invalid_state_file',
    'invalid_task_class', 'invalid_test_admission', 'invalid_test_budget', 'invalid_test_name',
    'invalid_test_plan', 'invalid_test_receipt', 'invalid_test_request', 'invalid_tests',
    'invalid_timeout', 'invalid_transport_capabilities', 'invalid_trusted_script_pin',
    'invalid_work_paths', 'invalid_worker_absence', 'journal_access_failed', 'journal_closed',
    'journal_corrupt', 'journal_full', 'journal_inside_workspace', 'journal_lock_changed',
    'journal_lock_lease_invalid', 'journal_lock_unavailable', 'journal_locked', 'journal_unavailable',
    'legacy_mutation', 'linked_work_scope_mismatch', 'linux_authorization_expired', 'linux_backend_busy',
    'linux_backend_identity_changed', 'linux_backend_lock_changed', 'linux_capture_count_changed',
    'linux_execution_attempt_required', 'linux_execution_context_expired',
    'linux_execution_context_mismatch', 'linux_execution_context_required',
    'linux_execution_directory_overlap', 'linux_execution_environment_changed',
    'linux_execution_identity_cleanup_required', 'linux_execution_interpreter_changed',
    'linux_execution_invalid', 'linux_execution_plan_changed', 'linux_execution_plan_limit',
    'linux_execution_proof_mismatch', 'linux_execution_runtime_limit', 'linux_execution_script_changed',
    'linux_execution_source_changed', 'linux_execution_state_changed', 'linux_execution_step_changed',
    'linux_execution_unqualified', 'linux_identity_ack_missing', 'linux_input_admission_invalid',
    'linux_input_attempt_not_held', 'linux_input_attempt_not_reusable', 'linux_input_boundary_order',
    'linux_input_capture_permit_invalid', 'linux_input_directory_changed',
    'linux_input_fresh_directory_required', 'linux_input_fresh_journal_required',
    'linux_input_only_journal', 'linux_input_payload_changed', 'linux_input_plan_invalid',
    'linux_input_reconciliation_forbidden', 'linux_input_state_changed', 'linux_job_already_attempted',
    'linux_kernel_guard_failed', 'linux_namespace_identity_invalid', 'linux_namespace_invalid',
    'linux_native_cleanup_unconfirmed', 'linux_native_receipt_missing', 'linux_output_limit',
    'linux_planner_attempt_not_reusable', 'linux_planner_canary_created_file',
    'linux_planner_canary_output_invalid', 'linux_planner_cli_version_changed',
    'linux_planner_cli_version_invalid', 'linux_planner_environment_unconfirmed',
    'linux_planner_executable_unconfirmed', 'linux_planner_fresh_directory_required',
    'linux_planner_native_policy_unqualified', 'linux_planner_plan_too_large',
    'linux_planner_policy_busy', 'linux_planner_policy_changed', 'linux_planner_policy_closed',
    'linux_planner_policy_scope_mismatch', 'linux_planner_policy_unavailable',
    'linux_planner_policy_unqualified', 'linux_planner_qualification_cleanup_unconfirmed',
    'linux_planner_sources_unconfirmed', 'linux_pool_backend_unqualified',
    'linux_pool_concrete_handles_required', 'linux_pool_driver_mismatch',
    'linux_pool_environment_changed', 'linux_pool_explicit_budgets_required',
    'linux_pool_inputs_invalid', 'linux_pool_job_changed', 'linux_pool_job_not_queued',
    'linux_pool_preparation_busy', 'linux_pool_preparation_changed', 'linux_pool_preparation_incomplete',
    'linux_pool_preparation_limit', 'linux_pool_registration_changed', 'linux_pool_request_conflict',
    'linux_pool_selection_changed', 'linux_pool_source_changed', 'linux_pool_ticket_not_ready',
    'linux_pool_verifier_changed', 'linux_pool_verifier_invalid',
    'linux_qualification_already_attempted', 'linux_qualification_case_invalid',
    'linux_qualification_cleanup_unconfirmed', 'linux_qualification_concrete_backend_required',
    'linux_qualification_corruption_not_refused', 'linux_qualification_document_changed',
    'linux_qualification_driver_refused', 'linux_qualification_environment_changed',
    'linux_qualification_eof', 'linux_qualification_evidence_changed',
    'linux_qualification_fault_message_unconfirmed', 'linux_qualification_fault_not_observed',
    'linux_qualification_files_changed', 'linux_qualification_guards_unconfirmed',
    'linux_qualification_job_changed', 'linux_qualification_matrix_expired',
    'linux_qualification_native_changed', 'linux_qualification_native_missing',
    'linux_qualification_observer_init_unconfirmed', 'linux_qualification_observer_required',
    'linux_qualification_origin_changed', 'linux_qualification_outcome_changed',
    'linux_qualification_process_unconfirmed', 'linux_qualification_protocol_limit',
    'linux_qualification_protocol_timeout', 'linux_qualification_protocol_unconfirmed',
    'linux_qualification_ready_state_changed', 'linux_qualification_real_host_required',
    'linux_qualification_replay_changed_job', 'linux_qualification_replay_not_refused',
    'linux_qualification_request_changed', 'linux_qualification_source_ack_changed',
    'linux_qualification_source_revoked', 'linux_qualification_source_unconfirmed',
    'linux_qualification_threads_unconfirmed', 'linux_qualification_unconfirmed',
    'linux_qualification_unexpected_native_receipt', 'linux_result_boundary_invalid',
    'linux_review_binding_unconfirmed', 'linux_runtime_changed', 'linux_runtime_inventory_limit',
    'linux_script_wall_expired', 'linux_source_ack_missing', 'linux_source_boundary_missing',
    'linux_source_boundary_replayed', 'linux_source_boundary_unconfirmed', 'linux_source_window_invalid',
    'linux_startup_expired', 'linux_supervisor_changed', 'linux_supervisor_cleanup_unconfirmed',
    'linux_supervisor_lost', 'local_intent_required', 'local_runtime_reason_forbidden', 'lock',
    'managed_protocol_invalid', 'managed_registration_forbidden', 'managed_required', 'manifest_changed',
    'manifest_too_large', 'message_context_changed', 'message_context_guard_required',
    'model_not_allowed', 'model_policy_required', 'model_unavailable', 'native_reconciliation_required',
    'native_test_platform_unqualified', 'no_interruption', 'operation_id_conflict',
    'operation_not_found', 'original_proposal_required', 'outbox', 'outbox_changed',
    'override_model_required', 'parent_changed', 'patch_hash_conflict', 'patch_reconciliation_uncertain',
    'patch_write_uncertain', 'path_not_allowed', 'pending', 'permission_revoked', 'plan',
    'planner_authorization_changed', 'planner_backend_policy_mismatch', 'planner_boundary_overlap',
    'planner_busy', 'planner_cli_version_changed', 'planner_cli_version_invalid',
    'planner_clock_changed', 'planner_controller_stopped', 'planner_directory_changed',
    'planner_disabled', 'planner_effect_requires_review', 'planner_fence_overlap',
    'planner_input_only_journal_forbidden', 'planner_journal_corrupt', 'planner_journal_full',
    'planner_linux_pair_mismatch', 'planner_lock_changed', 'planner_lock_unavailable',
    'planner_model_receipt_unavailable', 'planner_model_requires_review',
    'planner_native_policy_unqualified', 'planner_parent_operation_required',
    'planner_platform_unqualified', 'planner_policy_busy', 'planner_policy_changed',
    'planner_policy_closed', 'planner_policy_scope_mismatch', 'planner_policy_unavailable',
    'planner_policy_unqualified', 'planner_prompt_too_large',
    'planner_qualification_cleanup_unconfirmed', 'planner_reasoning_effort_changed',
    'planner_reconciliation_required', 'planner_recovered_effect_requires_review',
    'planner_response_too_large', 'planner_runtime_scope_changed', 'planner_runtime_stopped',
    'planner_scope_mismatch', 'planner_stop_corrupt', 'planner_stop_requested',
    'planner_supervised_backend_required', 'planner_tool_finish_unconfirmed',
    'planner_tool_receipt_unavailable', 'planner_wall_budget_exhausted', 'planner_workspace_changed',
    'planner_workspace_invalid', 'planner_workspace_not_empty_or_private', 'planner_workspace_overlap',
    'policy_changed', 'policy_scope_mismatch', 'policy_too_large', 'policy_unavailable',
    'preferred_provider_unsupported', 'private_checkpoint_alternative_key_refused',
    'private_checkpoint_base_changed', 'private_checkpoint_busy',
    'private_checkpoint_configuration_changed', 'private_checkpoint_configuration_refused',
    'private_checkpoint_controller_required', 'private_checkpoint_head_changed',
    'private_checkpoint_head_or_index_changed', 'private_checkpoint_index_changed',
    'private_checkpoint_interrupted_requires_review', 'private_checkpoint_invalid',
    'private_checkpoint_lock_missing_or_changed', 'private_checkpoint_mode_changed',
    'private_checkpoint_object_changed', 'private_checkpoint_parent_changed',
    'private_checkpoint_parent_required', 'private_checkpoint_payload_changed',
    'private_checkpoint_postimage_changed', 'private_checkpoint_producer_unavailable',
    'private_checkpoint_proof_changed', 'private_checkpoint_proof_required',
    'private_checkpoint_record_changed', 'private_checkpoint_ref_changed',
    'private_checkpoint_request_conflict', 'private_checkpoint_root_changed',
    'private_checkpoint_roots_overlap', 'private_checkpoint_scope_changed',
    'private_checkpoint_source_changed', 'private_checkpoint_tree_changed',
    'private_checkpoint_undeclared_change', 'private_checkpoint_workspace_changed',
    'private_directory_changed', 'private_directory_unavailable', 'private_document_changed',
    'private_document_too_large', 'private_file_exists', 'private_path', 'private_read_failed',
    'private_write_failed', 'process_conflict', 'process_state_unknown', 'process_still_running',
    'proof', 'protocol_mismatch', 'provider_disposition_requires_review', 'provider_not_supported',
    'read_bytes_budget_exhausted', 'reasoning_not_allowed', 'reasoning_not_supported',
    'receipt_conflict', 'reconciliation_required', 'redirect_blocked', 'request_configuration_conflict',
    'request_conflict', 'requires_stopped', 'resolution_conflict', 'response_too_large',
    'result_not_ready', 'result_too_large', 'review_authorization_requires_reconciliation',
    'review_controller_guard_required', 'review_controller_tool_not_allowed',
    'review_lifecycle_activity_unconfirmed', 'review_lifecycle_busy_or_incomplete',
    'review_lifecycle_claim_unconfirmed', 'review_lifecycle_completed_review_required',
    'review_lifecycle_concrete_handles_required', 'review_lifecycle_connection_changed',
    'review_lifecycle_context_changed', 'review_lifecycle_driver_changed',
    'review_lifecycle_intent_changed', 'review_lifecycle_invalid_request',
    'review_lifecycle_journal_pending', 'review_lifecycle_opinion_changed',
    'review_lifecycle_opinion_incomplete', 'review_lifecycle_permission_denied',
    'review_lifecycle_preparation_unconfirmed', 'review_lifecycle_publication_event_unconfirmed',
    'review_lifecycle_publication_unconfirmed', 'review_lifecycle_queued_job_required',
    'review_lifecycle_receipt_changed', 'review_lifecycle_record_changed',
    'review_lifecycle_record_limit', 'review_lifecycle_roots_changed', 'review_lifecycle_roots_overlap',
    'review_lifecycle_runtime_unconfirmed', 'review_lifecycle_source_changed',
    'review_lifecycle_source_ticket_changed', 'review_lifecycle_stop_record_unconfirmed',
    'review_lifecycle_stopped', 'review_lifecycle_ticket_changed', 'review_lifecycle_unconfirmed',
    'review_pool_cleanup_unconfirmed', 'review_pool_concrete_source_required',
    'review_pool_connection_changed', 'review_pool_operation_changed', 'review_pool_opinion_incomplete',
    'review_pool_policy_changed', 'review_pool_registration_required', 'review_pool_resources_required',
    'review_pool_runtime_scope_changed', 'review_pool_runtime_unconfirmed',
    'review_pool_selection_changed', 'review_pool_selection_limit',
    'review_pool_source_attachment_required', 'review_pool_source_changed',
    'review_pool_source_incomplete', 'review_pool_stopped', 'review_publication_only',
    'reviewer_registration_required', 'reviewer_tool_not_allowed', 'revision', 'revision_conflict',
    'rpc_rejected', 'runtime_abandonment_conflict', 'runtime_already_abandoned',
    'runtime_already_finished', 'runtime_already_refused', 'runtime_cached_turn_unregistered',
    'runtime_capacity_exhausted', 'runtime_cleanup_unconfirmed', 'runtime_configuration_conflict',
    'runtime_development_scope_mismatch', 'runtime_existing_turn_requires_review',
    'runtime_finish_conflict', 'runtime_finish_requires_review', 'runtime_finish_unconfirmed',
    'runtime_identity_changed', 'runtime_journal_corrupt', 'runtime_model_required',
    'runtime_not_admitted', 'runtime_operation_not_found', 'runtime_receipt_invalid',
    'runtime_refusal_identity_unconfirmed', 'runtime_refusal_invalid', 'runtime_refusal_result_conflict',
    'runtime_registration_forbidden', 'runtime_reservation_requires_review',
    'runtime_reservation_unconfirmed', 'runtime_result_invalid', 'runtime_scope_mismatch',
    'runtime_seal_invalid', 'runtime_start_requires_review', 'runtime_start_unconfirmed',
    'runtime_turn_requires_review', 'schema', 'scope_busy', 'scope_configuration_conflict',
    'scope_has_unfinished_operation', 'scope_lock_required', 'scope_lock_unavailable',
    'secret_content_refused', 'selection_not_authorized', 'selection_permission_denied',
    'selection_proof_mismatch', 'selection_unconfirmed', 'session_conflict', 'session_mismatch',
    'session_not_recorded', 'session_scope_conflict', 'signal_canary_unavailable', 'slot_changed',
    'source', 'stage_cleanup_requires_review', 'state', 'state_schema_invalid', 'state_sidecar_present',
    'state_unavailable', 'store_closed', 'supervisor_authorization_expired',
    'supervisor_boundary_unknown', 'supervisor_clock_changed', 'supervisor_clock_unavailable',
    'supervisor_control_invalid', 'supervisor_control_write_failed', 'supervisor_controller_changed',
    'supervisor_controller_unavailable', 'supervisor_journal_corrupt', 'supervisor_workspace_overlap',
    'team_manager_cycle_refused', 'team_role_connection_changed', 'team_role_context_changed',
    'team_role_grants_changed', 'team_role_inputs_changed', 'team_role_inputs_too_large',
    'team_role_interrupted', 'team_role_invalid', 'team_role_output_invalid', 'team_role_policy_changed',
    'team_role_prompt_too_large', 'team_role_registry_changed', 'team_role_result_unconfirmed',
    'team_role_selection_conflict', 'team_role_stopped', 'team_writer_concrete_source_required',
    'team_writer_reference_changed', 'team_writer_review_incomplete', 'team_writer_source_changed',
    'team_writer_source_thread_required', 'test_admission_requires_review', 'test_backend_access_failed',
    'test_backend_busy', 'test_backend_journal_corrupt', 'test_backend_journal_full',
    'test_backend_lock_changed', 'test_backend_lock_unavailable', 'test_backend_unqualified',
    'test_not_allowed', 'test_outcome_uncertain', 'test_plan_changed', 'test_plan_scope_mismatch',
    'test_plan_too_large', 'tests_budget_exhausted', 'timeout', 'tls_unavailable', 'tool_not_allowed',
    'tool_rejected', 'transaction', 'transaction_active', 'trusted_script_access_refused',
    'trusted_script_changed', 'trusted_script_directory_refused', 'trusted_script_in_patch_caps',
    'turn_not_found', 'turn_not_running', 'turn_still_running', 'unsettled',
    'unsupported_journal_version', 'unsupported_platform', 'unsupported_policy_version',
    'unsupported_runtime_journal', 'unsupported_state_version', 'usage_connection_mismatch', 'work_busy',
    'work_closed', 'work_identity_conflict', 'work_lock_required', 'work_lock_unavailable',
    'work_locked', 'work_not_found', 'worker_not_found', 'worker_not_resolved', 'worker_not_successful',
    'worker_proof_mismatch', 'worker_proof_required', 'workspace_identity_changed',
    'workspace_not_exclusive', 'write_set',
})|FAILURE_NOTICE_CODES
TARGET_FAILURE_STAGES=frozenset({'unknown','target_entry','target_binding','cursor_read',
    'historical_review','previous_generation_open','previous_state_check','provider_availability',
    'next_generation_intent','next_generation_create','initial_preparation','previous_retirement',
    'next_preparation','worker_open','task_open','cycle_run','provider_wake','cycle_retirement',
    'aggregate_publication','result_checkpoint','cursor_commit'})
_TARGET_OBSERVATION=ContextVar('continuation_target_failure_observation',default=None)
_CODE_ERROR_TYPES=((DevelopmentError,'development_error'),(ContinuationError,'continuation_error'),
    (PlannerPolicyError,'planner_policy_error'),(BoardReadError,'board_read_error'),
    (JournalError,'journal_error'),(StoreError,'store_error'),(ContextError,'context_error'),
    (RuntimeControlError,'runtime_control_error'),(RuntimeAdmissionRefused,'runtime_admission_refused'))
_FIXED_ERROR_TYPES=((PoolError,'pool_error'),(PermissionError,'permission_denied'),
    (FileNotFoundError,'file_not_found'),(FileExistsError,'file_exists'),
    (OSError,'os_error'),(NotADirectoryError,'os_error'),(IsADirectoryError,'os_error'),
    (BlockingIOError,'os_error'),(InterruptedError,'os_error'),(BrokenPipeError,'os_error'),
    (TimeoutError,'os_error'),(ConnectionError,'os_error'),(ConnectionAbortedError,'os_error'),
    (ConnectionRefusedError,'os_error'),(ConnectionResetError,'os_error'),
    (sqlite3.Error,'sqlite_error'),(sqlite3.DatabaseError,'sqlite_error'),
    (sqlite3.OperationalError,'sqlite_error'),(sqlite3.IntegrityError,'sqlite_error'),
    (sqlite3.ProgrammingError,'sqlite_error'),(sqlite3.InterfaceError,'sqlite_error'),
    (sqlite3.DataError,'sqlite_error'),(sqlite3.InternalError,'sqlite_error'),
    (sqlite3.NotSupportedError,'sqlite_error'),(KeyError,'missing_key'),
    (TypeError,'invalid_type'),(ValueError,'invalid_value'),(RuntimeError,'runtime_error'))


def _failure_uuid(value):
    if type(value) is not str:return None
    parts=value.split('-')
    return value if tuple(map(len,parts))==(8,4,4,4,12) and all(
        char in '0123456789abcdef' for part in parts for char in part) else None


class _TargetObservation:
    """Transient correlation only; never a permit or retained SDK handle."""
    __slots__=('logical_task_id','stage','generation_id','operation_id')
    def __init__(self,logical_task_id):
        self.logical_task_id=_failure_uuid(logical_task_id);self.stage='target_entry'
        self.generation_id=None;self.operation_id=None


def _observe_stage(stage):
    observation=_TARGET_OBSERVATION.get()
    if type(observation) is _TargetObservation:
        observation.stage=stage if type(stage) is str and stage in TARGET_FAILURE_STAGES else 'unknown'


def _observe_identity(generation_id,operation_id=None):
    observation=_TARGET_OBSERVATION.get()
    if type(observation) is _TargetObservation:
        observation.generation_id=_failure_uuid(generation_id)
        observation.operation_id=_failure_uuid(operation_id)


def _target_failure(error,observation):
    """No arbitrary exception attributes, text, args or paths enter stdout."""
    category,code='unknown_error','unrecognized_code'
    for cls,label in _CODE_ERROR_TYPES:
        if type(error) is cls:
            category=label;candidate=vars(error).get('code')
            if type(candidate) is str and candidate in TARGET_FAILURE_CODES:code=candidate
            break
    else:
        for cls,label in _FIXED_ERROR_TYPES:
            if type(error) is cls:
                category=label;code=label
                break
    value={'schema':'continuation-target-failure-v1','stage':'unknown','category':category,'code':code,
        'logical_task_id':None,'generation_id':None,'operation_id':None}
    if type(observation) is _TargetObservation:
        value.update(stage=observation.stage if type(observation.stage) is str and
            observation.stage in TARGET_FAILURE_STAGES else 'unknown',
            **{key:_failure_uuid(getattr(observation,key)) for key in
               ('logical_task_id','generation_id','operation_id')})
    if type(error) is DevelopmentError:
        qualification=qualification_diagnostic(vars(error).get('_linux_backend_qualification_diagnostic'))
        if qualification is not None:value['qualification']=qualification
    return value


def fail(code='continuation_pool_target_failed'):raise DevelopmentError(code)


def require(value,code='continuation_pool_target_failed'):
    if not value:fail(code)


def _read(private,name):
    raw=private.read(name,MAX_RECORD);value=decode(raw)
    require(canonical(value)==raw,'continuation_pool_binding_changed')
    return value


def _write(private,name,value,*,replace=False):
    raw=canonical(value);require(len(raw)<=MAX_RECORD)
    (private.replace if replace else private.create)(name,raw)


def _mkdir(path):
    # Callers supply a compiled child of held private roots, never Board text.
    os.mkdir(path,0o700)
    with PrivateDirectory(Path(path).parent) as parent:os.fsync(parent.fd)
    with PrivateDirectory(path):pass


@contextmanager
def _existing_work_database(directory,expected=None):
    """Hold the existing owned DB inode across the normal journal opening."""
    with PrivateDirectory(directory) as private:
        fd=os.open('work-journal.sqlite3',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=private.fd)
        try:
            private.validate_file(fd);pin=identity(os.fstat(fd))
            require(0<os.fstat(fd).st_size<=32*1024*1024)
            if expected is not None:require(pin==expected,'continuation_pool_cleanup_unknown')
            require(identity(os.stat('work-journal.sqlite3',dir_fd=private.fd,follow_symlinks=False))==pin)
            yield pin
            private.verify();private.validate_file(fd)
            require(identity(os.fstat(fd))==pin==identity(os.stat('work-journal.sqlite3',dir_fd=private.fd,follow_symlinks=False)),
                'continuation_pool_cleanup_unknown')
        finally:os.close(fd)


class _Pass:
    """Cancellation controls never invoke author SQLite across threads."""
    def __init__(self,service,should_stop):
        self.service,self.should_stop=service,should_stop
        self.deadline=time.monotonic()+min(service.max_seconds,service.loaded['batch']['timeout_seconds'])
        self.cancel=threading.Event();self.mutex=threading.Lock();self.pools=[];self.workers=[];self.reviewers=[];self.stop_failed=False
        self.human_reports={}
    def stopping(self):
        if self.cancel.is_set():return True
        try:
            self.service._pins()
            value=time.monotonic()>=self.deadline or bool(self.should_stop and self.should_stop())
        except Exception:value=True
        if value:self.stop()
        return value
    def local_guard(self):
        # A stop-only predicate. Never perform cross-pool Stop or RPC while
        # the preparer may be inspecting its queue under the metadata mutex.
        try:requested=bool(self.should_stop and self.should_stop())
        except Exception:requested=True
        if self.cancel.is_set() or time.monotonic()>=self.deadline or requested:
            fail('continuation_pool_stopped')
    def guard(self):
        if self.stopping():fail('continuation_pool_stopped')
    def add_pool(self,pool):
        with self.mutex:self.pools.append(pool);cancelled=self.cancel.is_set()
        if cancelled:pool.stop()
    def remove_pool(self,pool):
        with self.mutex:
            if pool in self.pools:self.pools.remove(pool)
    def add_reviewer(self,reviewer):
        from .continuation_aggregate_review import ContinuationAggregateReviewer
        require(type(reviewer) is ContinuationAggregateReviewer)
        with self.mutex:self.reviewers.append(reviewer);cancelled=self.cancel.is_set()
        if cancelled:reviewer.stop()
    def remove_reviewer(self,reviewer):
        with self.mutex:
            if reviewer in self.reviewers:self.reviewers.remove(reviewer)
    def remember_human(self,name,report):
        with self.mutex:self.human_reports[name]=deepcopy(report)
    def decorate(self,name,projection):
        with self.mutex:report=deepcopy(self.human_reports.get(name))
        return projection if report is None else {**projection,'human_visibility':report}
    def add_worker(self,directory,operation_id,scope):
        with _existing_work_database(directory) as db_pin:
            record=(str(directory),_directory_pin(directory),db_pin,operation_id,deepcopy(scope))
        with self.mutex:self.workers.append(record);cancelled=self.cancel.is_set()
        if cancelled:self.stop()
        return record
    def remove_worker(self,record):
        with self.mutex:
            if record in self.workers:self.workers.remove(record)
    def stop(self):
        self.cancel.set()
        with self.mutex:pools,workers,reviewers=tuple(self.pools),tuple(self.workers),tuple(self.reviewers)
        for reviewer in reviewers:
            try:reviewer.stop()
            except Exception:self.stop_failed=True
        for pool in pools:
            try:pool.stop()
            except Exception:self.stop_failed=True
        # Identical to the existing concrete stop companion: only metadata
        # crosses threads. A fresh journal handle writes the normal immutable
        # Stop markers; no Runtime is finished or claim released by inference.
        from .work_journal import WorkJournal,JournalError
        from .development_worker import DevelopmentBoardWorker
        for directory,pin,db_pin,operation,scope in workers:
            try:
                require(_directory_pin(directory)==pin)
                with _existing_work_database(directory,db_pin),WorkJournal(directory) as journal:
                    try:op=journal.get(operation)
                    except JournalError as error:
                        if error.code=='operation_not_found':continue  # Before Worker.prepare.
                        raise
                    require(op['scope']==scope and op['configuration'].get('kind')=='development'
                        and op['configuration']['pins']['journal']==pin)
                    DevelopmentBoardWorker(journal,None,None,None,None).stop(operation)
            except Exception:self.stop_failed=True


_PREPARATION_ISSUER=object()


class _InitialPreparation:
    """An ephemeral stop-only check; never source/claim/permission authority."""
    def __init__(self,issuer,preparer,operation_id,control):
        require(issuer is _PREPARATION_ISSUER and type(preparer) is LinuxPoolJobPreparer
            and type(control) is _Pass,'continuation_pool_binding_changed')
        self.issuer=issuer
        self.preparer,self.operation_id,self.control=preparer,operation_id,control
        self.pid,self.thread=os.getpid(),threading.current_thread();self.active=True
    def __reduce_ex__(self,_):raise TypeError('Preparation controls cannot be serialized')
    def __copy__(self):raise TypeError('Preparation controls cannot be copied')
    def __deepcopy__(self,_):raise TypeError('Preparation controls cannot be copied')


def check_initial_preparation(preparer,operation_id,*,fresh=False):
    scope=getattr(preparer,'_continuation_initial_preparation',None)
    require(type(scope) is _InitialPreparation and scope.issuer is _PREPARATION_ISSUER and scope.active is True and scope.preparer is preparer
        and scope.operation_id==operation_id and scope.pid==os.getpid()
        and scope.thread is threading.current_thread(),'continuation_pool_binding_changed')
    scope.control.local_guard()
    if fresh:scope.control.service._pins()
    scope.control.local_guard()


@contextmanager
def _initial_preparing(control,preparer,operation_id):
    require(getattr(preparer,'_continuation_initial_preparation',None) is None)
    scope=_InitialPreparation(_PREPARATION_ISSUER,preparer,operation_id,control)
    preparer._continuation_initial_preparation=scope
    try:
        check_initial_preparation(preparer,operation_id,fresh=True)
        yield
        check_initial_preparation(preparer,operation_id,fresh=True)
    finally:
        scope.active=False;del preparer._continuation_initial_preparation


class ContinuationPoolService:
    """An inert constructor and one explicitly invoked, bounded scheduler pass."""
    def __init__(self,root,*,configuration_path,owner_id,board_endpoint,executable,cli_version,
                 interpreter,unshare,gh_executable,max_seconds):
        self.root=Path(_path(os.fspath(root)));self.path=Path(configuration_path)
        self.owner_id,self.endpoint=opaque(owner_id),board_endpoint
        self.max_seconds=max_seconds;self.cli_version=cli_version
        self.loaded,self.configuration_pins=load_continuation(configuration_path,root=self.root,
            owner_id=owner_id,board_endpoint=board_endpoint,max_seconds=max_seconds)
        require(type(cli_version) is str and cli_version.startswith('codex-cli '))
        self.tools={key:_executable_pin(os.fspath(value)) for key,value in
            (('executable',executable),('interpreter',interpreter),('unshare',unshare),('gh',gh_executable))}
        self.sources=linux_source_pins();self.source_sha=digest(canonical(self.sources))
        self.root_pin=_directory_pin(self.root)
        self.thread=threading.current_thread();self.pid=os.getpid();self._mutex=threading.Lock()
        self._retained_lease=None  # Kept until process exit if cooperative joins fail.
        self._retained_resources=[]  # Author/reader handles outlive an uncertain review join.
        self._sealed_settings=canonical(self._settings())

    def _settings(self):
        return {'root':str(self.root),'root_pin':self.root_pin,'configuration_path':str(self.path),
            'owner_id':self.owner_id,'endpoint':self.endpoint,'max_seconds':self.max_seconds,
            'loaded':self.loaded,'pins':self.configuration_pins,'tools':self.tools,
            'sources':self.sources,'source_sha256':self.source_sha,'cli_version':self.cli_version}

    def _pins(self,*,full=False):
        require(canonical(self._settings())==self._sealed_settings,'continuation_pool_configuration_changed')
        require(_directory_pin(self.root)==self.root_pin,'continuation_pool_binding_changed')
        for pin in self.configuration_pins.values():
            require(file_document(pin['path'],MAX_CONFIGURATION)[1]==pin,'continuation_pool_configuration_changed')
        if full:
            require(linux_source_pins()==self.sources,'continuation_pool_source_changed')
            for pin in self.tools.values():
                require(_executable_pin(pin['path'])==pin,'continuation_pool_tools_changed')

    def _binding(self,target,job):
        return {'schema':'continuation-pool-task-binding-v1','owner_id':self.owner_id,
            'selected':target,'job':job,'workflow':next(row for row in self.loaded['workflow']['jobs'] if row['name']==job['name']),
            'configuration_pins':self.configuration_pins,'tools':self.tools,'cli_version':self.cli_version,
            'source_sha256':self.source_sha,'cycle_limits':self.loaded['cycle_limits'][job['name']]}

    @contextmanager
    def _lease(self):
        path=self.root/'continuations'
        try:_mkdir(path);fresh=True
        except FileExistsError:
            fresh=False
            with PrivateDirectory(path):pass
        owned=PrivateDirectory(path);fd=None
        try:
            if fresh:
                fd=os.open('owner.lock',os.O_RDWR|os.O_NOFOLLOW|os.O_CREAT|os.O_EXCL,0o600,dir_fd=owned.fd)
                os.fsync(fd);os.fsync(owned.fd)
            else:
                try:fd=os.open('owner.lock',os.O_RDWR|os.O_NOFOLLOW,dir_fd=owned.fd)
                except FileNotFoundError:fail('continuation_pool_pending')
            owned.validate_file(fd);pin=identity(os.fstat(fd))
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:fail('continuation_pool_owner_busy')
            def verify():
                owned.verify();owned.validate_file(fd)
                require(identity(os.fstat(fd))==pin==identity(os.stat('owner.lock',dir_fd=owned.fd,follow_symlinks=False)),
                    'continuation_pool_binding_changed')
            verify();yield owned,verify;verify()
        finally:
            if self._retained_lease is not None:
                # The unit's cgroup stop is the final guard. Do not admit a
                # second pass while a local thread may still own an effect.
                self._retained_lease=(owned,fd)
            else:
                if fd is not None:os.close(fd)
                owned.close()

    @staticmethod
    def _projection(target,*,phase='needs_you',reason='',operation_id=None,state_sha256=None,not_before=None):
        require(phase in STATES and reason in REASONS)
        if phase=='needs_you' and not reason:reason='continuation_pool_target_failed'
        if operation_id is not None:uuid_value(operation_id)
        return {'name':target['name'],'logical_task_id':target['logical_task_id'],'phase':phase,
            'reason':reason,'operation_id':operation_id,'state_sha256':state_sha256,
            'next_observation_at':not_before,'launchable':False}

    def _review_history(self,directory,target,job,cursor):
        """Pinned past opinion only; no source builder, registration or RPC."""
        receipt=cursor['review_receipt'];projection=cursor['result']
        require(type(receipt) is dict and set(receipt)=={'schema','status','logical_task_id','source_operation_id',
            'review_operation_id','binding_sha256','ledger_pin','review','accepted','launchable'}
            and receipt['schema']=='continuation-independent-review-receipt-v1' and receipt['status']=='published'
            and receipt['logical_task_id']==target['logical_task_id']
            and receipt['source_operation_id']==projection['operation_id']
            and receipt['accepted'] is False and receipt['launchable'] is False,
            'continuation_pool_binding_changed')
        uuid_value(receipt['source_operation_id']);uuid_value(receipt['review_operation_id'])
        path=directory/'aggregate/delivery/review-source/continuation-review.json'
        raw,pin=file_document(path,MAX_RECORD)
        require(pin==receipt['ledger_pin'],'continuation_pool_binding_changed')
        value=decode(raw)
        require(canonical(value)==raw and type(value) is dict and set(value)=={'request','phase','job','result','pins'}
            and value['phase']=='complete' and value['result']==receipt['review']
            and value['result'].get('status')=='complete' and value['result'].get('operation_id')==receipt['review_operation_id']
            and value['job'].get('id')==receipt['review_operation_id']
            and digest(canonical(value['request']))==receipt['binding_sha256'],'continuation_pool_binding_changed')
        request=value['request'];specification=next(row for row in self.loaded['workflow']['jobs'] if row['name']==job['name'])
        require(request.get('schema')=='continuation-aggregate-review-v1'
            and request.get('logical_task_id')==target['logical_task_id']
            and request.get('source_operation_id')==receipt['source_operation_id']
            and request.get('directory')==str(directory/'independent-review')
            and request.get('configuration_pins')==self.configuration_pins and request.get('name')==job['name']
            and request.get('reviewer')==specification['reviewer'],'continuation_pool_binding_changed')
        result=self._projection(target,phase='independent_review_published',operation_id=receipt['source_operation_id'],
            state_sha256=projection['state_sha256'])
        result.update(review_operation_id=receipt['review_operation_id'],review_receipt_sha256=digest(canonical(receipt)),
            accepted=False,historical=True)
        return result

    def run_once(self,*,should_stop=None):
        require(self.pid==os.getpid() and self.thread is threading.current_thread())
        require(should_stop is None or callable(should_stop))
        require(self._mutex.acquire(blocking=False),'continuation_pool_owner_busy')
        try:
            require(self._retained_lease is None,'continuation_pool_cleanup_unknown')
            self._pins(full=True)
            try:
                from .development_provider_continuation import DevelopmentProviderContinuation
                from .development_aggregate_delivery import AggregateDevelopmentDeliveryController
            except ImportError:fail('continuation_pool_api_unavailable')
            with self._lease() as (owned,held):
                control=_Pass(self,should_stop);targets=self.loaded['configuration']['tasks']
                jobs={job['name']:job for job in self.loaded['batch']['jobs']}
                results={};threads={};index=0;join_deadline=None
                def advance(target):
                    # Every concrete handle and callback is constructed here,
                    # not in the owner/scheduler thread.
                    observation=_TargetObservation(target['logical_task_id'])
                    token=_TARGET_OBSERVATION.set(observation)
                    try:results[target['name']]=self._target(owned.path,target,jobs[target['name']],control)
                    except Exception as error:
                        code=vars(error).get('code') if type(error) is DevelopmentError else None
                        projection=self._projection(target,reason=code if type(code) is str and code in REASONS
                            else 'continuation_pool_target_failed')
                        projection['failure']=_target_failure(error,observation)
                        results[target['name']]=control.decorate(target['name'],projection)
                    except BaseException:
                        # A thread's abrupt interruption is not a clean or
                        # never-started task. Do not let threading print it.
                        control.stop_failed=True;control.stop()
                        results[target['name']]=self._projection(target,reason='continuation_pool_cleanup_unknown')
                    finally:_TARGET_OBSERVATION.reset(token)
                try:
                    while index<len(targets) or threads:
                        held()
                        if control.stopping():
                            if join_deadline is None:join_deadline=time.monotonic()+JOIN_SECONDS
                        else:
                            while (index<len(targets) and len(threads)<self.loaded['configuration']['concurrency']
                                    and not control.stopping()):
                                target=targets[index];index+=1
                                thread=threading.Thread(target=advance,args=(target,),name='continuation-'+target['name'],daemon=False)
                                threads[target['name']]=thread;thread.start()
                        for name,thread in tuple(threads.items()):
                            thread.join(timeout=.025)
                            if not thread.is_alive():del threads[name]
                        if join_deadline is not None and time.monotonic()>=join_deadline and threads:
                            self._retained_lease=True
                            for target in targets:
                                if target['name'] not in results:
                                    results[target['name']]=self._projection(target,reason='continuation_pool_cleanup_unknown')
                            break
                        if control.cancel.is_set() and not threads:break
                except BaseException:
                    control.stop();deadline=time.monotonic()+JOIN_SECONDS
                    for thread in tuple(threads.values()):
                        if thread.ident is not None:thread.join(timeout=max(0,deadline-time.monotonic()))
                    if control.stop_failed or any(thread.is_alive() for thread in threads.values()):
                        self._retained_lease=True
                    fail('continuation_pool_cleanup_unknown')
                for target in targets:
                    results.setdefault(target['name'],self._projection(target,phase='not_started',reason='continuation_pool_stopped'))
                ordered=[results[target['name']] for target in targets]
                if control.stop_failed:self._retained_lease=True
                status='needs_you' if control.stop_failed or any(row['phase']=='needs_you' for row in ordered) else 'stopped' if control.cancel.is_set() else 'pass_complete'
                return {'schema':'continuation-pool-pass-v1','status':status,'tasks':ordered,
                    'activated':True,'pass_complete_is_project_completion':False}
        finally:self._mutex.release()

    def _target(self,root,target,job,control):
        _observe_stage('target_binding')
        control.guard();self._pins(full=True)
        key=digest(canonical({'owner_id':self.owner_id,'logical_task_id':target['logical_task_id']}))
        directory=root/key;binding=self._binding(target,job)
        try:_mkdir(directory);fresh=True
        except FileExistsError:fresh=False
        with PrivateDirectory(directory) as private:
            if fresh:
                _write(private,'binding.json',binding)
                _mkdir(directory/'generations');_mkdir(directory/'checkpoints')
                cursor={'schema':'continuation-pool-cursor-v1','phase':'idle','generation':None,'result':None,'not_before':None}
                _write(private,'cursor.json',cursor)
            else:
                require(_read(private,'binding.json')==binding,'continuation_pool_binding_changed')
                _observe_stage('cursor_read')
                cursor=_read(private,'cursor.json')
            _observe_stage('cursor_read')
            require(type(cursor) is dict and set(cursor) in ({'schema','phase','generation','result','not_before'},
                {'schema','phase','generation','result','not_before','wake_generation'},
                {'schema','phase','generation','result','not_before','review_receipt'})
                and cursor['schema']=='continuation-pool-cursor-v1' and cursor['phase'] in ('idle','pending'))
            waiting_generation=cursor.get('wake_generation')
            if waiting_generation is not None:
                uuid_value(waiting_generation)
                require(cursor['phase']=='idle' and cursor['generation'] is not None
                    and waiting_generation!=cursor['generation'] and type(cursor['result']) is dict
                    and cursor['result'].get('phase')=='paused_provider','continuation_pool_binding_changed')
            if cursor['phase']=='pending':return self._projection(target,reason='continuation_pool_pending')
            if 'review_receipt' in cursor:
                _observe_stage('historical_review')
                require(cursor['generation'] is not None and type(cursor['result']) is dict
                    and cursor['result'].get('phase')=='independent_review_published','continuation_pool_binding_changed')
                with ContinuationJournal.open(directory/'logical') as journal:
                    observed=journal.snapshot()
                    if observed['state']['stop_requested'] or observed['state']['phase']=='stopped':
                        return self._projection(target,phase='stopped',state_sha256=observed['state_sha256'])
                    require(observed['state']['phase']=='complete'
                        and observed['state_sha256']==cursor['result'].get('state_sha256'),'continuation_pool_binding_changed')
                    historical=self._review_history(directory,target,job,cursor)
                    current=journal.snapshot();control.guard()
                    if current['state']['stop_requested'] or current['state']['phase']=='stopped':
                        return self._projection(target,phase='stopped',state_sha256=current['state_sha256'])
                    require(current==observed,'continuation_pool_binding_changed')
                    return historical
            now=time.time();require(type(now) in (int,float) and now>0,'continuation_pool_clock_changed')
            if cursor['not_before'] is not None:
                require(type(cursor['not_before']) in (int,float) and cursor['not_before']>0)
                if now<cursor['not_before']:
                    # Stop remains visible during a long provider backoff.
                    # Historical Core metadata creates neither a permit nor
                    # a Worker/client and cannot admit any execution.
                    with ContinuationJournal.open(directory/'logical') as journal:
                        observed=journal.snapshot()
                        if observed['state']['phase'] in ('stopped','needs_you','complete'):
                            return self._projection(target,phase=observed['state']['phase'],
                                state_sha256=observed['state_sha256'])
                    return self._projection(target,phase='backoff',not_before=cursor['not_before'])
            with ExitStack() as resources:
                old=None
                if cursor['generation'] is not None:
                    _observe_stage('previous_generation_open')
                    _observe_identity(cursor['generation'])
                    old=_Generation.open(self,directory,target,job,cursor['generation'],control,resources)
                    _observe_stage('previous_state_check')
                    state=old.task.snapshot()
                    if state['state']['phase'] not in ('paused_cycle','paused_provider'):
                        phase=state['state']['phase']
                        if phase not in ('stopped','needs_you','complete'):
                            _write(private,'cursor.json',{**cursor,'phase':'pending'},replace=True)
                            return self._projection(target,reason='continuation_pool_pending',operation_id=old.operation,
                                state_sha256=state['state_sha256'])
                        if (phase=='complete' and type(cursor['result']) is dict
                                and cursor['result'].get('phase')=='awaiting_independent_review'
                                and cursor['result'].get('state_sha256')==state['state_sha256']):
                            phase='awaiting_independent_review'
                        return self._projection(target,phase=phase,operation_id=old.operation,
                            state_sha256=state['state_sha256'])
                    require(type(cursor['result']) is dict and cursor['result']['state_sha256']==state['state_sha256']
                        and cursor['result']['operation_id']==old.operation and cursor['result']['phase']==state['state']['phase'],
                        'continuation_pool_binding_changed')
                    require(old.provider.status(old.operation)['status']==state['state']['phase'])
                    _observe_stage('provider_availability')
                    if state['state']['phase']=='paused_provider' and not old.can_wake():
                        waiting=time.time()+self.loaded['configuration']['provider_backoff_seconds']
                        _write(private,'cursor.json',{**cursor,'not_before':waiting},replace=True)
                        return self._projection(target,phase='paused_provider',operation_id=old.operation,
                            state_sha256=state['state_sha256'],not_before=waiting)
                generation_id=waiting_generation or str(uuid.uuid4())
                _observe_stage('next_generation_intent')
                _observe_identity(generation_id)
                # Persist BEFORE all preparation, registrations and model calls.
                pending={key:value for key,value in cursor.items() if key!='wake_generation'}
                pending.update(phase='pending',generation=generation_id,result=None,not_before=None)
                _write(private,'cursor.json',pending,replace=True)
                _observe_stage('next_generation_create')
                if waiting_generation is None:
                    new=_Generation.create(self,directory,target,job,generation_id,control,resources,old=old)
                else:
                    require(old is not None and old.task.snapshot()['state']['phase']=='paused_provider')
                    new=_Generation.open(self,directory,target,job,generation_id,control,resources,candidate_for=old)
                try:
                    if old is None:
                        _observe_stage('initial_preparation');new.prepare()
                        _observe_stage('worker_open');new.open_worker()
                        _observe_stage('task_open');new.open_task(create=True)
                        _observe_stage('cycle_run')
                        result=new.provider.run_checkpoint(new.operation,request_id='continuation-'+new.operation,
                            after_actions=target['checkpoint_after_actions'])
                    elif old.task.snapshot()['state']['phase']=='paused_provider':
                        # Atomic provider wake owns preparation/admission. There is
                        # deliberately no WorkerPool dispatch on either side.
                        if waiting_generation is None:
                            _observe_stage('worker_open');new.open_worker(unprepared=True)
                            _observe_stage('task_open');new.open_task(create=False,old=old)
                        paused=old.task.snapshot()
                        _observe_stage('provider_wake')
                        result=old.provider.wake(old.operation,next_controller=new.provider,preparer=new.preparer,
                            next_operation_id=new.operation,parameters=new.arguments,
                            request_id='continuation-'+new.operation,after_actions=target['checkpoint_after_actions'])
                        if result['status']=='paused_provider' and new.task.snapshot()==paused:
                            # No new cycle was admitted. Keep the failed source
                            # generation, plus this exact candidate for a fresh
                            # normal wake; never orphan a prepared_waiting intent.
                            control.guard()
                            finished=time.time();require(type(finished) in (int,float) and finished>=now,
                                'continuation_pool_clock_changed')
                            not_before=finished+self.loaded['configuration']['provider_backoff_seconds']
                            projection=self._projection(target,phase='paused_provider',operation_id=old.operation,
                                state_sha256=paused['state_sha256'],not_before=not_before)
                            _write(private,'cursor.json',{'schema':'continuation-pool-cursor-v1','phase':'idle',
                                'generation':old.generation,'wake_generation':generation_id,
                                'result':projection,'not_before':not_before},replace=True)
                            return projection
                    else:
                        _observe_stage('previous_retirement')
                        _observe_identity(old.generation,old.operation)
                        retired=old.provider.maintenance.retire_checkpoint(old.operation)
                        require(retired['phase']=='retired')
                        _observe_stage('next_preparation')
                        _observe_identity(new.generation,new.operation)
                        old.provider.maintenance.prepare_next(old.operation,preparer=new.preparer,
                            next_operation_id=new.operation,parameters=new.arguments)
                        _observe_stage('worker_open');new.open_worker()
                        _observe_stage('task_open');new.open_task(create=False,old=old)
                        _observe_stage('cycle_run')
                        result=new.provider.run_checkpoint(new.operation,request_id='continuation-'+new.operation,
                            parent_checkpoint_id=checkpoint_id_for(old.operation),after_actions=target['checkpoint_after_actions'])
                    control.guard()
                    if result['status']=='paused_cycle':
                        _observe_stage('cycle_retirement')
                        require(new.provider.maintenance.retire_checkpoint(new.operation)['phase']=='retired')
                    elif result['status']=='review_ready':
                        _observe_stage('aggregate_publication')
                        result=new.publish()
                    _observe_stage('result_checkpoint')
                    snapshot=new.task.snapshot();phase=result['status']
                    if phase=='needs_you':
                        new.human_handoff();snapshot=new.task.snapshot()
                        if snapshot['state']['stop_requested']:phase='stopped'
                    require(phase in STATES,'continuation_pool_target_failed')
                    require(snapshot['state']['phase']==phase or phase in ('awaiting_independent_review','independent_review_published')
                        and snapshot['state']['phase']=='complete')
                    finished=time.time();require(type(finished) in (int,float) and finished>=now,'continuation_pool_clock_changed')
                    not_before=finished+self.loaded['configuration']['provider_backoff_seconds'] if phase=='paused_provider' else None
                    projection=control.decorate(target['name'],self._projection(target,phase=phase,operation_id=new.operation,
                        state_sha256=snapshot['state_sha256'],not_before=not_before))
                    completed={'schema':'continuation-pool-cursor-v1','phase':'idle',
                        'generation':generation_id,'result':projection,'not_before':not_before}
                    if phase=='independent_review_published':
                        completed['review_receipt']=result['review_receipt']
                        projection.update(review_operation_id=result['review_receipt']['review_operation_id'],
                            review_receipt_sha256=digest(canonical(result['review_receipt'])),accepted=False,historical=False)
                    _observe_stage('cursor_commit')
                    _write(private,'cursor.json',completed,replace=True)
                    return projection
                except Exception as error:
                    # The own running generation becomes NeedsYou with actual
                    # counters. This creates no accepted checkpoint or cleanup.
                    # An already sealed code completion is not reopened merely
                    # because a later publication/handoff is uncertain.
                    if type(new.task) is ContinuationTask and new.provider is not None:
                        try:
                            current=new.task.snapshot()
                            if current['state']['phase']=='running':
                                new.provider.driver._mark_review(new.operation,new.task._context(current,purpose='execute'))
                        except Exception:pass  # Pending service intent remains fail-closed.
                        if not (type(error) is DevelopmentError and vars(error).get('code')=='continuation_pool_stopped'):
                            new.human_handoff(error)
                    raise



class _LiveChecks:
    """Checks compiled against this generation's actual Worker and selection.

    A stored service cursor provides correlation only. Neither an idle flag nor
    a provider observation supplies permission to these callbacks.
    """
    def __init__(self,generation):self.g=generation;self.observed=None
    def checks(self):
        return ContinuationChecks(**{key:getattr(self,key) for key in
            ('scope','claim','source','fence','runtime','resume')})
    def operation(self):
        self.g.control.guard()
        return self.g.worker.journal.get(self.g.operation)
    def scope(self,context):
        op=self.operation();w=self.g.worker
        require(all(op['scope'][key]==context.scope[key] for key in
            ('owner_id','agent_id','project_id','ticket_id','board_endpoint')),
            'continuation_pool_binding_changed')
        with PrivateDirectory(op['configuration']['pins']['broker']['path']) as private:
            manifest=validate_manifest(decode(private.read('manifest.json',128*1024)))
            require(digest(canonical(manifest))==op['configuration']['manifest_hash'])
            auth=DevelopmentAuthorization(w.client,scope=op['scope'],manifest=manifest,
                registration=op['payload']['registration'],clock=w.clock,stop_requested=self.g.control.stopping)
            active='context' in op['payload'] and not w._stopped(op['id'])
            if active:
                auth.expected_context=deepcopy(op['payload']['context'])
                auth.claim_version=op['payload']['claim_version'];auth.claim_id=op['payload']['claim_id']
            observation=auth.snapshot(require_claim=active)
            registration=op['payload']['registration']
            require(registration['profile_version']==context.scope['profile_version']
                and registration['identity']['device_id']==context.scope['device_id'])
            self.observed=(context,observation,active,auth)
    def claim(self,context):
        require(self.observed is not None and self.observed[0] is context)
        ticket=self.observed[1]['ticket']
        # Self-unassignment at the final normal handoff requires an explicit
        # author assignment. A claim alone never supplies it.
        require(ticket.get('assignee_agent_id')==self.g.job['agent_id'],
            'continuation_pool_binding_changed')
        if not self.observed[2]:
            require((ticket['claimant'] is None and ticket['status']=='todo'
                and ticket.get('claim_expired') is not True) or self.observed[3].own_claim(ticket),
                'continuation_pool_binding_changed')
    def source(self,context):
        op=self.operation();w=self.g.worker;self.g.workspaces._roots()
        self.g.service._pins(full=True)
        # These readers take no broker/planner lock. Control can be called
        # while the ordinary SDK holds them during a confirmed action.
        # Their own source/test/permission guards continue to run unchanged.
        bound=self.g.driver.selections.load(self.g.context)
        self.g.driver._operation(w,self.g.context,bound)
    def fence(self,context):
        from .dev_fence import DevelopmentFence,canonical_anchor
        op=self.operation();w=self.g.worker;binding=op['payload'].get('parent_binding')
        anchor=canonical_anchor() if w.test_anchor is None else Path(w.test_anchor)
        try:os.stat(anchor,follow_symlinks=False)
        except FileNotFoundError:
            require(binding is None,'continuation_pool_binding_changed');return
        with PrivateDirectory(anchor) as private:
            fd=os.open('fence.lock',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=private.fd)
            try:
                private.validate_file(fd);pin=identity(os.fstat(fd));fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
                state=DevelopmentFence._validate(object.__new__(DevelopmentFence),_read(private,'fence.json'))
                manifest=decode(file_document(Path(op['configuration']['pins']['broker']['path'])/'manifest.json')[0])
                found=state['records'].get(DevelopmentFence.key(op['scope'],manifest['root_identity']))
                if binding is None:require(found is None,'continuation_pool_binding_changed')
                else:
                    expected=deepcopy(binding)
                    path=Path(op['configuration']['pins']['broker']['path'])/'planner.json'
                    try:planner=decode(file_document(path,2*1024*1024)[0])
                    except FileNotFoundError:pass
                    else:expected['configuration_hash']=digest(canonical(planner['configuration']))
                    require(found=={'binding':expected,'operation_id':op['id']},'continuation_pool_binding_changed')
                private.verify();private.validate_file(fd)
                require(identity(os.fstat(fd))==pin==identity(os.stat('fence.lock',dir_fd=private.fd,follow_symlinks=False)))
            finally:os.close(fd)
    def runtime(self,context):
        op=self.operation();w=self.g.worker
        rows=w.runtime.list_status();require(len(rows)<=128)
        for row in rows:
            record=w.runtime.get(row['id']);config=record['configuration']
            require(config['scope']==op['scope'] and config['work_id']==development_work_id(op['id'])
                and record['phase']=='finished' and type(record['receipt']) is dict
                and record['receipt'].get('cleanup_confirmed') is True,'continuation_pool_cleanup_unknown')
            guard=RuntimeGuard(w.runtime,w.control,op['scope']);guard.operation=record
            found=w.control.call('lookup_execution',{'idempotency_key':record['key']})
            require(type(found) is dict and found.get('found') is True,'continuation_pool_cleanup_unknown')
            receipt=guard.metadata(found.get('execution'))
            require(receipt==record['receipt'] and receipt['state']=='finished'
                and receipt['cleanup_confirmed'] is True,'continuation_pool_cleanup_unknown')
        if not rows:
            # A never-started generation has no model work or pending Runtime.
            require(op['stage']=='new' and w.store._db.execute('SELECT id FROM works LIMIT 1').fetchone() is None,
                'continuation_pool_cleanup_unknown')
    def resume(self,context):
        self.g.control.guard();self.g.service._pins()
        require(context.logical_task_id==self.g.target['logical_task_id'])
        if self.g.task is not None:
            snapshot=self.g.task.snapshot()
            require(snapshot['state']['stop_requested'] is False
                and snapshot['state']['phase'] not in ('needs_you','stopped','complete'),
                'continuation_pool_stopped')


class _Generation:
    """One thread's concrete queued pool, Worker, source and journal handles."""
    @classmethod
    def create(cls,service,directory,target,job,generation,control,resources,*,old=None):
        result=cls();result._setup(service,directory,target,job,generation,control,resources)
        _mkdir(result.directory)
        if old is None:grant=deepcopy(job['repository'])
        else:
            cp=old.checkpoints.verify(checkpoint_id_for(old.operation))
            grant={**deepcopy(job['repository']),'repository':cp['checkout'],'base_commit':cp['commit_sha']}
        for name in ('git-state','checkouts'):
            parent=service.root/name/directory.name
            try:_mkdir(parent)
            except FileExistsError:
                with PrivateDirectory(parent):pass
            _mkdir(parent/generation)
        grant['workspace_root']=str(service.root/'checkouts'/directory.name/generation)
        result.grant=grant
        result.workspaces=LocalGitWorkspaceController(service.root/'git-state'/directory.name/generation,
            grant=LocalRepositoryGrant(**grant))
        result._pool(create=True)
        with PrivateDirectory(result.directory) as private:
            _write(private,'generation.json',{'schema':'continuation-pool-generation-v1','generation':generation,
                'grant':grant,'submitted':result.submitted,'connection':result.driver.selections.connection_pin})
        if old is not None:
            with PrivateDirectory(result.directory) as private:
                _write(private,'parent.json',{'checkpoint_id':cp['checkpoint_id'],'checkpoint_sha256':cp['checkpoint_sha256']})
        result.task=None
        result.checkpoint_configuration=None if old is None else deepcopy(old.checkpoints.configuration)
        result._worker_open=False
        return result
    @classmethod
    def open(cls,service,directory,target,job,generation,control,resources,*,candidate_for=None):
        uuid_value(generation);result=cls();result._setup(service,directory,target,job,generation,control,resources)
        with PrivateDirectory(result.directory) as private:value=_read(private,'generation.json')
        require(type(value) is dict and set(value)=={'schema','generation','grant','submitted','connection'}
            and value['schema']=='continuation-pool-generation-v1' and value['generation']==generation)
        result.grant=value['grant']
        # Existing metadata is not permission: exact source identity/caps are
        # revalidated below by Git, Core, provider and real Worker paths.
        require(result.grant['owner_id']==service.owner_id
            and result.grant['repository_id']==job['repository']['repository_id']
            and result.grant['workspace_root']==str(service.root/'checkouts'/directory.name/generation))
        result.workspaces=LocalGitWorkspaceController(service.root/'git-state'/directory.name/generation,
            grant=LocalRepositoryGrant(**result.grant))
        result.submitted=value['submitted'];result._pool(create=False)
        require(result.driver.selections.connection_pin==value['connection'])
        result.task=None;result._worker_open=False;result.checkpoint_configuration=None
        if candidate_for is None:
            result.open_worker(existing=True);result.open_task(create=False)
        else:
            cp=candidate_for.checkpoints.verify(checkpoint_id_for(candidate_for.operation))
            require(result.grant=={**deepcopy(job['repository']),'repository':cp['checkout'],
                'base_commit':cp['commit_sha'],'workspace_root':str(service.root/'checkouts'/directory.name/generation)})
            with PrivateDirectory(result.directory) as private:
                require(_read(private,'parent.json')=={'checkpoint_id':cp['checkpoint_id'],
                    'checkpoint_sha256':cp['checkpoint_sha256']})
            result.open_worker(existing=True,unprepared=True);result.open_task(create=False,old=candidate_for)
        return result
    def _setup(self,service,directory,target,job,generation,control,resources):
        uuid_value(generation)
        _observe_identity(generation)
        self.service,self.target,self.job,self.control,self.resources=service,target,job,control,resources
        self.target_directory=directory;self.directory=directory/'generations'/generation
        self.generation=generation;self.worker=None;self.task=None;self.provider=None;self._resources_retained=False
        self.arguments={key:deepcopy(job[key]) for key in ('project_id','ticket_id','paths','resource','actions',
            'verifier_path','verifier_sha256','test_name','test_timeout_seconds','test_max_output_bytes','instruction','model')}
        self.arguments.update(planner_budgets=PlannerBudgets(**job['planner_budgets']),
            broker_budgets=Budgets(**job['broker_budgets']),cli_version=service.cli_version,
            **{key:service.tools[key]['path'] for key in ('executable','interpreter','unshare')},
            **_reasoning_configuration(job.get('reasoning_effort')))
    def _pool(self,*,create):
        if not create:
            for path in (self.directory/'pool'/'pool-state.json',self.directory/'pool'/'pool-owner.lock'):
                file_document(path,MAX_RECORD)
            with PrivateDirectory(self.directory/'selections'):pass
            with PrivateDirectory(self.directory/'preparation'):pass
        self.driver=DevelopmentPoolDriver(self.directory/'selections',connection_file=self.job['connection_file'],
            owner_id=self.service.owner_id,agent_id=self.job['agent_id'],board_endpoint=self.service.endpoint)
        self.pool=self.resources.enter_context(closing(WorkerPool(self.directory/'pool',concurrency=1,ceiling=1,
            drivers={'development':self.driver.binding()})))
        self.control.add_pool(self.pool);self.resources.callback(self.control.remove_pool,self.pool)
        if create:
            submitted=self.pool.submit(request_id='continuation-'+self.generation,driver_id='development',
                payload={key:self.job[key] for key in ('project_id','ticket_id')})
            with self.pool.mutex:
                self.submitted=deepcopy(self.pool._job(self.pool.journal.read(),submitted['id']))
        else:
            with self.pool.mutex:
                current=self.pool._job(self.pool.journal.read(),self.submitted['id'])
                require(all(current[key]==self.submitted[key] for key in
                    ('id','worker_id','session_id','driver_id','driver_revision','payload','request_id')))
                require((current['state']=='queued' and not current['stop_requested']) or
                    (current['state']=='cancelled' and current['stop_requested'] is True
                        and current['reason']=='stop_before_dispatch' and current['execution_active'] is False))
                # Historical stopped preparation metadata stays stopped. It
                # grants nothing; clean Core/checkpoint/retirement + Runtime
                # proofs must still pass before a NEW queued generation.
        self.operation=self.submitted['id']
        _observe_identity(self.generation,self.operation)
        self.context=self.pool._context(self.submitted if create else deepcopy(current),create=create)
        if not create:
            for kind,name in (('work','work-journal.sqlite3'),('runs','runs.sqlite3'),('runtime','runtime.sqlite3')):
                file_document(Path(self.context.journals[kind])/name,32*1024*1024)
        self.preparer=LinuxPoolJobPreparer(self.directory/'preparation',pool=self.pool,
            driver=self.driver,workspaces=self.workspaces)
    def prepare(self):
        self.control.guard()
        with _initial_preparing(self.control,self.preparer,self.operation):
            self.preparer.prepare(self.operation,**self.arguments)
        self.control.guard()
    def open_worker(self,*,existing=False,unprepared=False):
        require(not self._worker_open)
        if unprepared:
            bound={'selection':{'scope':{**self.driver.selections.fixed,
                **{key:self.job[key] for key in ('project_id','ticket_id')},'board_session_id':self.context.session_id}},
                'connection':self.driver.selections.connection_pin}
        else:bound=self.driver.selections.load(self.context)
        self.worker=self.driver._open(self.resources,self.context,bound,existing=existing,observe=False)
        self._worker_open=True
        worker_scope={**self.driver.selections.fixed,**{key:self.job[key] for key in ('project_id','ticket_id')},
            'board_session_id':self.context.session_id}
        record=self.control.add_worker(self.worker.journal.directory,self.operation,worker_scope)
        self.resources.callback(self.control.remove_worker,record)
        if existing and not unprepared:self.worker.client.adopt_registration()
        elif not unprepared:
            selection=bound['selection']
            self.worker.prepare(scope=selection['scope'],request_id='continuation-prepare-'+self.operation,
                instruction=bound['instruction'],broker_directory=selection['broker_directory'],
                backend_directory=selection['backend_directory'],planner_policy_directory=selection['planner_policy_directory'],
                model=selection['model'],budgets=PlannerBudgets(**selection['budgets']),operation_id=self.operation,
                **_reasoning_configuration(selection.get('reasoning_effort')))
    def open_task(self,*,create,old=None):
        try:from .development_provider_continuation import DevelopmentProviderContinuation
        except ImportError:fail('continuation_pool_api_unavailable')
        checks=_LiveChecks(self).checks();task_path=self.target_directory/'logical'
        if create:
            op=self.worker.journal.get(self.operation)
            with closing(DevelopmentJournal(op['configuration']['pins']['broker']['path'])) as broker:
                manifest=deepcopy(broker.manifest)
            plan=decode(file_document(Path(op['configuration']['pins']['backend']['path'])/'execution-plan.json',MAX_RECORD)[0])
            tests=_test_caps(manifest,plan)
            caps={'paths':sorted(manifest['files']),'tests':tests,'actions':manifest['actions'],
                'resource':manifest['resource'],'broker_limits':manifest['budgets'],'planner_limits':op['configuration']['budgets']}
            self.checkpoint_configuration={'schema':'private-development-checkpoint-config-v1',
                'task_id':self.target['logical_task_id'],'owner_id':self.service.owner_id,
                **{key:self.job[key] for key in ('project_id','ticket_id','resource')},
                'repository_id':self.grant['repository_id'],'original_public_base':self.job['repository']['base_commit'],
                'global_paths':sorted(self.job['paths']),'global_test_caps_sha256':digest(canonical(tests)),
                'static_caps_sha256':digest(canonical(caps)),'source_release_sha256':self.service.source_sha}
            with PrivateDirectory(self.target_directory) as private:
                _write(private,'checkpoint-configuration.json',self.checkpoint_configuration)
            registration=op['payload']['registration']
            scope={key:op['scope'][key] for key in ('owner_id','agent_id','project_id','ticket_id','board_endpoint')}
            scope.update(device_id=registration['identity']['device_id'],profile_version=registration['profile_version'],
                resource=self.job['resource'],source_release_sha256=self.service.source_sha)
            # This initial digest is factual selected-source content, not a
            # test pass or a future grant. All later sources have typed Proofs.
            initial=digest(canonical({path:row['sha256'] for path,row in manifest['files'].items()}))
            self.task=self.resources.enter_context(closing(ContinuationTask.create(task_path,
                logical_task_id=self.target['logical_task_id'],scope=scope,
                cycle_limits=self.service.loaded['cycle_limits'][self.job['name']],
                initial_source_sha256=initial,checks=checks)))
        else:
            with PrivateDirectory(self.target_directory) as private:
                config=_read(private,'checkpoint-configuration.json')
            require(self.checkpoint_configuration in (None,config))
            self.checkpoint_configuration=config
            if old is not None:
                self.task=ContinuationTask(old.task.journal,checks,clock=old.task.clock)
            else:
                journal=self.resources.enter_context(ContinuationJournal.open(task_path))
                self.task=ContinuationTask(journal,checks)
            require(self.task.configuration['logical_task_id']==self.target['logical_task_id']
                and self.task.configuration['cycle_limits']==self.service.loaded['cycle_limits'][self.job['name']]
                and all(self.task.configuration['scope'][key]==self.job[key] for key in
                    ('agent_id','project_id','ticket_id','resource'))
                and self.task.configuration['scope']['owner_id']==self.service.owner_id
                and self.task.configuration['scope']['source_release_sha256']==self.service.source_sha)
        config=self.checkpoint_configuration
        require(config['task_id']==self.target['logical_task_id']
            and config['owner_id']==self.service.owner_id
            and all(config[key]==self.job[key] for key in ('project_id','ticket_id','resource'))
            and config['repository_id']==self.job['repository']['repository_id']
            and config['original_public_base']==self.job['repository']['base_commit']
            and config['global_paths']==sorted(self.job['paths'])
            and config['source_release_sha256']==self.service.source_sha,'continuation_pool_binding_changed')
        self.checkpoints=PrivateDevelopmentCheckpointController(self.workspaces,self.target_directory/'checkpoints',
            configuration=config)
        self.provider=DevelopmentProviderContinuation(self.task,self.worker,self.checkpoints,
            backoff_seconds=self.service.loaded['configuration']['provider_backoff_seconds'])
    def can_wake(self):
        from .provider_disposition import observe_provider_availability,classify_provider_availability
        snapshot=self.task.snapshot();binding=self.provider._wake_binding(snapshot)
        observation=observe_provider_availability(self.service.tools['executable']['path'],binding_sha256=binding,timeout=30)
        candidate=classify_provider_availability(observation,binding_sha256=binding,enabled=True)
        require(self.task.snapshot()==snapshot)
        require(candidate['classification'] in ('available','provider_unavailable'))
        return candidate['classification']=='available'
    def human_handoff(self,error=None):
        """Best-effort closed visibility; never changes the original failure."""
        from .development_worker import DevelopmentBoardWorker
        if (type(self.worker) is not DevelopmentBoardWorker or type(self.task) is not ContinuationTask
                or self._resources_retained):return
        try:
            self.control.local_guard()
            state=self.task.snapshot()['state']
            if state['phase']!='needs_you' or state['stop_requested'] or self.worker._stopped(self.operation):return
            from .continuation_human_handoff import ContinuationHumanHandoff
            code=vars(error).get('code') if type(error) is DevelopmentError else None
            question='permissions' if code in PERMISSION_FAILURES else 'effects'
            self.control.local_guard()
            report=ContinuationHumanHandoff(self.worker,self.task,self.operation).publish(question_code=question)
            require(type(report) is dict)
            common={'schema','operation_id','status','human_question_confirmed','launchable'}
            extra={'request_id'} if report.get('status')=='question_confirmed' else {'message_id'} if report.get('status')=='notice_confirmed' else set()
            require(type(report) is dict and set(report)==common|extra
                and report['schema']=='continuation-human-visibility-v1' and report['operation_id']==self.operation
                and report['status'] in ('not_applicable','pending','notice_confirmed','question_confirmed')
                and type(report['human_question_confirmed']) is bool
                and report['human_question_confirmed']==(report['status']=='question_confirmed')
                and report['launchable'] is False)
            for field in extra:opaque(report[field],'continuation_pool_binding_changed',200)
            if report['status']!='not_applicable':self.control.remember_human(self.target['name'],report)
        except Exception:pass  # The original SDK error/Stop remains authoritative.
    def publish(self):
        # Exact final aggregation API is additive and intentionally mandatory.
        # An old SDK cannot silently publish only the last cycle's delta.
        try:
            from .development_aggregate_delivery import AggregateDevelopmentDeliveryController
            from .development_delivery import DevelopmentDeliveryProof
            from .development_aggregate_review_source import PublishedAggregateReviewSource
            from .continuation_aggregate_review import ContinuationAggregateReviewer
            from .git_publication import GitHubPublicationGrant,GitHubDraftPublisher,REPOSITORY
        except ImportError:fail('continuation_pool_api_unavailable')
        self.control.guard()
        path=self.target_directory/'aggregate';_mkdir(path)
        _mkdir(path/'git-state');_mkdir(path/'checkouts')
        grant=LocalRepositoryGrant(**{**self.job['repository'],'workspace_root':str(path/'checkouts')})
        original=LocalGitWorkspaceController(path/'git-state',grant=grant)
        final=DevelopmentDeliveryProof(self.worker,self.workspaces,self.operation)
        parent=self.task.snapshot()['state']['checkpoint']
        parent_id=None if parent is None else self._parent_checkpoint(parent)
        aggregate=AggregateDevelopmentDeliveryController(path/'delivery',task=self.task,final_proof=final,
            checkpoints=self.checkpoints,parent_checkpoint_id=parent_id,original_workspaces=original,
            final_intent_mode='provider_healthy_v1')
        specification=next(row for row in self.service.loaded['workflow']['jobs'] if row['name']==self.job['name'])
        aggregate.seal(message=specification['commit_message']);self.control.guard()
        # Preserve the workflow's narrower publication selection even if the
        # approved code manifest contained additional readable/editable paths.
        with aggregate._locked() as private:
            record=aggregate._read(private);aggregate._verified_seal(record)
            changes=record['capture']['request']['changes']
            require(all(row['path'] in specification['paths'] for row in changes),
                'continuation_pool_binding_changed')
        aggregate.complete_local();self.control.guard()
        _mkdir(path/'publications')
        publication_grant=GitHubPublicationGrant(self.service.owner_id,REPOSITORY,'main',self.job['repository']['base_commit'])
        publisher=GitHubDraftPublisher(aggregate.deliveries,path/'publications',grant=publication_grant,
            gh_executable=self.service.tools['gh']['path'],python_executable=self.service.tools['interpreter']['path'],
            authority=aggregate.publication_authority(publication_grant))
        published=aggregate.publish_and_handoff(publisher,title=specification['pr_title'],body=specification['pr_body'])
        require(published['status']=='awaiting_independent_review')
        self.control.guard()
        source=PublishedAggregateReviewSource(aggregate,publisher)
        reviewer=ContinuationAggregateReviewer(self.target_directory/'independent-review',source=source,
            configuration_path=self.service.path,root=self.service.root,owner_id=self.service.owner_id,
            board_endpoint=self.service.endpoint,name=self.job['name'],executable=self.service.tools['executable']['path'],
            cli_version=self.service.cli_version,interpreter=self.service.tools['interpreter']['path'],
            deadline=self.control.deadline,max_seconds=self.service.max_seconds)
        self.control.add_reviewer(reviewer);review_confirmed=False
        try:
            opinion=reviewer.run(should_stop=self.control.stopping)
            require(opinion.get('status')=='complete')
            receipt=reviewer.historical_receipt_pin()
            require(receipt['review']==opinion and receipt['source_operation_id']==self.operation
                and receipt['logical_task_id']==self.target['logical_task_id']
                and opinion['delivery_sha']==source.receipt['delivery_sha'],'continuation_pool_binding_changed')
            review_confirmed=True
        finally:
            if reviewer.pending or not review_confirmed:
                self.control.stop_failed=True
                self._resources_retained=True
                self.control.service._retained_resources.append((reviewer,self.resources.pop_all()))
                self.control.stop()
            else:self.control.remove_reviewer(reviewer)
        require(not reviewer.pending,'continuation_pool_cleanup_unknown')
        self.control.guard()
        return {'status':'independent_review_published','review_receipt':receipt}
    def _parent_checkpoint(self,parent):
        # Current generation has no private checkpoint when it returned final.
        # The parent is pinned before its construction, not found by scanning.
        with PrivateDirectory(self.directory) as private:value=_read(private,'parent.json')
        require(value['checkpoint_sha256']==parent['diff_sha256'])
        self.checkpoints.verify(value['checkpoint_id'])
        return value['checkpoint_id']
