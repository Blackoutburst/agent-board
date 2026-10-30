"""Read-only Linux broker proof -> exact GitDelivery evidence and Board link.

This never executes a test/model, charges broker budgets, publishes a PR or
changes a ticket. The concrete worker's existing publish CAS owns that effect.
"""
from copy import deepcopy
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path

from .dev_journal import DevelopmentJournal, PrivateDirectory, decode
from .dev_manifest import DevelopmentError, canonical, digest, relative_path, sha256, validate_manifest
from .dev_planner import _directory_pin, _reasoning_configuration
from .dev_planner_journal import PlannerJournal
from .dev_runtime import DevelopmentRuntimeGuard, development_work_id
from .development_worker import DevelopmentBoardWorker
from .family_audit import _finished
from .git_delivery import LocalGitDeliveryController, _changes, _inventory, object_id
from .git_workspace import LocalGitWorkspaceController, _operation
from .linux_test_backend import (MAX_JOB_BYTES, MAX_PLAN_BYTES, lookup_linux_native_cleanup,
                                 read_native_observation, validate_job_shape)
from .linux_test_protocol import linux_execution_backend_id
from .runtime_journal import RuntimeJournal


def fail(code='development_delivery_proof_changed'):
    raise DevelopmentError(code)


class DevelopmentDeliveryProof:
    """Concrete GitDelivery.test_guard; no recursive worker/workspace lock."""
    def __init__(self, worker, workspaces, operation_id):
        if type(worker) is not DevelopmentBoardWorker or type(workspaces) is not LocalGitWorkspaceController:
            fail('development_delivery_controller_required')
        self.worker, self.workspaces, self.operation_id = worker, workspaces, _operation(operation_id)
        op = worker.journal.get(operation_id); worker._pin_controller(op)
        self.configuration, self.scope = deepcopy(op['configuration']), deepcopy(op['scope'])

    @contextmanager
    def _read_work(self, work_id):
        # RunStore.work_lock can mark interrupted turns and create a missing
        # lock. This guard only acquires the already-existing exact lock.
        descriptor = None
        try:
            if work_id in self.worker.store._locks: fail('development_delivery_work_busy')
            with PrivateDirectory(self.worker.store.state_dir) as private:
                name = work_id+'.lock'
                descriptor = os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=private.fd)
                private.validate_file(descriptor)
                fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                before = os.fstat(descriptor)
                if os.stat(name,dir_fd=private.fd,follow_symlinks=False) != before: fail()
                yield
                private.verify(); private.validate_file(descriptor)
                if os.stat(name,dir_fd=private.fd,follow_symlinks=False) != before or os.fstat(descriptor) != before: fail()
        except OSError: fail('development_delivery_work_busy')
        finally:
            if descriptor is not None: os.close(descriptor)

    def _operation(self):
        op = self.worker.journal.get(self.operation_id); self.worker._pin_controller(op)
        if (op['configuration'] != self.configuration or op['scope'] != self.scope
                or op['stage'] not in ('result_ready','publishing','complete')): fail()
        self.worker._local_control(self.operation_id)
        pins = self.configuration['pins']
        if (any(_directory_pin(pin['path']) != pin for pin in pins.values())
                or pins['runs']['path'] != str(self.worker.store.state_dir)
                or pins['runtime']['path'] != str(self.worker.runtime.directory)
                or pins['journal']['path'] != str(self.worker.journal.directory)): fail()
        self.workspaces._roots()
        return op

    def _documents(self, op):
        config, pins = self.configuration, self.configuration['pins']
        documents = {}
        with PrivateDirectory(pins['broker']['path']) as private:
            for name,maximum in (('manifest.json',128*1024),('state.json',2*1024*1024),('planner.json',2*1024*1024)):
                documents[name] = private.read(name,maximum)
        manifest = validate_manifest(decode(documents['manifest.json']))
        broker, planner = decode(documents['state.json']), decode(documents['planner.json'])
        if (digest(canonical(manifest)) != config['manifest_hash'] or config.get('execution_profile') != 'linux-python-execution-v1'
                or type(broker) is not dict or broker.get('schema') != 3
                or broker.get('linux_execution_plan_sha256') != config.get('linux_execution_plan_sha256')
                or any(manifest['scope'][key] != self.scope[key] for key in ('owner_id','agent_id','project_id','ticket_id'))
                or manifest['scope']['session_id'] != self.scope['board_session_id']): fail('development_delivery_linux_proof_required')
        validator = object.__new__(DevelopmentJournal)
        validator._manifest, validator.manifest_hash = manifest, config['manifest_hash']
        validator.validate_state(broker)
        if any(row['phase'] != 'complete' for row in broker['operations'].values()): fail()
        with PrivateDirectory(pins['backend']['path']) as private:
            backend_raw = private.read('execution-plan.json',MAX_PLAN_BYTES)
            backend = decode(backend_raw)
            if decode(private.read('backend-manifest.json',128*1024)) != manifest: fail()
        if (canonical(backend) != backend_raw or digest(backend_raw) != config['linux_execution_plan_sha256']
                or backend.get('schema') != 'blackoutburst-linux-execution-plan-v1'
                or backend.get('root') != manifest['root'] or backend.get('root_identity') != manifest['root_identity']): fail()
        backend_pin = dict(backend_id=linux_execution_backend_id(config['linux_execution_plan_sha256']),
            plan_hash=config['linux_execution_plan_sha256'],kind='linux-python-execution-v1',
            supervision={'kind':'linux-python-execution-v1','sources':backend['sources'],
                         'environment':backend['environment'],'bootstrap_sha256':backend['bootstrap_sha256']})
        if digest(canonical(backend_pin)) != config['backend_hash']: fail()
        with PrivateDirectory(pins['planner_policy']['path']) as private:
            policy_raw = private.read('planner-plan.json',64*1024); policy = decode(policy_raw)
        if (digest(policy_raw) != config['policy_hash'] or canonical(policy) != policy_raw
                or policy.get('schema') != 'blackoutburst-linux-planner-policy-v1'
                or policy.get('checkout') != manifest['root'] or policy.get('checkout_identity') != manifest['root_identity']
                or policy.get('environment') != backend['environment']
                or policy.get('workspace') != pins['planner_workspace']['path']
                or policy.get('workspace_identity') != pins['planner_workspace']['identity']): fail()
        parent = op['payload']['parent_binding']; work_id = development_work_id(self.operation_id)
        parent_scope = dict(operation_id=self.operation_id,journal=pins['journal'],
            configuration_hash=digest(canonical({'configuration':config,'initial_context_hash':op['payload']['initial_context']['fingerprint']})))
        if parent != dict(scope=self.scope,root_identity=manifest['root_identity'],journal=pins['broker'],
                configuration_hash=parent_scope['configuration_hash'],parent_scope=parent_scope,
                child_scope=dict(work_id=work_id,runs=pins['runs'],runtime=pins['runtime'])): fail()
        expected = dict(schema=1,manifest_hash=config['manifest_hash'],scope=self.scope,claim_id=op['payload']['claim_id'],
            work_id=work_id,policy_hash=config['policy_hash'],policy_cli_hash=policy['cli_sha256'],
            pins={key:pins[key] for key in ('broker','runs','runtime')},model=config['model'],instruction=config['instruction'],
            budgets=op['payload']['planner_budgets'],context=op['payload']['context'],parent_operation=parent_scope)
        expected['pins']['planner'] = pins['planner_workspace']
        if 'reasoning_effort' in config:
            reasoning = _reasoning_configuration(config['reasoning_effort'])
            if not reasoning: fail()  # None is represented by absence, never a stored optional value.
            expected.update(reasoning)
        validator = object.__new__(PlannerJournal)
        validator.operation_id, validator.configuration, validator.fingerprint = self.operation_id, expected, digest(canonical(expected))
        validator.validate(planner)
        receipt = op['payload']['planner_receipt']
        if (planner['phase'] != 'review_ready' or receipt != dict(operation_id=self.operation_id,status='review_ready',reason=planner['reason'],
                used=planner['used'],review=planner['review'],stop_requested=False)
                or type(planner['step']) is not dict or planner['step']['action'].get('kind') != 'final'
                or planner['step']['tool_finished'] is not True or planner['step']['receipt'] != planner['review']
                or planner['review'].get('evidence_kind') != 'inspected_snapshot'): fail()
        documents['backend'] = backend_raw; documents['policy'] = policy_raw
        return manifest, broker, planner, policy, documents

    def _snapshot(self, snapshot, manifest, *, _aggregate=None):
        private_source = _aggregate is not None
        schema = 'local-captured-tree-v1'
        if private_source:
            from .development_aggregate_delivery import AggregateDevelopmentDeliveryController, PRIVATE_SOURCE_SCHEMA
            if (type(_aggregate) is not AggregateDevelopmentDeliveryController
                    or _aggregate.final_proof is not self): fail()
            _aggregate._logical()
            if _aggregate._source_capture() != snapshot: fail()
            schema = PRIVATE_SOURCE_SCHEMA
        if (type(snapshot) is not dict or set(snapshot) != {'schema','request','files'}
                or snapshot['schema'] != schema or type(snapshot['files']) is not list): fail()
        request = snapshot['request']
        with PrivateDirectory(self.workspaces.state_root/self.operation_id) as private:
            workspace = self.workspaces._read(private)
        if (workspace['phase'] != 'ready' or type(request) is not dict
                or set(request) != {'operation_id','scope','changes','workspace_request_sha256','checkout','base_commit'}
                or request['operation_id'] != self.operation_id or request['scope'] != manifest['scope']
                or workspace['request']['scope'] != manifest['scope'] or request['checkout'] != manifest['root']
                or workspace['request']['checkout'] != manifest['root']
                or request['workspace_request_sha256'] != workspace['request_sha256']
                or request['base_commit'] != manifest['base_sha'] or request['base_commit'] != self.workspaces.grant['base_commit']): fail()
        self.workspaces._ready(workspace)
        if private_source:
            if request['changes'] != []: fail()
            changes = []
        else:
            changes = _changes(request['changes'])
        if any(row['path'] not in manifest['files'] or row['sha256'] is None for row in changes): fail()
        root = Path(manifest['root']); base = {}
        raw = self.workspaces._git(['ls-tree','-r','-z','--full-tree',manifest['base_sha']],cwd=root)
        for entry in raw.rstrip(b'\0').split(b'\0'):
            head,path = entry.split(b'\t',1); mode,kind,oid = head.split(); path = path.decode('utf-8','strict')
            relative_path(path)
            if kind != b'blob' or mode not in (b'100644',b'100755') or path in base: fail()
            base[path] = (mode.decode(),oid.decode())
        captured = {}
        for n,row in enumerate(snapshot['files']):
            if (type(row) is not dict or set(row) != {'path','mode','bytes','sha256','blob_oid','payload'}
                    or row['path'] in captured or row['path'] not in base
                    or row['mode'] != base[row['path']][0] or type(row['bytes']) is not int or row['bytes'] < 0
                    or row['payload'] != 'file-'+str(n)): fail()
            sha256(row['sha256'])
            if (private_source or row['path'] not in manifest['files']) and row['blob_oid'] != base[row['path']][1]: fail()
            captured[row['path']] = row
        if set(captured) != set(base) or not set(manifest['files']) <= set(captured): fail()
        actual,_ = _inventory(root)
        if set(actual) != set(captured): fail()
        for path,(mode,data) in actual.items():
            row = captured[path]
            if (row['mode'] != mode or row['bytes'] != len(data) or row['sha256'] != digest(data)
                    or row['blob_oid'] != object_id(data,'blob',manifest['base_sha'])): fail()
        current = {}
        for path,before in manifest['files'].items():
            original = self.workspaces._git(['cat-file','blob',base[path][1]],cwd=root)
            if digest(original) != before['sha256']: fail()
            current[path] = captured[path]['sha256']
        # This is the planner's ALL-manifest-path map, not a renamed Git hash.
        return current, digest(canonical(current))

    def _runtime(self, op, manifest, planner, policy):
        work_id = development_work_id(self.operation_id); required = set(planner['review']['tests'])
        with self._read_work(work_id):
            stored = self.worker.store.status(work_id); work = stored['work']
            if (work_id != work['id'] or work.get('owner_id') != self.scope['owner_id']
                    or work.get('project_id') != self.scope['project_id'] or work.get('ticket_id') != self.scope['ticket_id']
                    or work.get('workspace') != policy['workspace'] or work.get('executable') != policy['executable']
                    or work.get('model') != self.configuration['model'] or not stored['turns']): fail()
            guard = DevelopmentRuntimeGuard(self.worker.runtime,self.worker.control,self.scope,operation_id=self.operation_id)
            expected_runtime = set(); hashes = []
            for turn in stored['turns']:
                result = self.worker.store.turn_result(turn['id']); verification = result.get('verification')
                if (turn['status'] != 'completed' or turn.get('requires_review') or type(verification) is not dict
                        or any(verification.get(key) is not True for key in ('tool_free','cleanup_confirmed','session_verified','turn_completed','exit_zero'))
                        or verification.get('authentication') != 'chatgpt'): fail()
                record = self.worker.runtime.find(guard.configuration(work,turn['id']))
                _finished(record,manifest,self.worker.control); expected_runtime.add(record['id'])
                hashes.append(digest(canonical(record['receipt'])))
            history, final = planner['history'], planner['step']
            if any(type(step) is not dict or step.get('kind') not in ('read','patch','test') for step in history): fail()
            steps = [dict(step_id=s['step_id'],kind=s['kind'],model_turn_id=s.get('model_turn_id')) for s in history]
            steps.append(dict(step_id=final['id'],kind='final',model_turn_id=final['model_turn_id']))
            turns = {row['id']:row for row in stored['turns']}
            if (len(steps) != len(turns) or len({s['model_turn_id'] for s in steps}) != len(steps)
                    or {s['model_turn_id'] for s in steps} != set(turns)
                    or any(turns[s['model_turn_id']]['request_id'] != 'dev-model-'+s['step_id'] for s in steps)): fail()
            for step in steps:
                labels = sorted(required) if step['kind']=='test' else ['patch_file'] if step['kind']=='patch' else ['read_file']
                records = [self.worker.runtime.find(RuntimeJournal.configuration(self.scope,work_id,step['step_id'],label,provider='local_broker')) for label in labels]
                records = [row for row in records if row is not None]
                if len(records) != 1: fail()
                _finished(records[0],manifest,self.worker.control); expected_runtime.add(records[0]['id'])
                hashes.append(digest(canonical(records[0]['receipt'])))
            rows = self.worker.runtime.db.execute("SELECT id FROM executions WHERE json_extract(configuration,'$.work_id')=? LIMIT 130",(work_id,)).fetchall()
            if len(rows)>128 or {row['id'] for row in rows} != expected_runtime: fail()
        return digest(canonical(sorted(hashes)))

    def evidence(self, snapshot):
        return self._evidence(snapshot)

    def _aggregate_source_evidence(self, snapshot, *, controller):
        """Private whole-tree observation; never a public Delivery snapshot.

        The concrete live aggregate and its recorded source bytes are required.
        There is no serialized flag or ordinary GitDelivery empty-change mode.
        """
        return self._evidence(snapshot, _aggregate=controller)

    def _evidence(self, snapshot, *, _aggregate=None):
        try:
            op = self._operation(); manifest,broker,planner,policy,documents = self._documents(op)
            current,planner_hash = self._snapshot(snapshot,manifest,_aggregate=_aggregate)
            review = planner['review']; required = {suite['name'] for suite in manifest['tests']}
            if (not required or type(review['tests']) is not dict or set(review['tests']) != required
                    or review['snapshot_sha256'] != planner_hash or planner['tests'] != review['tests']
                    or review['diff']['base_sha'] != manifest['base_sha']): fail()
            receipts = []; native_hashes = {}
            for name,proof in sorted(review['tests'].items()):
                operation = broker['operations'][digest(proof['key'].encode('ascii'))]; receipt = operation['receipt']
                if (proof['snapshot_sha256'] != planner_hash or operation['kind'] != 'test'
                        or receipt['result'] != 'passed' or receipt['name'] != name or receipt.get('cleanup_confirmed') is not True
                        or receipt['operation_id'] != proof['operation_id'] or digest(canonical(receipt)) != proof['receipt_sha256']): fail()
                record = broker['linux_execution'][operation['id']]
                if (record['phase'] != 'broker_admitted' or record['reservation']['workflow_id'] != self.operation_id
                        or record['admitted_snapshot_sha256'] != planner_hash
                        or record['reservation']['planner_snapshot_sha256'] != planner_hash
                        or record['native_receipt_sha256'] is None): fail()
                observation = lookup_linux_native_cleanup(self.configuration['pins']['backend']['path'],manifest=manifest,
                    record=record,operation_id=operation['id'],workflow_id=self.operation_id,
                    expected_plan_sha256=self.configuration['linux_execution_plan_sha256'])
                if observation['native_receipt_sha256'] != record['native_receipt_sha256']: fail()
                with PrivateDirectory(Path(self.configuration['pins']['backend']['path'])/('execution-'+operation['id'])) as private:
                    job = validate_job_shape(decode(private.read('job.json',MAX_JOB_BYTES)),private.path)
                    native = read_native_observation(private,job,decode(documents['backend']))
                if (native is None or digest(canonical(native)) != record['native_receipt_sha256']
                        or native['reason'] != 'completed' or native['exit_code'] != 0 or native['source_released'] is not True
                        or native['output_bytes'] != receipt['output_bytes'] or native['output_sha256'] != receipt['output_sha256']): fail()
                receipts.append(dict(name=name,sha256=proof['receipt_sha256'])); native_hashes[name]=record['native_receipt_sha256']
            runtime_hash = self._runtime(op,manifest,planner,policy)
            fresh = self._operation()
            if fresh != op or self._documents(fresh)[4] != documents: fail()
            git_hash = digest(canonical(snapshot))
            binding = dict(schema='development-tested-git-binding-v1',operation_id=self.operation_id,
                configuration_sha256=digest(canonical(self.configuration)),manifest_sha256=self.configuration['manifest_hash'],
                planner_document_sha256=digest(documents['planner.json']),broker_document_sha256=digest(documents['state.json']),
                planner_snapshot_sha256=planner_hash,git_snapshot_sha256=git_hash,runtime_cleanup_sha256=runtime_hash)
            return dict(schema='controller-test-evidence-v1',snapshot_sha256=git_hash,receipts=[dict(name=row['name'],
                sha256=digest(canonical({**binding,'test_receipt_sha256':row['sha256'],'native_receipt_sha256':native_hashes[row['name']]})))
                for row in receipts])
        except DevelopmentError:
            raise
        except Exception:
            fail()

    def __call__(self, *, operation_id, scope, snapshot, evidence):
        if (type(snapshot) is not dict or type(snapshot.get('request')) is not dict
                or operation_id != self.operation_id or scope != snapshot['request'].get('scope')): fail()
        if evidence != self.evidence(snapshot): fail()
        return True


