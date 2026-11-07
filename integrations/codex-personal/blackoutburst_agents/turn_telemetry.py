"""Private, append-only, bounded journal of Codex turn boundaries (#51).

`codex_turn.execute_turn(on_turn_event=...)` reports only closed boundary
facts: an event kind, wall and monotonic clocks, the validated provider
session ID and, on the cleanup fact, the process return code. This module
binds them to validated turn identities (operation, ticket, profile, slot,
request) and appends one canonical JSON line per fact. After the transport
returns, a final `result` fact records the status, closed reason code and
verification booleans that `execute_turn` itself returned.

It is deliberately outside the native Linux source roster and no roster module
imports it. A non-roster caller (`development_queue_slots`) injects an
`observer` through explicit seams; the planner hands it its transport and an
identity callable. Nothing here reads model text, prompts, arguments,
credentials or diagnostics, and nothing here can retry a turn.

The observer is a pass-through: it calls the transport exactly once, with one
added `on_turn_event` keyword, and returns the very same result object. The
only exception is a failure while recording the `result` fact of a
`completed` result: then it returns a `blocked`/`callback_failed`,
`uncertain` copy without text, so a turn whose facts are not durable is never
accepted. It never retries, never calls the transport again and re-raises
transport exceptions unchanged.

Failure semantics (fail closed): every refusal or persistence failure raises
`DevelopmentError`. `execute_turn` then stops its process group and blocks
the turn as `callback_failed`; `RunStore` keeps a launched, unfinished turn
`uncertain`. Telemetry therefore never turns an uncertain turn into a success
and never re-invokes the transport. A failed write also makes the journal
unavailable for the rest of its life: a partial or unsynced line is never
followed by more records. A missing terminal record means "uncertain"; a later
`correlate` record may reference an earlier record, never fabricate a terminal.
"""
import errno
import fcntl
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, canonical, opaque

SCHEMA = 'turn-telemetry-v2'
EVENT_FIELDS = frozenset({'kind', 'wall', 'monotonic', 'session_id', 'exit'})
EVENT_KINDS = frozenset({'process_started', 'thread.started', 'turn.started', 'turn.completed', 'turn.failed',
                         'error', 'cleanup_confirmed', 'cleanup_failed'})
CLEANUPS = frozenset({'cleanup_confirmed', 'cleanup_failed'})
EXITS = frozenset({'zero', 'nonzero', 'signal', 'unknown'})
STATUSES = frozenset({'completed', 'blocked', 'cancelled'})
VERIFICATION = ('cleanup_confirmed', 'exit_zero', 'session_verified', 'tool_free', 'turn_completed')
TERMINALS = frozenset({'turn.completed', 'turn.failed', 'error'})
FAILURES = frozenset({'turn.failed', 'error'})
OPERATIONS = frozenset({'coder', 'reviewer', 'planner'})
CORRELATIONS = frozenset({'recovered_uncertain', 'reviewed_uncertain', 'superseded'})
TURN_FIELDS = frozenset({'schema', 'seq', 'kind', 'wall', 'monotonic', 'session_id', 'operation', 'ticket_id',
                         'agent_id', 'slot', 'request_id', 'pid', 'exit'})
RESULT_FIELDS = TURN_FIELDS - {'exit'} | {'status', 'reason', 'verification'}
IDENTITY = ('operation', 'ticket_id', 'agent_id', 'slot', 'request_id', 'pid')
# Strict order of one turn's facts; equal ranks are mutually exclusive.
RANK = {'process_started': 0, 'thread.started': 1, 'turn.started': 2, 'turn.completed': 3, 'turn.failed': 3,
        'error': 3, 'cleanup_confirmed': 4, 'cleanup_failed': 4, 'result': 5}
