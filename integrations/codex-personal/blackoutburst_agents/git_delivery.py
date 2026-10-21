"""Explicit local snapshot -> commit object -> independent readonly review export.

This composes the frozen workspace controller without advancing its HEAD/index.
Controller callbacks, not this component or declared hashes, verify test receipts.
No Board permission, model, test command, credential, network or PR is implemented.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import time
import unicodedata

from .dev_journal import PrivateDirectory, decode
from .dev_manifest import (DevelopmentError, RepositoryFiles, absolute_directory,
                           canonical, check_content, digest, identity, opaque,
                           relative_path, sha256, signature)
from .git_workspace import (LocalGitWorkspaceController, MAX_CHECKOUT_BYTES,
    MAX_CHECKOUT_FILES, MAX_CHECKOUT_FILE_BYTES, _ancestry, _commit, _directory,
    _operation, _path, _scope)
from .work_journal import _scope as board_scope

SCHEMA = 'local-git-delivery-v1'
MAX_RECORD = 8 * 1024 * 1024
MAX_CHANGES = 128


def fail(code='git_delivery_invalid'):
    raise DevelopmentError(code)


def copy(value):
    return decode(canonical(value))


def object_id(data, kind, base):
    algorithm = hashlib.sha1 if len(base) == 40 else hashlib.sha256
    return algorithm(kind.encode() + b' ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def _commit_bytes(tree, base, message, epoch):
    if (type(epoch) is not int or not 0 <= epoch < 2**63 or type(message) is not str
            or not message.strip() or '\0' in message or len(message.encode()) > 4096):
        fail('git_delivery_commit_changed')
    who = 'Blackoutburst local controller <local@example.invalid> ' + str(epoch) + ' +0000'
    return ('tree '+tree+'\nparent '+base+'\nauthor '+who+'\ncommitter '+who+
            '\n\n'+message.rstrip()+'\n').encode()


def _inventory(root, *, readonly=False):
    """Bound every physical entry, not Git's ignored/untracked view."""
    files, pins, total, aliases = {}, [], 0, set()
    with_root = RepositoryFiles(root)
    try:
        def walk(descriptor, prefix, depth):
            nonlocal total
            if depth > 32: fail('git_delivery_tree_refused')
            before = os.fstat(descriptor)
            if readonly and stat.S_IMODE(before.st_mode) != 0o500:
                fail('git_delivery_not_readonly')
            names = sorted(os.listdir(descriptor))
            if len(names) > MAX_CHECKOUT_FILES: fail('git_delivery_tree_too_large')
            for name in names:
                if not prefix and name == '.git' and not readonly:
                    continue
                path = prefix + name
                relative_path(path)
                alias = unicodedata.normalize('NFC', path).casefold()
                if alias in aliases: fail('git_delivery_path_alias')
                aliases.add(alias)
                if len(aliases) > MAX_CHECKOUT_FILES: fail('git_delivery_tree_too_large')
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    fail('git_delivery_tree_refused')
                pins.append([path, list(signature(info))])
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    try:
                        if signature(os.fstat(child)) != signature(info): fail('git_delivery_tree_changed')
                        walk(child, path + '/', depth + 1)
                        if signature(os.fstat(child)) != signature(info): fail('git_delivery_tree_changed')
                    finally: os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    data, observed = RepositoryFiles.read_at(descriptor, name, MAX_CHECKOUT_FILE_BYTES)
                    mode = '100755' if info.st_mode & 0o111 else '100644'
                    if readonly and stat.S_IMODE(info.st_mode) != (0o500 if mode == '100755' else 0o400):
                        fail('git_delivery_not_readonly')
                    total += len(data)
                    if total > MAX_CHECKOUT_BYTES: fail('git_delivery_tree_too_large')
                    if observed != signature(info): fail('git_delivery_tree_changed')
                    files[path] = (mode, data)
                else: fail('git_delivery_tree_refused')
                if signature(os.stat(name, dir_fd=descriptor, follow_symlinks=False)) != signature(info):
                    fail('git_delivery_tree_changed')
            if signature(os.fstat(descriptor)) != signature(before): fail('git_delivery_tree_changed')
        walk(with_root.fd, '', 0)
        with_root.verify()
        return files, digest(canonical(pins))
    except OSError:
        fail('git_delivery_tree_refused')
    finally: with_root.close()


