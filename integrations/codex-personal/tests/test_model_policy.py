"""No provider/board calls; all candidate names and observations are synthetic."""
import copy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blackoutburst_agents.model_policy import (MAX_POLICY_BYTES, ModelPolicy, PolicyError, PROVIDER,
    TASK_CLASSES, connection_identifier, decision_from_dict, load_policy, parse_policy,
    routing_configuration, select_model, validate_pinned, validate_routing_configuration)

NOW = 1700001000
CONTEXT = 'a' * 64
SCOPE = {'owner_id': 'owner-a', 'connection_id': 'local-chatgpt-a'}


def source():
    models = {}
    for name in ('focused', 'workhorse', 'deep', 'critic'):
        models[name] = {'reasoning_efforts': ['low', 'high'], 'availability': {
            **SCOPE, 'model': name, 'provider': PROVIDER, 'status': 'verified',
            'observed_at': NOW - 10, 'reference': 'proof-' + name}}
    return {'schema_version': 1, 'policy_id': 'personal-models', 'version': 1,
        **SCOPE, 'provider': PROVIDER, 'proof_max_age_seconds': 3600,
        'models': models, 'routes': {
            task: {'model': name, 'reasoning_effort': None}
            for task, name in zip(TASK_CLASSES, models)}}


def policy(value=None):
    return parse_policy(source() if value is None else value, **SCOPE)


def choose(selected=None, **overrides):
    args = dict(SCOPE, task_class='simple', context_hash=CONTEXT,
                profile_version=3, now=NOW)
    args.update(overrides)
    return select_model(policy() if selected is None else selected, **args)


def validate(decision, selected=None, **overrides):
    args = dict(SCOPE, context_hash=CONTEXT, profile_version=3, now=NOW)
    args.update(overrides)
    return validate_pinned(decision, policy() if selected is None else selected, **args)


def rehash(fields):
    fields = dict(fields)
    fields.pop('decision_hash', None)
    raw = json.dumps(fields, sort_keys=True, ensure_ascii=False,
                     separators=(',', ':'), allow_nan=False).encode()
    return decision_from_dict({**fields, 'decision_hash': hashlib.sha256(raw).hexdigest()})


