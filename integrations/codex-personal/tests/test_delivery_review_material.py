"""Pure factual renderer: strict evidence and byte-exact whole-pair budgets."""
from copy import deepcopy
import json

import pytest

from blackoutburst_agents.delivery_review_material import (
    MAX_MATERIAL_BYTES, render_review_material, validate_review_evidence)
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest


def evidence(*, aggregate=False, names=('test',)):
    tests = [dict(name=name, operation_id='test-' + name, script_sha256='1' * 64,
        timeout_seconds=10, max_output_bytes=4096, result='passed', cleanup_confirmed=True,
        output_bytes=2, output_sha256='2' * 64, test_receipt_sha256='3' * 64,
        native_receipt_sha256='4' * 64, native_exit_code=0, source_released=True) for name in names]
    return dict(schema='controller-test-evidence-v2', snapshot_sha256='5' * 64,
        receipts=[dict(name=name, sha256='6' * 64) for name in names],
        review=dict(schema='verified-delivery-review-evidence-v1',
            kind='aggregate' if aggregate else 'development', operation_id='operation',
            source_snapshot_sha256='7' * 64 if aggregate else '5' * 64,
            planner_snapshot_sha256='8' * 64, runtime_cleanup_sha256='9' * 64,
            original_public_base='a' * 40 if aggregate else None,
            lineage_sha256='b' * 64 if aggregate else None, tests=tests))


def inputs(**changes):
    return dict(operation_id='operation', base_commit='a' * 40, delivery_sha='b' * 40,
        tree_sha='c' * 40, snapshot_sha256='5' * 64, test_evidence=evidence(),
        files=[dict(path='module.py', mode='100644', before=b'a\n', after=b'b\n')]) | changes


def render(**changes):
    return json.loads(render_review_material(**inputs(**changes)))


def test_complete_material_joins_real_controller_receipts_and_detaches_inputs():
    values = inputs(); before = deepcopy(values)
    text = render_review_material(**values); result = json.loads(text)
    assert values == before and len(text.encode()) <= MAX_MATERIAL_BYTES
    assert result['schema'] == 'delivery-review-material-v1'
    assert result['content_trust'] == 'untrusted_source_data'
    assert result['test_metadata_status'] == 'detailed'
    assert result['test_evidence_sha256'] == digest(canonical(values['test_evidence']))
    assert result['test_binding'] == {key: value for key, value in values['test_evidence']['review'].items() if key != 'tests'}
    assert result['tests'] == [values['test_evidence']['review']['tests'][0] | {'controller_receipt_sha256': '6' * 64}]
    file = result['files'][0]
    assert file['before_sha256'] == digest(b'a\n') and file['after_sha256'] == digest(b'b\n')
    assert file['before_bytes'] == file['after_bytes'] == 2
    assert file['content'] == dict(status='complete', before_text='a\n', after_text='b\n')
    assert file['unified_diff'] == '--- a/module.py\n+++ b/module.py\n@@ -1 +1 @@\n-a\n+b\n'
    assert result['completeness'] == 'complete' and result['pull_request'] is None
    detached = validate_review_evidence(values['test_evidence']); detached['tests'][0]['name'] = 'changed'
    assert values == before


def test_aggregate_keeps_tested_final_source_separate_from_delivered_original_base_snapshot():
    value = evidence(aggregate=True); result = render(test_evidence=value)
    assert result['snapshot_sha256'] == value['snapshot_sha256'] == '5' * 64
    assert result['test_binding']['source_snapshot_sha256'] == '7' * 64
    assert result['test_binding']['original_public_base'] == 'a' * 40
    assert result['test_binding']['lineage_sha256'] == 'b' * 64
    with pytest.raises(DevelopmentError):
        render(base_commit='d' * 40, test_evidence=value)


def test_legacy_is_explicit_without_fabricating_test_details():
    old = dict(schema='controller-test-evidence-v1', snapshot_sha256='5' * 64,
        receipts=[dict(name='legacy', sha256='6' * 64)])
    result = render(test_evidence=old)
    assert result['test_metadata_status'] == 'legacy_unavailable' and result['test_binding'] is None
    assert result['tests'] == [dict(name='legacy', controller_receipt_sha256='6' * 64)]
    assert result['test_evidence_sha256'] == digest(canonical(old))
    with pytest.raises(DevelopmentError):
        validate_review_evidence(old)
    old['review'] = evidence()['review']
    with pytest.raises(DevelopmentError):
        render(test_evidence=old)