class DevelopmentPublishedDelivery:
    """Closed read-only verification of an already complete concrete publisher."""
    def __init__(self, proof, deliveries, publisher):
        from .git_publication import GitHubDraftPublisher
        from .development_aggregate_delivery import AggregateDevelopmentDeliveryProof
        if (type(proof) not in (DevelopmentDeliveryProof,AggregateDevelopmentDeliveryProof)
                or type(deliveries) is not LocalGitDeliveryController
                or type(publisher) is not GitHubDraftPublisher or deliveries.workspaces is not proof.workspaces
                or deliveries.test_guard is not proof or publisher.deliveries is not deliveries):
            fail('development_delivery_publication_required')
        if type(proof) is AggregateDevelopmentDeliveryProof:
            from .git_publication import _development_authority_pair
            if not _development_authority_pair(publisher.authority,proof):
                fail('development_delivery_publication_required')
        self.proof,self.deliveries,self.publisher = proof,deliveries,publisher

    def verify(self, worker, operation_id, scope):
        if worker is not self.proof.worker or operation_id != self.proof.operation_id or scope != self.proof.scope: fail()
        publisher = self.publisher
        with self.proof.workspaces._locked(operation_id) as (workspace,_), \
             self.deliveries._record(self.deliveries.delivery_root,operation_id) as (delivery,_), \
             self.deliveries._record(publisher.root,operation_id) as (publication,_):
            value = publisher._read(publication)
            if value['phase'] != 'complete': fail('development_delivery_publication_incomplete')
            publisher._before_effect(operation_id,workspace,delivery,publication,value,head_required=True)
            found = publisher._lookup_pr(publication,value)
            if found != value['pr']: fail()
            # No push/create/publish method is called by this reader.
            publisher._before_effect(operation_id,workspace,delivery,publication,value,head_required=True)
            if publisher._lookup_pr(publication,value) != found or publisher._read(publication) != value: fail()
            request = value['request']
            return dict(schema='development-published-delivery-v1',operation_id=operation_id,
                delivery_sha=request['delivery_sha'],tree_sha=request['tree_sha'],snapshot_sha256=request['snapshot_sha256'],
                publication_request_sha256=value['request_sha256'],pull_request=deepcopy(found))