class ModelPolicyTests(unittest.TestCase):
    def assert_code(self, code, callback):
        with self.assertRaises(PolicyError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(code, str(caught.exception))

    def test_four_explicit_routes(self):
        for task, model in zip(TASK_CLASSES, ('focused', 'workhorse', 'deep', 'critic')):
            with self.subTest(task=task):
                result = choose(task_class=task)
                self.assertEqual(model, result.model)
                self.assertEqual('class_route', result.reason)
                self.assertEqual(PROVIDER, result.provider)
                self.assertEqual(CONTEXT, result.context_hash)
                self.assertEqual(3, result.profile_version)
                self.assertEqual('proof-' + model, result.availability_reference)

    def test_manual_override_supersedes_profile(self):
        result = choose(override_model='critic', preferred_model='foreign',
                        preferred_provider='paid-api')
        self.assertEqual(('critic', 'manual_override'), (result.model, result.reason))

    def test_profile_preference_and_empty_preference(self):
        result = choose(preferred_model='focused', preferred_provider=PROVIDER)
        self.assertEqual(('focused', 'profile_preference'), (result.model, result.reason))
        self.assertEqual(choose(), choose(preferred_model='', preferred_provider=''))

    def test_unknown_profile_candidate_is_not_silently_ignored(self):
        self.assert_code('model_not_allowed', lambda: choose(preferred_model='unlisted'))
        self.assert_code('preferred_provider_unsupported',
                         lambda: choose(preferred_provider='paid-api'))

    def test_no_provider_or_other_account_fallback(self):
        value = source()
        value['provider'] = 'paid-api'
        self.assert_code('provider_not_supported', lambda: policy(value))
        self.assert_code('policy_scope_mismatch', lambda: choose(owner_id='owner-b'))
        self.assert_code('policy_scope_mismatch', lambda: choose(connection_id='local-chatgpt-b'))

    def test_availability_proof_scope_cannot_be_relabelled(self):
        for field in ('owner_id', 'connection_id', 'model', 'provider'):
            with self.subTest(field=field):
                value = source()
                value['models']['workhorse']['availability'][field] = 'foreign'
                self.assert_code('availability_scope_mismatch', lambda: policy(value))
        value = source()
        value['owner_id'] = 'owner-b'
        self.assert_code('availability_scope_mismatch',
            lambda: parse_policy(value, owner_id='owner-b', connection_id=SCOPE['connection_id']))

    def test_expired_or_unknown_proof_never_selects_alternative(self):
        for status, stamp, reference, code in (
                ('unknown', None, None, 'availability_unverified'),
                ('unavailable', NOW - 5, 'refusal', 'model_unavailable'),
                ('verified', NOW - 3601, 'old', 'availability_expired'),
                ('verified', NOW + 1, 'future', 'availability_in_future')):
            with self.subTest(status=status, code=code):
                value = source()
                value['models']['workhorse']['availability'].update(
                    status=status, observed_at=stamp, reference=reference)
                self.assert_code(code, lambda: choose(policy(value)))
                # A manual choice is a distinct user decision, not a retry path.
                self.assertEqual('focused', choose(policy(value), override_model='focused').model)

    def test_expiry_boundary_and_no_future_access_promise(self):
        value = source()
        value['models']['workhorse']['availability']['observed_at'] = NOW - 3600
        selected = policy(value)
        result = choose(selected)
        self.assertIs(result, validate(result, selected))
        self.assert_code('availability_expired', lambda: validate(result, selected, now=NOW + 1))
        self.assertNotIn('guaranteed', result.to_dict())
        self.assertNotIn('remaining_quota', result.to_dict())

    def test_reasoning_requires_model_allowlist_and_transport_support(self):
        value = source()
        value['routes']['simple']['reasoning_effort'] = 'high'
        selected = policy(value)
        self.assert_code('reasoning_not_supported', lambda: choose(selected))
        result = choose(selected, supported_reasoning_efforts=('high',))
        self.assertEqual('high', result.reasoning_effort)
        self.assertIs(result, validate(result, selected, supported_reasoning_efforts=('high',)))
        self.assert_code('reasoning_not_supported', lambda: validate(result, selected))
        self.assert_code('reasoning_not_allowed', lambda: choose(override_model='focused',
            override_reasoning_effort='max', supported_reasoning_efforts=('max',)))

    def test_reasoning_override_must_have_manual_model(self):
        self.assert_code('override_model_required', lambda: choose(override_reasoning_effort='high'))
        result = choose(override_model='focused', override_reasoning_effort='low',
                        supported_reasoning_efforts=['low'])
        self.assertEqual('low', result.reasoning_effort)

    def test_preference_does_not_inherit_effort_of_a_different_model(self):
        value = source()
        value['routes']['simple']['reasoning_effort'] = 'high'
        result = choose(policy(value), preferred_model='focused')
        self.assertIsNone(result.reasoning_effort)

    def test_unknown_effort_or_capabilities_refused(self):
        self.assert_code('invalid_reasoning_effort',
            lambda: choose(override_model='focused', override_reasoning_effort='invented'))
        for capability in ('high', [None], ['invented'], [True]):
            with self.subTest(capability=capability):
                self.assert_code('invalid_transport_capabilities',
                    lambda: choose(supported_reasoning_efforts=capability))

    def test_policy_and_decision_are_immutable_and_detached(self):
        value = source()
        selected = policy(value)
        value['routes']['simple']['model'] = 'deep'
        editable = selected.to_dict()
        editable['routes']['simple']['model'] = 'deep'
        self.assertEqual('workhorse', choose(selected).model)
        with self.assertRaises(FrozenInstanceError):
            selected.canonical_json = '{}'
        result = choose(selected)
        with self.assertRaises(FrozenInstanceError):
            result.model = 'deep'
        receipt = result.to_dict()
        receipt['model'] = 'deep'
        self.assertEqual('workhorse', result.model)

    def test_deterministic_json_order_hash_and_serialization(self):
        value = source()
        reordered = dict(reversed(list(value.items())))
        self.assertEqual(policy(value).policy_hash, policy(reordered).policy_hash)
        result = choose(policy(reordered))
        self.assertEqual(result, choose(policy(value)))
        restored = decision_from_dict(json.loads(json.dumps(result.to_dict())))
        self.assertIs(restored, validate(restored))

    def test_pin_rejects_changes_to_context_profile_or_policy(self):
        result = choose()
        self.assert_code('context_changed', lambda: validate(result, context_hash='b' * 64))
        self.assert_code('context_changed', lambda: validate(result, profile_version=4))
        for field, replacement in (('version', 2), ('policy_id', 'different')):
            value = source()
            value[field] = replacement
            self.assert_code('policy_changed', lambda: validate(result, policy(value)))
        value = source()
        value['routes']['simple']['model'] = 'deep'
        self.assert_code('policy_changed', lambda: validate(result, policy(value)))
        value = source()
        value['models']['workhorse']['availability']['observed_at'] = NOW - 9
        self.assert_code('policy_changed', lambda: validate(result, policy(value)))

    def test_pinned_selection_is_not_recomputed_with_a_new_timestamp(self):
        result = choose()
        with patch('blackoutburst_agents.model_policy.select_model', side_effect=AssertionError('must not reselect')):
            self.assertIs(result, validate(result, now=NOW + 2))
        self.assertEqual(NOW, result.selected_at)
        self.assertNotEqual(result.decision_hash, choose(now=NOW + 2).decision_hash)

    def test_pin_rejects_tampering_even_if_model_is_allowlisted(self):
        result = choose()
        self.assert_code('decision_hash_mismatch', lambda: validate(replace(result, model='deep')))
        fields = result.to_dict()
        fields['model'] = 'deep'
        self.assert_code('invalid_decision', lambda: validate(rehash(fields)))
        fields = result.to_dict()
        fields['availability_reference'] = 'another-proof'
        self.assert_code('invalid_decision', lambda: validate(rehash(fields)))

    def test_pin_cannot_move_to_other_owner_or_connection(self):
        for field in ('owner_id', 'connection_id'):
            fields = choose().to_dict()
            fields[field] = 'foreign'
            self.assert_code('decision_scope_mismatch', lambda: validate(rehash(fields)))

    def test_invalid_decision_types_are_safe(self):
        for field, value, code in (
                ('reason', [], 'invalid_decision'), ('reason', 'automatic_fallback', 'invalid_decision'),
                ('schema_version', True, 'invalid_decision'),
                ('selected_at', True, 'invalid_decision'),
                ('selected_at', 10**500, 'invalid_decision'),
                ('task_class', {}, 'invalid_decision')):
            with self.subTest(field=field, value=str(value)[:20]):
                fields = choose().to_dict()
                fields[field] = value
                self.assert_code(code, lambda: validate(rehash(fields)))
        self.assert_code('decision_in_future', lambda: validate(choose(now=NOW + 1)))

    def test_input_type_and_identifier_bounds(self):
        for value in ('', 'x' * 129, '../bad model', '--config', 'bad\nmodel', True, {}):
            with self.subTest(value=value):
                self.assert_code('invalid_model', lambda: choose(override_model=value))
        for value in (True, 0, -1, float('nan'), float('inf'), 10**500):
            self.assert_code('invalid_time', lambda: choose(now=value))
        for value in (True, 0, 1.5, '1'):
            self.assert_code('invalid_profile_version', lambda: choose(profile_version=value))
        self.assert_code('invalid_context_hash', lambda: choose(context_hash='A' * 64))
        self.assert_code('invalid_task_class', lambda: choose(task_class='please fix everything'))

    def test_strict_schema_and_unknown_credential_fields(self):
        for change in (
                lambda v: v.update(token='secret'),
                lambda v: v['models']['workhorse'].update(api_key='secret'),
                lambda v: v['routes']['simple'].update(fallback='deep'),
                lambda v: v['routes'].pop('review'),
                lambda v: v.update(version=True),
                lambda v: v.update(proof_max_age_seconds=True),
                lambda v: v.update(proof_max_age_seconds=30 * 86400 + 1),
                lambda v: v['models']['workhorse'].update(reasoning_efforts=['low', 'low']),
                lambda v: v['models']['workhorse']['availability'].update(status='unknown'),
                lambda v: v['models']['workhorse']['availability'].update(observed_at=10**500)):
            value = source()
            change(value)
            self.assert_code('invalid_policy', lambda: policy(value))
        value = source()
        value['schema_version'] = 2
        self.assert_code('unsupported_policy_version', lambda: policy(value))

    def test_size_and_model_count_bounds(self):
        value = source()
        original = value['models']['focused']
        value['models'] = {}
        for index in range(101):
            name = 'candidate-' + str(index)
            candidate = copy.deepcopy(original)
            candidate['availability']['model'] = name
            value['models'][name] = candidate
        self.assert_code('invalid_policy', lambda: policy(value))
        self.assert_code('invalid_policy', lambda: ModelPolicy(' ' * (MAX_POLICY_BYTES + 1)).to_dict())

    def test_serialization_rejects_extra_fields_or_non_mapping(self):
        self.assert_code('invalid_decision', lambda: decision_from_dict([]))
        self.assert_code('invalid_decision', lambda: decision_from_dict({**choose().to_dict(), 'token': 'secret'}))

    def test_no_model_calls_or_environment_discovery(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'unused', 'MODEL': 'unlisted'}):
            self.assertEqual('workhorse', choose().model)

    def test_connection_binding_excludes_session_but_separates_device_and_endpoint(self):
        scope = {'owner_id': 'owner', 'agent_id': 'agent', 'board_endpoint': 'https://one.test/mcp',
                 'board_session_id': 'session-a'}
        registration = {'identity': {'owner_id': 'owner', 'id': 'agent', 'device_id': 'device-a'}}
        original = connection_identifier(scope, registration)
        self.assertEqual(original, connection_identifier({**scope, 'board_session_id': 'session-b'}, registration))
        self.assertNotEqual(original, connection_identifier({**scope, 'board_endpoint': 'https://two.test/mcp'}, registration))
        self.assertNotEqual(original, connection_identifier(scope,
            {'identity': {**registration['identity'], 'device_id': 'device-b'}}))
        self.assert_code('policy_scope_mismatch', lambda: connection_identifier({**scope, 'owner_id': 'other'}, registration))

    def test_stored_routing_snapshot_hash_and_class_are_validated(self):
        routing = routing_configuration(policy(), 'review', owner_id=SCOPE['owner_id'])
        self.assertEqual(routing, validate_routing_configuration(routing))
        modified = copy.deepcopy(routing)
        modified['policy']['routes']['review']['model'] = 'focused'
        self.assert_code('invalid_routing_configuration', lambda: validate_routing_configuration(modified))
        self.assert_code('model_policy_required', lambda: routing_configuration(None, 'review', owner_id=SCOPE['owner_id']))
        self.assert_code('invalid_task_class', lambda: routing_configuration(policy(), None, owner_id=SCOPE['owner_id']))