def test_omission_is_pair_complete_deterministic_and_keeps_every_descriptor_diff_and_test():
    large_before = (b'same line\n' * 1800) + b'a'
    large_after = (b'same line\n' * 1800) + b'b'
    files = [dict(path='z.py', mode='100644', before=b'z\n', after=b'Z\n'),
        dict(path='a.py', mode='100755', before=large_before, after=large_after)]
    result = render(files=files, test_evidence=evidence(names=('second', 'first')))
    assert result == render(files=list(reversed(files)), test_evidence=evidence(names=('second', 'first')))
    assert [row['path'] for row in result['files']] == ['a.py', 'z.py']
    assert [row['name'] for row in result['tests']] == ['first', 'second']
    omitted, small = result['files']
    assert result['completeness'] == 'limited'
    assert omitted['content'] == dict(status='omitted', reason='budget')
    assert omitted['before_bytes'] == len(large_before) and omitted['after_bytes'] == len(large_after)
    assert omitted['before_sha256'] == digest(large_before) and omitted['after_sha256'] == digest(large_after)
    assert omitted['unified_diff'].endswith('-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n')
    assert small['content'] == dict(status='complete', before_text='z\n', after_text='Z\n')


def test_no_newline_at_eof_and_non_lf_separators_are_unambiguous():
    result = render(files=[dict(path='a.py', mode='100644', before=b'a', after=b'b')])
    assert result['files'][0]['unified_diff'] == (
        '--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n')
    same = 'a\rb\u2028c'
    result = render(files=[dict(path='a.py', mode='100644', before=same.encode(), after=(same + 'd').encode())])
    assert '@@ -1 +1 @@' in result['files'][0]['unified_diff']


def largest_mandatory_payload(character='x'):
    # At these sizes complete pairs cannot fit. The mandatory exact diff grows
    # monotonically; exercise the real production constant without replacing it.
    low, high = 14000 // len(character.encode()), 24000 // len(character.encode())
    while low < high:
        middle = (low + high + 1) // 2
        try:
            render_review_material(**inputs(files=[dict(path='a.py', mode='100644',
                before=b'', after=(character * middle).encode())]))
        except DevelopmentError as error:
            assert error.code == 'delivery_review_material_too_large'
            high = middle - 1
        else:
            low = middle
    return character * low


def test_exact_24000_utf8_bytes_fit_and_24001_mandatory_bytes_refuse():
    payload = largest_mandatory_payload()
    text = render_review_material(**inputs(files=[dict(path='a.py', mode='100644', before=b'', after=payload.encode())]))
    assert len(text.encode('utf-8')) == 24000
    assert json.loads(text)['files'][0]['content'] == dict(status='omitted', reason='budget')
    with pytest.raises(DevelopmentError, match='delivery_review_material_too_large'):
        render_review_material(**inputs(files=[dict(path='a.py', mode='100644', before=b'', after=(payload + 'x').encode())]))


def test_multibyte_and_json_escape_overhead_count_encoded_bytes_not_characters():
    payload = largest_mandatory_payload('é')
    options = inputs(files=[dict(path='a.py', mode='100644', before=b'', after=payload.encode())])
    text = render_review_material(**options)
    # Add one ASCII byte if the two-byte character leaves a one-byte remainder.
    if len(text.encode()) == 23999:
        payload += 'x'; options['files'][0]['after'] = payload.encode()
        text = render_review_material(**options)
    assert len(text.encode()) == 24000 and len(text) < 24000
    options['files'][0]['after'] += b'x'
    with pytest.raises(DevelopmentError, match='delivery_review_material_too_large'):
        render_review_material(**options)
    escaped = render(files=[dict(path='a.py', mode='100644', before=b'"\\\t\n', after='é"\\\t\n'.encode())])
    assert escaped['files'][0]['content']['after_text'] == 'é"\\\t\n'
    assert len(canonical(escaped)) <= 24000


@pytest.mark.parametrize('field,value', [
    ('name', True), ('timeout_seconds', True), ('timeout_seconds', 0), ('timeout_seconds', 601),
    ('max_output_bytes', 1.0), ('max_output_bytes', -1), ('output_bytes', 4097),
    ('output_bytes', False), ('result', 'failed'), ('cleanup_confirmed', 1),
    ('source_released', False), ('native_exit_code', False), ('native_exit_code', 1),
    ('script_sha256', 'z' * 64), ('native_receipt_sha256', None), ('operation_id', '../escape'),
    ('stdout', 'forbidden raw output'), ('script_path', '/private/verifier.py'),
])
def test_v2_rejects_coercions_unknown_sensitive_fields_and_unproven_result(field, value):
    value_in = evidence(); value_in['review']['tests'][0][field] = value
    with pytest.raises(DevelopmentError):
        validate_review_evidence(value_in)