def _changes(value):
    if type(value) is not list or not 1 <= len(value) <= MAX_CHANGES:
        fail('git_delivery_changes_refused')
    result, aliases = [], set()
    for row in value:
        if type(row) is not dict or set(row) != {'path', 'sha256', 'mode'}:
            fail('git_delivery_changes_refused')
        relative_path(row['path'])
        alias = unicodedata.normalize('NFC', row['path']).casefold()
        if alias in aliases: fail('git_delivery_changes_refused')
        aliases.add(alias)
        if row['sha256'] is None:
            if row['mode'] is not None: fail('git_delivery_changes_refused')
        elif row['mode'] not in ('100644', '100755'):
            fail('git_delivery_changes_refused')
        else: sha256(row['sha256'])
        result.append(copy(row))
    return sorted(result, key=lambda row: row['path'])


class LocalGitDeliveryController:
    """Cooperative controller boundary; readonly modes are not an owner-UID sandbox."""
    def __init__(self, workspaces, delivery_root, review_root, *, test_guard=None):
        if type(workspaces) is not LocalGitWorkspaceController: fail('git_delivery_controller_required')
        self.workspaces, self.test_guard = workspaces, test_guard
        self.delivery_root, self.review_root = _path(delivery_root), _path(review_root)
        self.pins = [_directory(path, private=True) for path in (self.delivery_root, self.review_root)]
        self._roots()

    def _roots(self):
        self.workspaces._roots()
        paths = (self.delivery_root, self.review_root)
        if [_directory(p, private=True) for p in paths] != self.pins: fail('git_delivery_root_changed')
        chains = [_ancestry(p) for p in (*paths, self.workspaces.state_root,
                  self.workspaces.workspace_root, self.workspaces.repository)]
        if any(a[-1] in b or b[-1] in a for n, a in enumerate(chains) for b in chains[n+1:]):
            fail('git_delivery_roots_overlap')

    @contextmanager
    def _record(self, root, operation_id, *, create=False):
        self._roots(); path = root / _operation(operation_id); fresh = False
        if create:
            try:
                os.mkdir(path, 0o700)
                with PrivateDirectory(root) as parent: os.fsync(parent.fd)
                fresh = True
            except FileExistsError: pass
        with PrivateDirectory(path) as private:
            fd = os.open('delivery.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=private.fd)
            try:
                private.validate_file(fd)
                try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: fail('git_delivery_busy')
                yield private, fresh
            finally: os.close(fd)

    def _workspace(self, private, scope=None):
        value = self.workspaces._read(private)
        if value['phase'] != 'ready': fail('git_delivery_workspace_not_ready')
        if scope is not None and _scope(scope) != value['request']['scope']: fail('git_delivery_scope_changed')
        self.workspaces._ready(value)
        root = Path(value['request']['checkout'])
        if self.workspaces._git(['diff-index','--cached','--raw','--no-ext-diff',
                self.workspaces.grant['base_commit'],'--'], cwd=root): fail('git_delivery_index_changed')
        return value, root

    def _metadata(self, root):
        metadata = self.workspaces._copy_metadata(root)
        if any(unicodedata.normalize('NFC', row[0]).casefold().startswith('hooks/') for row in metadata[1]):
            fail('git_delivery_hooks_refused')
        return digest(canonical(metadata))

    def _base_entries(self, root):
        raw = self.workspaces._git(['ls-tree', '-r', '-z', '--full-tree', self.workspaces.grant['base_commit']], cwd=root)
        result = {}
        for entry in raw.rstrip(b'\0').split(b'\0'):
            head, path = entry.split(b'\t', 1); mode, kind, oid = head.split()
            path = path.decode('utf-8', 'strict'); relative_path(path)
            if kind != b'blob' or mode not in (b'100644', b'100755'): fail('git_delivery_base_refused')
            result[path] = (mode.decode(), oid.decode())
        return result

    def _snapshot(self, root, changes):
        files, _pins = _inventory(root)
        base = self.workspaces.grant['base_commit']; before = self._base_entries(root)
        after = {p: (mode, object_id(data, 'blob', base)) for p, (mode, data) in files.items()}
        actual = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
        if actual != [c['path'] for c in changes]: fail('git_delivery_undeclared_change')
        for c in changes:
            if c['path'] not in files:
                if c['sha256'] is not None: fail('git_delivery_postimage_changed')
            else:
                mode, data = files[c['path']]
                check_content(data)
                if c['sha256'] != digest(data) or c['mode'] != mode: fail('git_delivery_postimage_changed')
        entries = [dict(path=p, mode=mode, bytes=len(data), sha256=digest(data),
                        blob_oid=object_id(data, 'blob', base), payload='file-' + str(n))
                   for n, (p, (mode, data)) in enumerate(sorted(files.items()))]
        return files, entries

    @staticmethod
    def _read(private):
        value = decode(private.read('delivery.json', MAX_RECORD))
        if (type(value) is not dict or set(value) != {'schema','phase','request','snapshot',
                'snapshot_sha256','metadata_sha256','seal','reason'} or value['schema'] != SCHEMA
                or value['phase'] not in ('capturing','captured','sealing','sealed')
                or value['snapshot_sha256'] != digest(canonical(value['snapshot']))):
            fail('git_delivery_record_refused')
        return value

    def capture(self, *, operation_id, scope, changes):
        changes = _changes(changes)
        with self.workspaces._locked(operation_id) as (workspace_private, _):
            source, root = self._workspace(workspace_private, scope)
            request = dict(operation_id=operation_id, scope=_scope(scope), changes=changes,
                           workspace_request_sha256=source['request_sha256'], checkout=str(root),
                           base_commit=self.workspaces.grant['base_commit'])
            with self._record(self.delivery_root, operation_id, create=True) as (private, fresh):
                if not fresh:
                    value = self._read(private)
                    if value['request'] != request: fail('git_delivery_request_conflict')
                    self._validate(private, value, source, root)
                    return copy(value['snapshot'])
                metadata = self._metadata(root)
                files, entries = self._snapshot(root, changes)
                snapshot = dict(schema='local-captured-tree-v1', request=request, files=entries)
                value = dict(schema=SCHEMA, phase='capturing', request=request, snapshot=snapshot,
                    snapshot_sha256=digest(canonical(snapshot)), metadata_sha256=metadata, seal=None, reason='')
                private.create('delivery.json', canonical(value))
                for row in entries: private.create(row['payload'], files[row['path']][1])
                if self._snapshot(root, changes)[1] != entries or self._metadata(root) != metadata:
                    fail('git_delivery_tree_changed')
                self._roots()
                value['phase'] = 'captured'; private.replace('delivery.json', canonical(value))
                return copy(snapshot)

    def _validate(self, private, value, source, root):
        self._roots()
        request = value['request']
        if (type(request) is not dict or set(request) != {'operation_id','scope','changes',
                    'workspace_request_sha256','checkout','base_commit'}
                or value['phase'] not in ('captured','sealed') or request.get('operation_id') != private.path.name
                or request.get('scope') != source['request']['scope'] or request.get('checkout') != str(root)
                or request.get('base_commit') != self.workspaces.grant['base_commit']
                or request.get('workspace_request_sha256') != source['request_sha256']
                or value['snapshot'].get('request') != request): fail('git_delivery_interrupted_or_changed')
        changes = _changes(request['changes'])
        entries = self._snapshot(root, changes)[1]
        if (value['snapshot'] != dict(schema='local-captured-tree-v1', request=request, files=entries)
                or self._metadata(root) != value['metadata_sha256']): fail('git_delivery_tree_changed')
        for n, row in enumerate(entries):
            if row['payload'] != 'file-' + str(n): fail('git_delivery_record_refused')
            raw = private.read(row['payload'], MAX_CHECKOUT_FILE_BYTES)
            if len(raw) != row['bytes'] or digest(raw) != row['sha256']: fail('git_delivery_payload_changed')
        if value['phase'] == 'sealed': self._commit_proof(root, value)

    def _tests(self, value, evidence):
        if (not callable(self.test_guard) or type(evidence) is not dict
                or set(evidence) != {'schema','snapshot_sha256','receipts'}
                or evidence['schema'] != 'controller-test-evidence-v1'
                or evidence['snapshot_sha256'] != value['snapshot_sha256']
                or type(evidence['receipts']) is not list or not 1 <= len(evidence['receipts']) <= 32
                or len(canonical(evidence)) > 32768): fail('git_delivery_test_evidence_required')
        names = set()
        for row in evidence['receipts']:
            if type(row) is not dict or set(row) != {'name','sha256'}: fail('git_delivery_test_evidence_required')
            opaque(row['name']); sha256(row['sha256'])
            if row['name'] in names: fail('git_delivery_test_evidence_required')
            names.add(row['name'])
        if self.test_guard(operation_id=value['request']['operation_id'], scope=copy(value['request']['scope']),
                snapshot=copy(value['snapshot']), evidence=copy(evidence)) is not True:
            fail('git_delivery_test_evidence_unconfirmed')

    def _write_object(self, private, root, data, kind):
        name = 'object-' + digest(kind.encode() + b'\0' + data)
        try: private.create(name, data)
        except DevelopmentError as error:
            if error.code != 'private_file_exists' or private.read(name, MAX_CHECKOUT_BYTES) != data: raise
        expected = object_id(data, kind, self.workspaces.grant['base_commit'])
        actual = self.workspaces._git(['hash-object','-w','--no-filters','-t',kind,'--',str(private.path/name)], cwd=root).strip().decode()
        if actual != expected: fail('git_delivery_object_changed')
        return actual

    def _tree(self, private, root, entries):
        tree = {}
        for row in entries:
            current = tree
            for part in row['path'].split('/')[:-1]: current = current.setdefault(part, {})
            raw = private.read(row['payload'], MAX_CHECKOUT_FILE_BYTES)
            oid = self._write_object(private, root, raw, 'blob')
            if oid != row['blob_oid']: fail('git_delivery_object_changed')
            current[row['path'].split('/')[-1]] = (row['mode'], oid)
        def write(node):
            raw = b''
            for name in sorted(node, key=lambda n: n.encode() + (b'/' if type(node[n]) is dict else b'')):
                item = node[name]; mode, oid = ('40000',write(item)) if type(item) is dict else item
                raw += mode.encode() + b' ' + name.encode() + b'\0' + bytes.fromhex(oid)
            return self._write_object(private, root, raw, 'tree')
        return write(tree)

    def _commit_proof(self, root, value):
        seal = value['seal']
        if (type(seal) is not dict or set(seal) != {'schema','delivery_sha','tree_sha','base_commit',
                'snapshot_sha256','ref','message','commit_epoch','test_evidence'}
                or seal['schema'] != 'local-delivery-commit-v1'
                or seal['base_commit'] != value['request']['base_commit']
                or seal['snapshot_sha256'] != value['snapshot_sha256']
                or seal['ref'] != 'refs/heads/codex/delivery-' + value['request']['operation_id']):
            fail('git_delivery_commit_changed')
        _commit(seal['delivery_sha']); _commit(seal['tree_sha'])
        raw_commit = _commit_bytes(seal['tree_sha'], seal['base_commit'], seal['message'], seal['commit_epoch'])
        w = self.workspaces
        if (w._git(['rev-parse','--verify',seal['ref']], cwd=root).strip().decode() != seal['delivery_sha']
                or w._git(['cat-file','commit',seal['delivery_sha']], cwd=root) != raw_commit
                or w._git(['rev-parse',seal['delivery_sha']+'^{tree}'], cwd=root).strip().decode() != seal['tree_sha']):
            fail('git_delivery_commit_changed')
        raw = w._git(['ls-tree','-r','-z','--full-tree',seal['delivery_sha']], cwd=root)
        expected = b''.join((row['mode']+' blob '+row['blob_oid']+'\t'+row['path']).encode()+b'\0'
                            for row in sorted(value['snapshot']['files'], key=lambda row: row['path'].encode()))
        if raw != expected: fail('git_delivery_commit_changed')

    def finalize(self, *, operation_id, snapshot_sha256, test_evidence, message):
        sha256(snapshot_sha256)
        if type(message) is not str or not message.strip() or '\0' in message or len(message.encode()) > 4096:
            fail('git_delivery_message_refused')
        with self.workspaces._locked(operation_id) as (workspace_private, _), \
             self._record(self.delivery_root, operation_id) as (private, _):
            source, root = self._workspace(workspace_private); value = self._read(private)
            self._validate(private, value, source, root)
            if value['snapshot_sha256'] != snapshot_sha256: fail('git_delivery_snapshot_changed')
            self._tests(value, test_evidence)
            self._validate(private, value, source, root)
            if value['phase'] == 'sealed':
                if value['seal']['message'] != message or value['seal']['test_evidence'] != test_evidence:
                    fail('git_delivery_request_conflict')
                return copy(value['seal'])
            value['phase'] = 'sealing'; private.replace('delivery.json', canonical(value))
            tree = self._tree(private, root, value['snapshot']['files'])
            epoch = int(time.time())
            raw = _commit_bytes(tree, value['request']['base_commit'], message, epoch)
            commit = self._write_object(private, root, raw, 'commit')
            ref = 'refs/heads/codex/delivery-' + operation_id
            self.workspaces._git(['update-ref',ref,commit,'0'*len(commit)], cwd=root)
            self.workspaces._fsync_tree(root/'.git')
            # Finalize never changes HEAD/index, so the original workspace API
            # remains usable and still enforces the pinned base branch.
            self.workspaces._ready(source)
            if self._snapshot(root, value['request']['changes'])[1] != value['snapshot']['files']:
                fail('git_delivery_tree_changed')
            value['seal'] = dict(schema='local-delivery-commit-v1', delivery_sha=commit, tree_sha=tree,
                base_commit=value['request']['base_commit'], snapshot_sha256=snapshot_sha256,
                ref=ref, message=message, commit_epoch=epoch, test_evidence=copy(test_evidence))
            value['metadata_sha256'] = self._metadata(root); self._commit_proof(root, value)
            self._roots()
            value['phase'] = 'sealed'; private.replace('delivery.json', canonical(value))
            return copy(value['seal'])

    def _sealed(self, operation_id, workspace_private, private):
        source, root = self._workspace(workspace_private); value = self._read(private)
        self._validate(private, value, source, root)
        if value['phase'] != 'sealed': fail('git_delivery_not_sealed')
        self._tests(value, value['seal']['test_evidence'])
        self._validate(private, value, source, root)
        return value

    def prepare_review(self, *, operation_id, review_operation_id, reviewer_scope, context_sha256, material):
        board_scope(reviewer_scope); sha256(context_sha256)
        if type(material) is not str or not material.strip() or '\0' in material or len(material.encode()) > 24000:
            fail('git_delivery_material_refused')
        with self.workspaces._locked(operation_id) as (workspace_private, _), \
             self._record(self.delivery_root, operation_id) as (private, _):
            value = self._sealed(operation_id, workspace_private, private)
            author = value['request']['scope']
            if (any(reviewer_scope[k] != author[k] for k in ('owner_id','project_id','ticket_id'))
                    or reviewer_scope['agent_id'] == author['agent_id']): fail('git_delivery_distinct_reviewer_required')
            selection = dict(schema='delivery-review-selection-v1', author_agent_id=author['agent_id'],
                delivery_sha=value['seal']['delivery_sha'], context_sha256=context_sha256,
                material_sha256=digest(material.encode()))
            request = dict(operation_id=operation_id, review_operation_id=_operation(review_operation_id),
                scope=copy(reviewer_scope), selection=selection, snapshot_sha256=value['snapshot_sha256'])
            with self._record(self.review_root, review_operation_id, create=True) as (review, fresh):
                if not fresh:
                    result = decode(review.read('review.json', 32768))
                    self._review_validate(review, result, request, value)
                    return copy(result)
                result = dict(schema='local-delivery-review-v1', phase='preparing', request=request,
                    workspace=str(review.path/'export'), identity=None, files_pin=None)
                review.create('review.json', canonical(result))
                export = review.path/'export'; os.mkdir(export, 0o700)
                for row in value['snapshot']['files']:
                    path = export/row['path']; path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                    # Every newly created parent is private; there is no reuse or
                    # overwrite path in an interrupted review export.
                    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
                    try:
                        PrivateDirectory._write(fd, private.read(row['payload'], MAX_CHECKOUT_FILE_BYTES))
                        os.fchmod(fd, 0o500 if row['mode']=='100755' else 0o400); os.fsync(fd)
                    finally: os.close(fd)
                for root, dirs, _files in os.walk(export, topdown=False):
                    fd = absolute_directory(root)
                    try: os.fchmod(fd, 0o500); os.fsync(fd)
                    finally: os.close(fd)
                os.fsync(review.fd)
                result.update(phase='ready', identity=list(identity(export.stat())),
                              files_pin=_inventory(export, readonly=True)[1])
                self._sealed(operation_id, workspace_private, private)
                self._review_validate(review, result, request, value)
                review.replace('review.json', canonical(result)); return copy(result)

    def _review_validate(self, review, result, request, value):
        self._roots()
        if (type(request) is not dict or set(request) != {'operation_id','review_operation_id','scope',
                'selection','snapshot_sha256'} or request['operation_id'] != value['request']['operation_id']
                or request['review_operation_id'] != review.path.name): fail('git_delivery_review_interrupted_or_changed')
        board_scope(request['scope'])
        selection = request['selection']; author = value['request']['scope']
        if (type(selection) is not dict or set(selection) != {'schema','author_agent_id','delivery_sha',
                'context_sha256','material_sha256'} or selection['schema'] != 'delivery-review-selection-v1'
                or selection['author_agent_id'] != author['agent_id']
                or selection['delivery_sha'] != value['seal']['delivery_sha']
                or request['scope']['agent_id'] == author['agent_id']
                or any(request['scope'][k] != author[k] for k in ('owner_id','project_id','ticket_id'))):
            fail('git_delivery_review_interrupted_or_changed')
        sha256(selection['context_sha256']); sha256(selection['material_sha256'])
        if (type(result) is not dict or set(result) != {'schema','phase','request','workspace','identity','files_pin'}
                or result['schema'] != 'local-delivery-review-v1' or result['phase'] != 'ready'
                or result['request'] != request or request['snapshot_sha256'] != value['snapshot_sha256']
                or result['workspace'] != str(review.path/'export')): fail('git_delivery_review_interrupted_or_changed')
        files, pins = _inventory(result['workspace'], readonly=True)
        if (list(identity(Path(result['workspace']).stat())) != result['identity'] or pins != result['files_pin']
                or set(files) != {r['path'] for r in value['snapshot']['files']}): fail('git_delivery_review_changed')
        for row in value['snapshot']['files']:
            mode, raw = files[row['path']]
            if mode != row['mode'] or digest(raw) != row['sha256']: fail('git_delivery_review_changed')

    def review_guard(self, operation_id, review_operation_id, *, scope, selection, context, material, workspace):
        """Bind with functools.partial for DeliveryReviewWorker.delivery_guard."""
        with self.workspaces._locked(operation_id) as (workspace_private, _), \
             self._record(self.delivery_root, operation_id) as (private, _), \
             self._record(self.review_root, review_operation_id) as (review, _):
            value = self._sealed(operation_id, workspace_private, private)
            result = decode(review.read('review.json', 32768)); request = result.get('request', {})
            self._review_validate(review, result, request, value)
            author = value['request']['scope']
            if (request.get('operation_id') != operation_id or request.get('review_operation_id') != review_operation_id
                    or scope != request.get('scope') or scope['agent_id'] == author['agent_id']
                    or any(scope[k] != author[k] for k in ('owner_id','project_id','ticket_id'))
                    or selection != request.get('selection') or workspace != result['workspace']
                    or context.get('fingerprint') != selection['context_sha256']
                    or digest(material.encode()) != selection['material_sha256']
                    or selection['delivery_sha'] != value['seal']['delivery_sha']
                    or selection['author_agent_id'] != author['agent_id']): fail('git_delivery_review_selection_changed')
            self._sealed(operation_id, workspace_private, private)
            return True