COMPLETE = ('process_started', 'thread.started', 'turn.started', 'turn.completed', 'cleanup_confirmed', 'result')
MAX_RECORD = 1024
MAX_RECORDS = 16384
MAX_BYTES = 8 * 1024 * 1024
HEADROOM = 256  # Records kept for turns already started when no new turn fits.
_NAME = re.compile(r'turns-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl')
_SLOT = re.compile(r'[A-Za-z0-9_.:-]{1,200}')
_REASON = re.compile(r'[a-z][a-z0-9_]{0,63}')
_SESSION = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def fail(code='turn_telemetry_invalid'):
    raise DevelopmentError(code)


def journal_name(journal_id):
    value = str(uuid.UUID(journal_id)) if type(journal_id) is str else None
    if value != journal_id:
        fail()
    return 'turns-' + value + '.jsonl'


def _clock(value):
    if type(value) is not float or not math.isfinite(value) or value < 0:
        fail()
    return value


def _session(value):
    if value is not None and (type(value) is not str or not _SESSION.fullmatch(value)
                              or str(uuid.UUID(value)) != value):
        fail()
    return value


def _exit_class(code):
    if code is None:
        return 'unknown'
    if type(code) is not int:
        fail()
    return 'zero' if code == 0 else 'nonzero' if code > 0 else 'signal'


def _identity(record):
    if record['operation'] not in OPERATIONS:
        fail()
    _clock(record['wall']); _clock(record['monotonic']); _session(record['session_id'])
    opaque(record['ticket_id']); opaque(record['agent_id']); opaque(record['request_id'])
    if type(record['slot']) is not str or not _SLOT.fullmatch(record['slot']):
        fail()
    if type(record['pid']) is not int or record['pid'] < 1:
        fail()


def _validate(record):
    """Closed schema of every stored line; anything else is refused."""
    if type(record) is not dict or record.get('schema') != SCHEMA:
        fail('turn_telemetry_corrupt')
    seq, kind = record.get('seq'), record.get('kind')
    if type(seq) is not int or seq < 1:
        fail('turn_telemetry_corrupt')
    try:
        if kind in EVENT_KINDS:
            if set(record) != TURN_FIELDS:
                fail()
            _identity(record)
            if (record['exit'] not in EXITS) if record['kind'] in CLEANUPS else record['exit'] is not None:
                fail()
        elif kind == 'result':
            if set(record) != RESULT_FIELDS:
                fail()
            _identity(record)
            verification = record['verification']
            if (record['status'] not in STATUSES or type(record['reason']) is not str
                    or not _REASON.fullmatch(record['reason']) or type(verification) is not dict
                    or set(verification) != set(VERIFICATION)
                    or any(value not in (True, False, None) or (value is not None and type(value) is not bool)
                           for value in verification.values())):
                fail()
        elif kind == 'correlation':
            if (set(record) != {'schema', 'seq', 'kind', 'wall', 'refers_to', 'reason'}
                    or type(record['refers_to']) is not int or not 1 <= record['refers_to'] < seq
                    or record['reason'] not in CORRELATIONS):
                fail()
            _clock(record['wall'])
        elif kind == 'journal_full':
            if set(record) != {'schema', 'seq', 'kind', 'wall'}:
                fail()
            _clock(record['wall'])
        else:
            fail()
    except DevelopmentError:
        fail('turn_telemetry_corrupt')
    return record


def _parse(raw, maximum):
    """Every complete line in order; a torn tail is reported, never repaired."""
    if len(raw) > maximum:
        fail('turn_telemetry_corrupt')
    lines = raw.split(b'\n')
    tail = lines.pop()
    records = []
    for line in lines:
        if not line or len(line) + 1 > MAX_RECORD:
            fail('turn_telemetry_corrupt')
        try:
            record = decode(line)
        except DevelopmentError:
            fail('turn_telemetry_corrupt')
        record = _validate(record)
        if record['seq'] != len(records) + 1:
            fail('turn_telemetry_corrupt')
        records.append(record)
    return records, bool(tail)


