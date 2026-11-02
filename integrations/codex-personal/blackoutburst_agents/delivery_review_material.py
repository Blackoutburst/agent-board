"""Closed factual review material from already controller-verified source bytes.

This module performs no IO, admission, execution or receipt verification. Its
shape/hash checks are not test authority: callers retain their concrete proof
and fresh source guards before and after collecting the supplied facts.
"""
from copy import deepcopy
import difflib
import re

from .dev_manifest import DevelopmentError, canonical, check_content, digest, relative_path

MAX_MATERIAL_BYTES = 24000
MAX_EVIDENCE_BYTES = 32768
MAX_CHANGED_FILES = 128
EVIDENCE_SCHEMA = 'controller-test-evidence-v2'
REVIEW_SCHEMA = 'verified-delivery-review-evidence-v1'
MATERIAL_SCHEMA = 'delivery-review-material-v1'
REVIEW_FIELDS = frozenset({'schema', 'kind', 'operation_id', 'source_snapshot_sha256',
    'planner_snapshot_sha256', 'runtime_cleanup_sha256', 'original_public_base',
    'lineage_sha256', 'tests'})
TEST_FIELDS = frozenset({'name', 'operation_id', 'script_sha256', 'timeout_seconds',
    'max_output_bytes', 'result', 'cleanup_confirmed', 'output_bytes', 'output_sha256',
    'test_receipt_sha256', 'native_receipt_sha256', 'native_exit_code', 'source_released'})


def _require(value, code='delivery_review_evidence_invalid'):
    if not value:
        raise DevelopmentError(code)


def _hash(value):
    _require(type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None)