@pytest.mark.parametrize('change', [
    lambda e: e.update(extra=True), lambda e: e['review'].update(extra=True),
    lambda e: e['review'].update(schema='unknown'), lambda e: e['review'].update(kind=True),
    lambda e: e['review'].update(original_public_base='a' * 40),
    lambda e: e['review'].update(lineage_sha256='a' * 64),
    lambda e: e['review'].update(source_snapshot_sha256='d' * 64),
    lambda e: e['review']['tests'].append(deepcopy(e['review']['tests'][0])),
    lambda e: e['receipts'].append(deepcopy(e['receipts'][0])),
    lambda e: e['receipts'][0].update(name='different'),
    lambda e: e['receipts'][0].update(extra=True), lambda e: e.update(receipts=[]),
])
def test_v2_requires_closed_binding_names_and_whole_receipt_projection(change):
    value = evidence(); change(value)
    with pytest.raises(DevelopmentError):
        validate_review_evidence(value)


@pytest.mark.parametrize('change', [
    dict(operation_id='other'), dict(snapshot_sha256='f' * 64), dict(tree_sha=True),
    dict(base_commit='a' * 64), dict(files=[]),
    dict(files=[dict(path='../a', mode='100644', before=b'a', after=b'b')]),
    dict(files=[dict(path='.env', mode='100644', before=b'a', after=b'b')]),
    dict(files=[dict(path='a', mode='120000', before=b'a', after=b'b')]),
    dict(files=[dict(path='a', mode='100644', before='a', after=b'b')]),
    dict(files=[dict(path='a', mode='100644', before=b'a', after=b'b', before_sha256='f' * 64)]),
])
def test_renderer_rejects_forged_bindings_paths_modes_and_unknown_file_fields(change):
    with pytest.raises(DevelopmentError):
        render_review_material(**inputs(**change))


@pytest.mark.parametrize('bad', [b'\xff', b'a\x00b', b'-----BEGIN PRIVATE KEY-----', b'Bearer ' + b'A' * 24])
def test_unsafe_content_is_refused_even_when_full_pair_would_be_omitted(bad):
    before = (b'unchanged\n' * 5000) + bad
    after = before + b'change'
    with pytest.raises(DevelopmentError):
        render(files=[dict(path='a.py', mode='100644', before=before, after=after)])


def test_exact_github_pr_binding_optional_and_no_extra_fields():
    pr = dict(number=82, url='https://github.com/Blackoutburst/agent-board/pull/82',
        head_sha='b' * 40, base_sha='a' * 40, draft=True)
    assert render(pull_request=pr)['pull_request'] == pr
    for changed in (pr | {'head_sha': 'c' * 40}, pr | {'base_sha': 'c' * 40},
                    pr | {'url': 'https://attacker.invalid/pull/82'}, pr | {'number': True},
                    pr | {'draft': 1}, pr | {'extra': True}):
        with pytest.raises(DevelopmentError):
            render(pull_request=changed)


def test_quote_escaping_overhead_hits_the_same_utf8_ceiling():
    low, high = 7000, 13000  # Optional pairs cannot fit in this range.
    while low < high:
        middle = (low + high + 1) // 2
        try:
            render_review_material(**inputs(files=[dict(path='a.py', mode='100644', before=b'', after=b'"' * middle)]))
        except DevelopmentError as error:
            assert error.code == 'delivery_review_material_too_large'
            high = middle - 1
        else:
            low = middle
    payload = b'"' * low
    text = render_review_material(**inputs(files=[dict(path='a.py', mode='100644', before=b'', after=payload)]))
    if len(text.encode()) == 23999:
        payload += b'x'
        text = render_review_material(**inputs(files=[dict(path='a.py', mode='100644', before=b'', after=payload)]))
    assert len(text.encode()) == 24000
    assert json.loads(text)['files'][0]['after_bytes'] == len(payload)
    with pytest.raises(DevelopmentError, match='delivery_review_material_too_large'):
        render_review_material(**inputs(files=[dict(path='a.py', mode='100644', before=b'', after=payload + b'x')]))


def test_all_thirty_two_tests_are_mandatory_and_an_extra_test_refuses():
    value = evidence(names=tuple('test-' + str(n) for n in range(32)))
    result = render(test_evidence=value)
    assert len(result['tests']) == 32
    assert {row['name'] for row in result['tests']} == {row['name'] for row in value['receipts']}
    invalid = evidence(names=tuple('test-' + str(n) for n in range(33)))
    with pytest.raises(DevelopmentError):
        validate_review_evidence(invalid)
    forged = deepcopy(value); forged['review']['tests'] = forged['review']['tests'][:-1]
    with pytest.raises(DevelopmentError):
        validate_review_evidence(forged)