class TurnTelemetryJournal:
    """One bounded journal file. Thread-safe; one writer object per file.

    The file is created lazily by the first record, so a pass without a model
    turn leaves nothing behind. It is 0600 in a 0700 directory, opened with
    O_APPEND|O_NOFOLLOW|O_CREAT, locked exclusively, and fsynced per record.
    """

    def __init__(self, directory, journal_id, *, max_records=MAX_RECORDS, max_bytes=MAX_BYTES,
                 headroom=HEADROOM):
        if (any(type(value) is not int for value in (max_records, max_bytes, headroom))
                or not 0 <= headroom < max_records <= MAX_RECORDS
                or not MAX_RECORD <= max_bytes <= MAX_BYTES or headroom * MAX_RECORD >= max_bytes):
            fail()
        self.directory = Path(directory).absolute()
        self.name = journal_name(journal_id)
        self.max_records, self.max_bytes, self.headroom = max_records, max_bytes, headroom
        self._lock = threading.Lock()
        self._private = self._fd = None
        self.count = self.size = 0
        self.full = False
        self.unavailable = None
        self.closed = False

    @property
    def path(self):
        return self.directory / self.name

    def state(self):
        """Closed status for a pass result; never record contents."""
        with self._lock:
            return {'schema': SCHEMA, 'file': self.name, 'records': self.count, 'bytes': self.size,
                    'full': self.full, 'unavailable': self.unavailable}

    def _open(self):
        try:
            self._private = PrivateDirectory(self.directory, create=True)
            fd = os.open(self.name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=self._private.fd)
            try:
                PrivateDirectory.validate_file(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                reader = os.open(self.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self._private.fd)
                try:
                    if os.fstat(reader)[1:3] != os.fstat(fd)[1:3]:
                        fail('turn_telemetry_insecure')
                    chunks, total = [], 0
                    while total <= self.max_bytes:
                        chunk = os.read(reader, 65536)
                        if not chunk:
                            break
                        chunks.append(chunk); total += len(chunk)
                finally:
                    os.close(reader)
                raw = b''.join(chunks)
                records, torn = _parse(raw, self.max_bytes)
                if torn:
                    fail('turn_telemetry_corrupt')
                os.fsync(self._private.fd)
            except BaseException:
                os.close(fd)
                raise
        except BlockingIOError:
            fail('turn_telemetry_busy')
        except DevelopmentError as error:
            if error.code in ('turn_telemetry_busy', 'turn_telemetry_corrupt', 'turn_telemetry_insecure'):
                raise
            fail('turn_telemetry_unavailable' if error.code == 'private_directory_unavailable'
                 else 'turn_telemetry_insecure')
        except OSError as error:
            fail('turn_telemetry_insecure' if error.errno == errno.ELOOP else 'turn_telemetry_unavailable')
        self._fd, self.count, self.size = fd, len(records), len(raw)
        self.full = any(record['kind'] == 'journal_full' for record in records)

    def _write(self, raw):
        try:
            if os.write(self._fd, raw) != len(raw):
                raise OSError('short telemetry write')
            os.fsync(self._fd)
        except OSError:
            # The tail may now be torn or unsynced: never append after it.
            self.unavailable = 'turn_telemetry_unavailable'
            fail(self.unavailable)
        self.count += 1; self.size += len(raw)

    def _ready(self):
        # Called with the lock held. Any open refusal is sticky.
        if self.closed:
            fail('turn_telemetry_closed')
        if self.unavailable is not None:
            fail(self.unavailable)
        if self._fd is None:
            try:
                self._open()
            except DevelopmentError as error:
                self.unavailable = error.code
                raise

    def _append(self, body, *, starts_turn=False):
        with self._lock:
            self._ready()
            record = {'schema': SCHEMA, 'seq': self.count + 1, **body}
            raw = canonical(_validate(record)) + b'\n'
            if len(raw) > MAX_RECORD:
                fail()
            reserve = self.headroom if starts_turn else 0
            if (self.full and starts_turn) or self.count + 1 > self.max_records - reserve or (
                    self.size + len(raw) > self.max_bytes - reserve * MAX_RECORD):
                if not self.full and starts_turn:
                    self.full = True
                    marker = {'schema': SCHEMA, 'seq': self.count + 1, 'kind': 'journal_full',
                              'wall': _clock(body['wall'])}
                    self._write(canonical(marker) + b'\n')
                self.full = True
                fail('turn_telemetry_full')
            self._write(raw)
            return record['seq']

    def recorder(self, *, operation, slot, agent_id):
        """Low-level callback: (event, ticket_id, request_id) -> seq."""
        if operation not in OPERATIONS or type(slot) is not str or not _SLOT.fullmatch(slot):
            fail()
        opaque(agent_id)

        def identity(ticket_id, request_id, session_id):
            return {'operation': operation, 'ticket_id': opaque(ticket_id), 'agent_id': agent_id, 'slot': slot,
                    'request_id': opaque(request_id), 'pid': os.getpid(), 'session_id': _session(session_id)}

        def record(event, ticket_id, request_id):
            if type(event) is not dict or set(event) != EVENT_FIELDS or event['kind'] not in EVENT_KINDS:
                fail()
            cleanup = event['kind'] in CLEANUPS
            if not cleanup and event['exit'] is not None:
                fail()
            return self._append({'kind': event['kind'], 'wall': _clock(event['wall']),
                'monotonic': _clock(event['monotonic']), 'exit': _exit_class(event['exit']) if cleanup else None,
                **identity(ticket_id, request_id, event['session_id'])},
                starts_turn=event['kind'] == 'process_started')

        def result(value, ticket_id, request_id):
            """The returned result's closed facts only; never its text."""
            if type(value) is not dict or type(value.get('verification')) is not dict:
                fail()
            verification = value['verification']
            return self._append({'kind': 'result', 'wall': time.time(), 'monotonic': time.monotonic(),
                'status': value.get('status'), 'reason': value.get('reason'),
                'verification': {name: verification.get(name) for name in VERIFICATION},
                **identity(ticket_id, request_id, value.get('session_id'))})
        record.result = result
        return record

    def observer(self, *, operation, slot, agent_id):
        """Injected `_turn_event`: (transport, identity) -> observed transport.

        `identity()` returns (ticket_id, request_id) for the current turn.
        """
        record = self.recorder(operation=operation, slot=slot, agent_id=agent_id)

        def wrap(transport, identity):
            if not callable(transport) or not callable(identity):
                fail()

            def observed(*args, **kwargs):
                if 'on_turn_event' in kwargs:
                    fail()  # Never chained, replaced or hidden.
                ticket_id, request_id = identity()
                opaque(ticket_id); opaque(request_id)
                value = transport(*args, on_turn_event=lambda event: record(event, ticket_id, request_id),
                                  **kwargs)
                try:
                    record.result(value, ticket_id, request_id)
                except Exception:
                    if type(value) is dict and value.get('status') == 'completed':
                        return {**value, 'status': 'blocked', 'reason': 'callback_failed', 'text': None,
                                'uncertain': True}
                return value
            return observed
        return wrap

    def correlate(self, refers_to, reason, *, wall):
        """Recovery evidence about an earlier record. It is never a terminal."""
        if type(refers_to) is not int or reason not in CORRELATIONS:
            fail()
        with self._lock:
            self._ready()
            known = self.count
        if not 1 <= refers_to <= known:
            fail()
        return self._append({'kind': 'correlation', 'wall': _clock(wall), 'refers_to': refers_to, 'reason': reason})

    def close(self):
        with self._lock:
            self.closed = True
            if self._fd is not None:
                os.close(self._fd); self._fd = None
            if self._private is not None:
                self._private.close(); self._private = None


def read_journal(directory, journal_id, *, maximum=MAX_BYTES):
    """(records, torn_tail) of one journal; read-only, bounded, never repaired."""
    name = journal_name(journal_id)
    with PrivateDirectory(directory) as private:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=private.fd)
        try:
            PrivateDirectory.validate_file(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                fail('turn_telemetry_corrupt')
            raw = b''
            while len(raw) <= maximum:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                raw += chunk
        finally:
            os.close(fd)
    return _parse(raw, maximum)


def _summary(group):
    records = group['records']; first = records[0]
    kinds = [record['kind'] for record in records]
    violations = []
    if any(record[name] != first[name] for record in records for name in IDENTITY):
        violations.append('identity_changed')
    ranks = [RANK[kind] for kind in kinds]
    if any(after <= before for before, after in zip(ranks, ranks[1:])):
        violations.append('order')
    if any(after['monotonic'] < before['monotonic'] for before, after in zip(records, records[1:])):
        violations.append('monotonic_backwards')
    unset = object(); session = unset
    for record in records:
        if record['kind'] == 'process_started':
            if record['session_id'] is not None:
                violations.append('session_changed')
        elif session is unset:
            session = record['session_id']
        elif record['session_id'] != session:
            violations.append('session_changed')
    cleanup = next((record for record in records if record['kind'] in CLEANUPS), None)
    result = next((record for record in records if record['kind'] == 'result'), None)
    terminals = [kind for kind in kinds if kind in TERMINALS]
    launched = kinds[0] == 'process_started'
    if not launched and kinds != ['result']:
        violations.append('missing_process_started')
    summary = {name: first[name] for name in ('operation', 'slot', 'agent_id', 'ticket_id', 'request_id')}
    summary.update(session_id=None if session is unset else session, kinds=kinds, terminals=terminals,
        events={record['kind']: {'wall': record['wall'], 'monotonic': record['monotonic']}
                for record in records if record['kind'] != 'result'},
        exit=None if cleanup is None else cleanup['exit'],
        result=None if result is None else {key: result[key] for key in ('status', 'reason', 'verification')},
        violations=sorted(set(violations)), correlations=group['correlations'])
    if not violations and kinds == ['result'] and result['status'] != 'completed':
        outcome = 'failed'  # Refused before any process launch: no model call.
    elif (violations or result is None or cleanup is None or cleanup['kind'] != 'cleanup_confirmed'
          or not terminals or cleanup['exit'] in ('signal', 'unknown')):
        # No terminal, a killed process, unconfirmed cleanup or missing facts.
        outcome = 'uncertain'
    elif (tuple(kinds) == COMPLETE and result['status'] == 'completed' and result['reason'] == 'verified'
          and cleanup['exit'] == 'zero' and all(value is True for value in result['verification'].values())):
        outcome = 'completed'
    elif result['status'] == 'completed':
        outcome = 'uncertain'  # A claimed success the recorded facts do not support.
    else:
        outcome = 'failed'
    summary['outcome'] = outcome
    return summary


def turns(records):
    """Per-turn summaries checked for order, identity, session and clocks.

    A turn is `completed` only for the exact sequence process_started,
    thread.started, turn.started, turn.completed, cleanup_confirmed (zero exit)
    and a `result` that execute_turn itself returned as completed/verified
    with every verification boolean true. A received failure terminal or a
    natural nonzero exit with confirmed cleanup is `failed`; anything else,
    including any violation, stays `uncertain`.
    """
    groups, open_turns, by_seq = [], {}, {}
    for record in records:
        kind = record['kind']
        if kind == 'correlation':
            target = by_seq.get(record['refers_to'])
            if target is not None:
                target['correlations'].append({'seq': record['seq'], 'reason': record['reason']})
            continue
        if kind not in RANK:
            continue
        key = (record['slot'], record['request_id'])
        group = open_turns.get(key)
        if kind == 'process_started' or group is None:
            group = {'records': [], 'correlations': []}
            groups.append(group); open_turns[key] = group
        group['records'].append(record); by_seq[record['seq']] = group
        if kind == 'result':
            open_turns.pop(key, None)
    return [_summary(group) for group in groups]
