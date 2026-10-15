import copy
import json
from pathlib import Path
import sys
import unittest

from blackoutburst_agents.usage_observation import UsageError, normalize_response, unavailable, validate_snapshot


def response():
    return {'accountId': 'PRIVATE_ACCOUNT', 'planType': 'PRIVATE_PLAN', 'rateLimitResetCredits': {'credits': [{'id': 'PRIVATE_CREDIT'}]},
        'ordinaryUsageAllowed': True, 'rateLimits': {}, 'rateLimitsByLimitId': {'codex': {
            'limitId': 'codex', 'primary': {'usedPercent': 25, 'windowDurationMins': 300, 'resetsAt': 1800000000},
            'secondary': None, 'credits': {'balance': 'PRIVATE_BALANCE'}}}}


class UsageObservationTests(unittest.TestCase):
    def test_only_metadata_is_retained_and_nullable_windows_are_not_zero(self):
        value = normalize_response(response(), '1.2.3')
        self.assertEqual(value['data_status'], 'observed')
        self.assertIsNone(value['buckets'][0]['secondary'])
        self.assertNotIn('PRIVATE', json.dumps(value))
        self.assertEqual(value['buckets'][0]['primary']['used_percent'], 25)

    def test_missing_map_allows_legacy_but_empty_or_malformed_map_does_not(self):
        raw = response(); raw['rateLimits'] = raw['rateLimitsByLimitId']['codex']; raw.pop('rateLimitsByLimitId')
        self.assertEqual(normalize_response(raw)['buckets'][0]['source_view'], 'legacy')
        for bad in ({}, [], False, 'PRIVATE'):
            raw['rateLimitsByLimitId'] = bad
            self.assertEqual(normalize_response(raw)['data_status'], 'unavailable')

    def test_zero_and_hundred_are_valid_only_when_explicit_integers(self):
        for good in (0, 100):
            raw = response(); raw['rateLimitsByLimitId']['codex']['primary']['usedPercent'] = good
            self.assertEqual(normalize_response(raw)['buckets'][0]['primary']['used_percent'], good)
        for bad in (True, False, -1, 101, 10.0, '0', None, float('nan')):
            raw = response(); raw['rateLimitsByLimitId']['codex']['primary']['usedPercent'] = bad
            self.assertEqual(normalize_response(raw)['data_status'], 'unavailable')

    def test_missing_measurement_is_unavailable_not_remaining_hundred(self):
        raw = response(); raw['rateLimitsByLimitId']['codex']['primary'] = None
        value = normalize_response(raw)
        self.assertEqual(value['data_status'], 'unavailable'); self.assertEqual(value['buckets'], [])
        self.assertIsNone(value['ordinary_usage_allowed'])

    def test_nullable_duration_reset_and_allowed_remain_unknown(self):
        raw = response(); raw.pop('ordinaryUsageAllowed')
        raw['rateLimitsByLimitId']['codex']['primary'] = {'usedPercent': 0}
        value = normalize_response(raw)
        self.assertEqual(value['buckets'][0]['primary'], {'used_percent': 0, 'window_minutes': None, 'resets_at': None})
        self.assertIsNone(value['ordinary_usage_allowed'])

    def test_schema_rejects_unknown_fields_and_unsafe_types(self):
        good = normalize_response(response())
        for location, field, value in [('root', 'accountId', 'PRIVATE'), ('root', 'schema_version', True),
                ('root', 'ordinary_usage_allowed', 1), ('bucket', 'limit_id', '<script>'),
                ('bucket', 'spend_control_reached', 'false'), ('window', 'resets_at', 1.0)]:
            bad = copy.deepcopy(good)
            target = bad if location == 'root' else bad['buckets'][0] if location == 'bucket' else bad['buckets'][0]['primary']
            target[field] = value
            with self.assertRaises((UsageError, TypeError)): validate_snapshot(bad)

    def test_unknown_reason_is_not_interpreted_as_recovery(self):
        raw = response(); raw['ordinaryUsageAllowed'] = None
        raw['rateLimitsByLimitId']['codex']['rateLimitReachedType'] = 'future_service_reason'
        value = normalize_response(raw)
        self.assertIsNone(value['buckets'][0]['reached_reason']); self.assertIsNone(value['ordinary_usage_allowed'])

    def test_server_and_adapter_validation_are_identical(self):
        root = str(Path(__file__).resolve().parents[3])
        sys.path.insert(0, root)
        try:
            from usage_observations import validate_snapshot as server_validate, UsageError as ServerError
            values = [normalize_response(response()), unavailable('timeout')]
            for value in values:
                self.assertEqual(validate_snapshot(value), server_validate(value))
            for path, replacement in [('schema_version', True), ('buckets', []), ('cli_version', 'PRIVATE PATH')]:
                bad = copy.deepcopy(values[0]); bad[path] = replacement
                with self.assertRaises(UsageError): validate_snapshot(bad)
                with self.assertRaises(ServerError): server_validate(bad)
        finally: sys.path.remove(root)