def _identifier(value, maximum=200):
    _require(type(value) is str and re.fullmatch(
        r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,' + str(maximum - 1) + '}', value) is not None)
    check_content(value.encode('utf-8'))


def _git_sha(value):
    _require(type(value) is str and re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', value) is not None)


def _receipts(evidence, schema):
    fields = {'schema', 'snapshot_sha256', 'receipts'}
    if schema == EVIDENCE_SCHEMA:
        fields.add('review')
    _require(type(evidence) is dict and set(evidence) == fields
        and type(evidence['schema']) is str and evidence['schema'] == schema)
    _hash(evidence['snapshot_sha256'])
    rows = evidence['receipts']
    _require(type(rows) is list and 1 <= len(rows) <= 32)
    names = set()
    for row in rows:
        _require(type(row) is dict and set(row) == {'name', 'sha256'})
        _identifier(row['name'])
        _hash(row['sha256'])
        _require(row['name'] not in names)
        names.add(row['name'])
    _require(len(canonical(evidence)) <= MAX_EVIDENCE_BYTES)
    return names


def validate_review_evidence(evidence):
    """Validate the whole closed v2 projection; return a detached review copy.

    Receipt names/hashes and the development/aggregate snapshot distinction are
    checked structurally. The existing concrete test guard must still validate
    the complete envelope against actual planner/Broker/native/Runtime proof.
    """
    names = _receipts(evidence, EVIDENCE_SCHEMA)
    review = evidence['review']
    _require(type(review) is dict and set(review) == REVIEW_FIELDS
        and type(review['schema']) is str and review['schema'] == REVIEW_SCHEMA
        and type(review['kind']) is str and review['kind'] in ('development', 'aggregate'))
    _identifier(review['operation_id'])
    for key in ('source_snapshot_sha256', 'planner_snapshot_sha256', 'runtime_cleanup_sha256'):
        _hash(review[key])
    if review['kind'] == 'development':
        _require(review['original_public_base'] is None and review['lineage_sha256'] is None
            and review['source_snapshot_sha256'] == evidence['snapshot_sha256'])
    else:
        _git_sha(review['original_public_base'])
        _hash(review['lineage_sha256'])
    tests = review['tests']
    _require(type(tests) is list and 1 <= len(tests) <= 32)
    observed = set()
    for row in tests:
        _require(type(row) is dict and set(row) == TEST_FIELDS)
        _identifier(row['name'], 64)
        _identifier(row['operation_id'])
        _require(row['name'] not in observed)
        observed.add(row['name'])
        for key in ('script_sha256', 'output_sha256', 'test_receipt_sha256', 'native_receipt_sha256'):
            _hash(row[key])
        _require(type(row['timeout_seconds']) is int and 1 <= row['timeout_seconds'] <= 600
            and type(row['max_output_bytes']) is int and 0 <= row['max_output_bytes'] <= 1048576
            and type(row['output_bytes']) is int and 0 <= row['output_bytes'] <= row['max_output_bytes']
            and type(row['result']) is str and row['result'] == 'passed'
            and row['cleanup_confirmed'] is True and row['source_released'] is True
            and type(row['native_exit_code']) is int and row['native_exit_code'] == 0)
    _require(observed == names)
    return deepcopy(review)


def _lf_lines(text):
    """Git text lines use LF; CR and Unicode separators remain source bytes."""
    parts = text.split('\n')
    return [line + '\n' for line in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _diff(path, before, after):
    lines = difflib.unified_diff(_lf_lines(before), _lf_lines(after),
        fromfile='a/' + path, tofile='b/' + path)
    return ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n'
        for line in lines)


def _publication(value, *, base_commit, delivery_sha):
    if value is None:
        return None
    _require(type(value) is dict and set(value) == {'number', 'url', 'head_sha', 'base_sha', 'draft'},
        'delivery_review_material_invalid')
    number = value['number']
    _require(type(number) is int and 1 <= number < 2**63
        and type(value['url']) is str
        and value['url'] == 'https://github.com/Blackoutburst/agent-board/pull/' + str(number)
        and type(value['head_sha']) is str and type(value['base_sha']) is str
        and value['head_sha'] == delivery_sha and value['base_sha'] == base_commit
        and type(value['draft']) is bool, 'delivery_review_material_invalid')
    return deepcopy(value)


def render_review_material(*, operation_id, base_commit, delivery_sha, tree_sha,
                           snapshot_sha256, test_evidence, files, pull_request=None):
    """Render complete mandatory facts, then whole before/after pairs if they fit.

    All inputs must already belong to the same fresh controller-verified seal.
    The returned UTF-8 JSON never exceeds 24000 bytes. Mandatory descriptors,
    tests, bindings and exact diffs are never omitted; overflowing them refuses.
    Full source pairs are optional only as a pair, with explicit budget reasons.
    """
    _identifier(operation_id)
    for value in (base_commit, delivery_sha, tree_sha):
        _git_sha(value)
    _require(len(base_commit) == len(delivery_sha) == len(tree_sha), 'delivery_review_material_invalid')
    _hash(snapshot_sha256)
    _require(type(test_evidence) is dict and test_evidence.get('snapshot_sha256') == snapshot_sha256)
    if test_evidence.get('schema') == EVIDENCE_SCHEMA:
        review = validate_review_evidence(test_evidence)
        _require(review['operation_id'] == operation_id, 'delivery_review_material_invalid')
        if review['kind'] == 'aggregate':
            _require(review['original_public_base'] == base_commit, 'delivery_review_material_invalid')
        by_name = {row['name']: row['sha256'] for row in test_evidence['receipts']}
        tests = [{**row, 'controller_receipt_sha256': by_name[row['name']]}
            for row in sorted(review['tests'], key=lambda row: row['name'])]
        binding = {key: value for key, value in review.items() if key != 'tests'}
        metadata_status = 'detailed'
    else:
        _receipts(test_evidence, 'controller-test-evidence-v1')
        tests = [{'name': row['name'], 'controller_receipt_sha256': row['sha256']}
            for row in sorted(test_evidence['receipts'], key=lambda row: row['name'])]
        binding = None
        metadata_status = 'legacy_unavailable'
    _require(type(files) is list and 1 <= len(files) <= MAX_CHANGED_FILES,
        'delivery_review_material_invalid')
    paths = set()
    prepared = []
    for row in files:
        _require(type(row) is dict and set(row) == {'path', 'mode', 'before', 'after'},
            'delivery_review_material_invalid')
        relative_path(row['path'])
        check_content(row['path'].encode('utf-8'))
        _require(row['path'] not in paths and type(row['mode']) is str and row['mode'] in ('100644', '100755')
            and type(row['before']) is bytes and type(row['after']) is bytes,
            'delivery_review_material_invalid')
        paths.add(row['path'])
        before, after = check_content(row['before']), check_content(row['after'])
        descriptor = {'path': row['path'], 'mode': row['mode'],
            'before_sha256': digest(row['before']), 'before_bytes': len(row['before']),
            'after_sha256': digest(row['after']), 'after_bytes': len(row['after']),
            'unified_diff': _diff(row['path'], before, after),
            'content': {'status': 'omitted', 'reason': 'budget'}}
        prepared.append((descriptor, before, after))
    prepared.sort(key=lambda item: item[0]['path'])
    document = {'schema': MATERIAL_SCHEMA, 'content_trust': 'untrusted_source_data',
        'operation_id': operation_id, 'base_commit': base_commit, 'delivery_sha': delivery_sha,
        'tree_sha': tree_sha, 'snapshot_sha256': snapshot_sha256,
        'test_evidence_sha256': digest(canonical(test_evidence)),
        'test_evidence_schema': test_evidence['schema'], 'test_metadata_status': metadata_status,
        'test_binding': binding, 'tests': tests,
        'pull_request': _publication(pull_request, base_commit=base_commit, delivery_sha=delivery_sha),
        'completeness': 'limited', 'files': [item[0] for item in prepared]}
    _require(len(canonical(document)) <= MAX_MATERIAL_BYTES, 'delivery_review_material_too_large')
    for descriptor, before, after in prepared:
        omitted = descriptor['content']
        descriptor['content'] = {'status': 'complete', 'before_text': before, 'after_text': after}
        complete = all(row['content']['status'] == 'complete' for row in document['files'])
        document['completeness'] = 'complete' if complete else 'limited'
        if len(canonical(document)) > MAX_MATERIAL_BYTES:
            descriptor['content'] = omitted
            document['completeness'] = 'limited'
    raw = canonical(document)
    _require(len(raw) <= MAX_MATERIAL_BYTES, 'delivery_review_material_too_large')
    return raw.decode('utf-8', 'strict')