@unittest.skipUnless(os.name == 'posix', 'private files require POSIX')
class PolicyFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'policy.json'
        self.path.write_text(json.dumps(source()), encoding='utf-8')
        self.path.chmod(0o600)

    def assert_code(self, code, callback):
        with self.assertRaises(PolicyError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertNotIn(str(self.path), str(caught.exception))

    def load(self):
        return load_policy(self.path, **SCOPE)

    def test_private_file_success(self):
        self.assertEqual(policy(), self.load())

    def test_duplicate_json_keys_in_nested_candidate_are_rejected(self):
        raw = json.dumps(source()).replace('"status": "verified"', '"status": "unknown", "status": "verified"', 1)
        self.path.write_text(raw)
        self.assert_code('invalid_policy', self.load)

    def test_nonfinite_invalid_utf8_and_deep_json(self):
        for data in (b'{"invalid": NaN}', b'\xff', b'[' * 1200 + b']' * 1200):
            self.path.write_bytes(data)
            self.assert_code('invalid_policy', self.load)

    def test_permissive_file_mode_and_other_os_owner(self):
        self.path.chmod(0o644)
        self.assert_code('insecure_policy_file', self.load)
        self.path.chmod(0o600)
        with patch('blackoutburst_agents.model_policy.os.geteuid', return_value=os.geteuid() + 1):
            self.assert_code('insecure_policy_file', self.load)

    def test_no_symlink_or_hardlink(self):
        linked = self.path.with_name('linked')
        linked.symlink_to(self.path)
        self.assert_code('policy_unavailable', lambda: load_policy(linked, **SCOPE))
        linked.unlink()
        os.link(self.path, linked)
        self.assert_code('insecure_policy_file', self.load)

    def test_fifo_does_not_block(self):
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        self.assert_code('insecure_policy_file', self.load)

    def test_too_large_missing_or_directory(self):
        self.path.write_bytes(b' ' * (MAX_POLICY_BYTES + 1))
        self.assert_code('policy_too_large', self.load)
        self.path.unlink()
        self.assert_code('policy_unavailable', self.load)
        self.path.mkdir(mode=0o700)
        self.assert_code('insecure_policy_file', self.load)


if __name__ == '__main__':
    unittest.main()
