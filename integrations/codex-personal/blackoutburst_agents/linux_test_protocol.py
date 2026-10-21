"""Pure request/input codecs for a future Linux named Python backend.

These are bounded data contracts, not host observations, grants, qualification
receipts, or an executor. Importing this module performs no filesystem/network
operation. A caller-supplied expected plan hash never establishes its existence
or trust. The live locked snapshot and actual plan must be checked separately by
an eventual controller before any effect. Darwin TestRequest is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import uuid

from .dev_journal import decode
from .dev_manifest import (DevelopmentError, canonical, check_content, digest, opaque,
                           relative_path, sha256, validate_manifest)
from .linux_input_snapshot import validate_input_snapshot


REQUEST_SCHEMA = 'blackoutburst-linux-test-request-v1'
INPUT_SCHEMA = 'blackoutburst-linux-test-inputs-v1'
MAGIC = b'BB_LINUX_INPUT_V1\n'
MAX_FILES = 128
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_INPUT_ENTRIES = 256
MAX_HEADER_BYTES = 128 * 1024
MAX_REQUEST_BYTES = 8192
EXECUTION_REQUEST_SCHEMA = 'blackoutburst-linux-execution-request-v1'
REQUEST_FIELDS = frozenset({'schema', 'operation_id', 'workflow_id', 'manifest_sha256',
    'plan_sha256', 'scope_sha256', 'input_snapshot_sha256', 'input_packet_sha256',
    'planner_snapshot_sha256', 'root', 'root_identity', 'name', 'timeout_seconds',
    'max_output_bytes'})


def _fail():
    raise DevelopmentError('invalid_linux_test_protocol')


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def linux_backend_id(expected_plan_sha256):
    """Name only; deriving this identifier never creates or selects a backend."""
    return 'linux-python-v1-' + sha256(expected_plan_sha256)[:40]


def _input_context(manifest, snapshot, planner_snapshot):
    validate_manifest(manifest)
    validate_input_snapshot(snapshot, manifest=manifest, planner_snapshot=planner_snapshot)
    paths = sorted(snapshot['files'])
    directories = { '/'.join(parts[:index]) for path in paths
                   for parts in [relative_path(path)] for index in range(1, len(parts)) }
    if (not 1 <= len(paths) <= MAX_FILES
            or len(paths) + len(directories) + 1 > MAX_INPUT_ENTRIES):
        _fail()
    return paths, digest(canonical(snapshot))


def encode_input_packet(contents, *, manifest, snapshot, planner_snapshot):
    """Copy exact immutable bytes into a bounded canonical packet, in memory."""
    paths, snapshot_hash = _input_context(manifest, snapshot, planner_snapshot)
    if type(contents) is not dict or set(contents) != set(paths):
        _fail()
    entries, parts, total = [], [], 0
    for path in paths:
        data, expected = contents[path], snapshot['files'][path]
        if (type(data) is not bytes or len(data) != expected['bytes']
                or len(data) > min(MAX_FILE_BYTES, manifest['budgets']['max_file_bytes'])
                or digest(data) != expected['sha256']):
            _fail()
        check_content(data)
        entries.append({'path': path, 'offset': total, 'bytes': len(data), 'sha256': expected['sha256']})
        total += len(data)
        if total > min(MAX_TOTAL_BYTES, manifest['budgets']['max_read_bytes']):
            _fail()
        parts.append(data)
    header = canonical({'schema': INPUT_SCHEMA, 'snapshot_sha256': snapshot_hash, 'files': entries})
    if len(header) > MAX_HEADER_BYTES:
        _fail()
    return MAGIC + len(header).to_bytes(8, 'big') + header + b''.join(parts)


def decode_input_packet(packet, *, manifest, snapshot, planner_snapshot):
    """Validate all framing/paths/offsets/hashes; never open a path or import code."""
    paths, snapshot_hash = _input_context(manifest, snapshot, planner_snapshot)
    prefix = len(MAGIC) + 8
    if (type(packet) is not bytes or not prefix < len(packet) <= prefix + MAX_HEADER_BYTES + MAX_TOTAL_BYTES
            or packet[:len(MAGIC)] != MAGIC):
        _fail()
    size = int.from_bytes(packet[len(MAGIC):prefix], 'big')
    if not 1 <= size <= MAX_HEADER_BYTES or prefix + size > len(packet):
        _fail()
    header_bytes, data = packet[prefix:prefix+size], packet[prefix+size:]
    header = decode(header_bytes)
    if (type(header) is not dict or set(header) != {'schema', 'snapshot_sha256', 'files'}
            or header['schema'] != INPUT_SCHEMA or header['snapshot_sha256'] != snapshot_hash
            or type(header['files']) is not list or len(header['files']) != len(paths)
            or canonical(header) != header_bytes
            or len(data) > min(MAX_TOTAL_BYTES, manifest['budgets']['max_read_bytes'])):
        _fail()
    result, offset = {}, 0
    for path, entry in zip(paths, header['files']):
        expected = snapshot['files'][path]
        if (type(entry) is not dict or set(entry) != {'path', 'offset', 'bytes', 'sha256'}
                or type(entry['path']) is not str or entry['path'] != path
                or not _integer(entry['offset'], 0, MAX_TOTAL_BYTES) or entry['offset'] != offset
                or not _integer(entry['bytes'], 0, min(MAX_FILE_BYTES, manifest['budgets']['max_file_bytes']))
                or entry['bytes'] != expected['bytes'] or entry['sha256'] != expected['sha256']):
            _fail()
        part = data[offset:offset+entry['bytes']]
        if len(part) != entry['bytes'] or digest(part) != expected['sha256']:
            _fail()
        check_content(part)
        result[path] = part
        offset += entry['bytes']
    if offset != len(data):
        _fail()
    return result


def _request_shape(value):
    if (type(value) is not dict or set(value) != REQUEST_FIELDS or value['schema'] != REQUEST_SCHEMA
            or type(value['root']) is not str or not value['root'].startswith('/')
            or len(value['root'].encode('utf-8', 'surrogatepass')) > 4096
            or str(Path(value['root'])) != value['root'] or value['root'] == '/'
            or any(part in ('', '.', '..') for part in value['root'].split('/')[1:])
            or any(ord(char) < 32 or ord(char) == 127 for char in value['root'])
            or type(value['root_identity']) is not list or len(value['root_identity']) != 2
            or not _integer(value['root_identity'][0], 0, 2**64-1)
            or not _integer(value['root_identity'][1], 1, 2**64-1)
            or not _integer(value['timeout_seconds'], 1, 60)
            or not _integer(value['max_output_bytes'], 0, 1048576)):
        _fail()
    for field in ('operation_id', 'manifest_sha256', 'plan_sha256', 'scope_sha256',
                  'input_snapshot_sha256', 'input_packet_sha256', 'planner_snapshot_sha256'):
        sha256(value[field])
    opaque(value['name'], 'invalid_linux_test_protocol', 64)
    try:
        if type(value['workflow_id']) is not str or str(uuid.UUID(value['workflow_id'])) != value['workflow_id']:
            _fail()
        value['root'].encode('utf-8', 'strict')
    except (ValueError, UnicodeError):
        _fail()
    if len(canonical(value)) > MAX_REQUEST_BYTES:
        _fail()
    return value


@dataclass(frozen=True)
class LinuxTestRequest:
    """Separate from Darwin v1, and never printed or sent to the model."""
    operation_id: str
    workflow_id: str
    manifest_sha256: str
    plan_sha256: str
    scope_sha256: str
    input_snapshot_sha256: str
    input_packet_sha256: str
    planner_snapshot_sha256: str
    root: str
    root_identity: tuple[int, int]
    name: str
    timeout_seconds: int
    max_output_bytes: int

    def document(self):
        if type(self.root_identity) is not tuple:
            _fail()
        return _request_shape({'schema': REQUEST_SCHEMA, **vars(self), 'root_identity': list(self.root_identity)})


def validate_linux_request(value, *, manifest, snapshot, planner_snapshot,
                           expected_plan_sha256, input_packet):
    """Bind request to expected documents and actual bytes, not to host authority."""
    if type(value) is LinuxTestRequest:
        value = value.document()
    _request_shape(value)
    _, snapshot_hash = _input_context(manifest, snapshot, planner_snapshot)
    expected_plan_sha256 = sha256(expected_plan_sha256)
    suite = next((test for test in manifest['tests'] if test['name'] == value['name']), None)
    if (suite is None or suite['backend_id'] != linux_backend_id(expected_plan_sha256)
            or value['workflow_id'] != snapshot['operation_id']
            or value['manifest_sha256'] != digest(canonical(manifest))
            or value['plan_sha256'] != expected_plan_sha256
            or value['scope_sha256'] != digest(canonical(manifest['scope']))
            or value['input_snapshot_sha256'] != snapshot_hash
            or value['planner_snapshot_sha256'] != planner_snapshot['sha256']
            or value['root'] != manifest['root'] or value['root_identity'] != manifest['root_identity']
            or any(value[key] != suite[key] for key in ('timeout_seconds', 'max_output_bytes'))):
        _fail()
    decode_input_packet(input_packet, manifest=manifest, snapshot=snapshot, planner_snapshot=planner_snapshot)
    if value['input_packet_sha256'] != digest(input_packet):
        _fail()
    return decode(canonical(value))


def linux_execution_backend_id(plan_sha256):
    """Execution and input-only names deliberately cannot alias one another."""
    return 'linux-python-exec-v1-' + sha256(plan_sha256)[:40]


def validate_execution_request_shape(value):
    extra = {'input_plan_sha256', 'fence_sha256'}
    if (type(value) is not dict or set(value) != REQUEST_FIELDS | extra
            or value.get('schema') != EXECUTION_REQUEST_SCHEMA):
        _fail()
    projected = {name: item for name, item in value.items() if name not in extra}
    projected['schema'] = REQUEST_SCHEMA
    _request_shape(projected)
    for name in extra:
        sha256(value[name])
    if len(canonical(value)) > MAX_REQUEST_BYTES:
        _fail()
    return value


def validate_linux_execution_request(value, *, manifest, snapshot, planner_snapshot,
                                     execution_plan_sha256, input_plan_sha256, input_packet):
    """Exact data binding only; no persistent document reconstructs a live lease."""
    validate_execution_request_shape(value)
    _, snapshot_hash = _input_context(manifest, snapshot, planner_snapshot)
    suite = next((test for test in manifest['tests'] if test['name'] == value['name']), None)
    if (suite is None or suite['backend_id'] != linux_execution_backend_id(execution_plan_sha256)
            or value['plan_sha256'] != execution_plan_sha256
            or value['input_plan_sha256'] != input_plan_sha256
            or value['workflow_id'] != snapshot['operation_id']
            or value['manifest_sha256'] != digest(canonical(manifest))
            or value['scope_sha256'] != digest(canonical(manifest['scope']))
            or value['input_snapshot_sha256'] != snapshot_hash
            or value['input_packet_sha256'] != digest(input_packet)
            or value['planner_snapshot_sha256'] != planner_snapshot['sha256']
            or value['fence_sha256'] != snapshot['fence_record_sha256']
            or value['root'] != manifest['root'] or value['root_identity'] != manifest['root_identity']
            or any(value[name] != suite[name] for name in ('timeout_seconds', 'max_output_bytes'))):
        _fail()
    decode_input_packet(input_packet, manifest=manifest, snapshot=snapshot, planner_snapshot=planner_snapshot)
    return decode(canonical(value))
