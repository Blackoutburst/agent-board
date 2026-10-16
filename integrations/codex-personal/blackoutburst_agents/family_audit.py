"""Private controller audit of exactly sealed files; no Runtime/tool/model effect.

This is a dated inspection on an exclusive checkout, not an atomic repository or
permanent freshness proof. No source content leaves this module except bounded
candidate replacements, which stay in the private parent instruction.
"""
from contextlib import closing

from .dev_journal import DevelopmentJournal,PrivateDirectory,decode
from .dev_manifest import (DevelopmentError,RepositoryFiles,canonical,check_content,digest,
                           validate_manifest)
from .dev_planner import _directory_pin
from .dev_runtime import DevelopmentRuntimeGuard,development_work_id
from .runtime_journal import RuntimeJournal
from .runtime_guard import RuntimeGuard

MAX_CANDIDATE_BYTES=8192
MAX_AUDIT_BYTES=8*1024*1024


def manifest_document(directory):
    with PrivateDirectory(directory) as private:
        manifest=validate_manifest(decode(private.read('manifest.json',128*1024)))
    return manifest,digest(canonical(manifest))


def inspect_files(manifest, *, authorize, include_changes=False, maximum=MAX_AUDIT_BYTES):
    if authorize() is not True:raise DevelopmentError('family_authorization_changed')
    current,candidates,used={},[],0
    with closing(RepositoryFiles(manifest['root'],manifest['root_identity'])) as files:
        for path,before in sorted(manifest['files'].items()):
            if authorize() is not True:raise DevelopmentError('family_authorization_changed')
            data,_=files.read(path,min(manifest['budgets']['max_file_bytes'],maximum-used))
            used+=len(data)
            if used>maximum:raise DevelopmentError('family_audit_budget_exhausted')
            check_content(data);after=digest(data);current[path]=after
            if include_changes and after!=before['sha256']:
                try:content=data.decode('utf-8','strict')
                except UnicodeError:raise DevelopmentError('family_candidate_not_utf8') from None
                candidates.append({'path':path,'before_sha256':before['sha256'],
                                   'after_sha256':after,'content':content})
                if len(canonical(candidates))>MAX_CANDIDATE_BYTES:
                    raise DevelopmentError('family_candidate_too_large')
        files.verify()
    if authorize() is not True:raise DevelopmentError('family_authorization_changed')
    return {'files':current,'snapshot_sha256':digest(canonical(current)),
            'candidates':candidates,'read_bytes':used}


def _finished(record,manifest,client):
    if (type(record) is not dict or record.get('phase')!='finished'
            or type(record.get('start_confirmed')) not in (int,bool) or record['start_confirmed']!=1
            or type(record.get('receipt')) is not dict
            or record['receipt'].get('state')!='finished' or record['receipt'].get('cleanup_confirmed') is not True
            or record['receipt'].get('outcome')!='completed' or record['receipt'].get('reason')!='completed'
            or record['receipt'].get('device_id')!=manifest['scope']['device_id']
            or record['receipt'].get('profile_version')!=manifest['scope']['profile_version']):
        raise DevelopmentError('family_cleanup_unconfirmed')
    try:
        validator=RuntimeGuard(None,client,record['configuration']['scope'])
        validator.operation={**record,'receipt':None}
        validator.metadata(record['receipt'])
    except Exception:
        raise DevelopmentError('family_cleanup_unconfirmed') from None


def audit_workflow(worker,operation_id,*,manifest_hash,authorize,expected_hashes=None):
    """No new broker effect; cached tests plus current scoped file hashes only."""
    if authorize() is not True:raise DevelopmentError('family_authorization_changed')
    op=worker.journal.get(operation_id);worker._pin_controller(op)
    if op['stage'] not in ('complete','result_ready'):
        raise DevelopmentError('family_workflow_not_review_ready')
    config=op['configuration'];pins=config['pins']
    for pin in pins.values():
        if _directory_pin(pin['path'])!=pin:raise DevelopmentError('family_directory_changed')
    manifest,actual_hash=manifest_document(pins['broker']['path'])
    if actual_hash!=manifest_hash or config['manifest_hash']!=manifest_hash:
        raise DevelopmentError('family_manifest_changed')
    if (any(manifest['scope'][key]!=op['scope'][key] for key in ('owner_id','agent_id','project_id','ticket_id'))
            or manifest['scope']['session_id']!=op['scope']['board_session_id']):
        raise DevelopmentError('family_scope_mismatch')
    with PrivateDirectory(pins['broker']['path']) as private:
        planner=decode(private.read('planner.json',2*1024*1024))
        broker=decode(private.read('state.json',2*1024*1024))
    receipt=op['payload'].get('planner_receipt')
    if (type(planner) is not dict or planner.get('operation_id')!=operation_id
            or planner.get('phase')!='review_ready' or type(receipt) is not dict
            or receipt.get('status')!='review_ready' or receipt.get('operation_id')!=operation_id
            or planner.get('review')!=receipt.get('review') or type(planner.get('configuration')) is not dict
            or planner['configuration'].get('scope')!=op['scope']
            or planner['configuration'].get('manifest_hash')!=manifest_hash
            or type(broker) is not dict or broker.get('manifest_hash')!=manifest_hash
            or type(broker.get('operations')) is not dict):
        raise DevelopmentError('family_audit_receipt_changed')
    # Only the existing pure document validator is used: do not construct a
    # DevelopmentJournal, whose constructor may create a missing lock file.
    validator=object.__new__(DevelopmentJournal)
    validator._manifest=manifest;validator.manifest_hash=manifest_hash
    validator.validate_state(broker)
    if any(type(entry) is not dict or entry.get('phase')!='complete' for entry in broker['operations'].values()):
        raise DevelopmentError('family_cleanup_unconfirmed')
    review=receipt['review']
    if type(review) is not dict or review.get('evidence_kind')!='inspected_snapshot':
        raise DevelopmentError('family_audit_receipt_changed')
    snapshot=inspect_files(manifest,authorize=authorize,include_changes=True)
    if (snapshot['snapshot_sha256']!=review.get('snapshot_sha256')
            or expected_hashes is not None and snapshot['files']!=expected_hashes):
        raise DevelopmentError('family_source_snapshot_changed')
    required={suite['name'] for suite in manifest['tests']};proofs=review.get('tests')
    if not required or type(proofs) is not dict or set(proofs)!=required:
        raise DevelopmentError('family_test_proof_missing')
    for name,proof in proofs.items():
        try:
            operation=broker['operations'][digest(proof['key'].encode('ascii'))]
            test=operation['receipt']
            if (proof['snapshot_sha256']!=snapshot['snapshot_sha256'] or operation['kind']!='test'
                    or operation['phase']!='complete' or type(test) is not dict
                    or test.get('result')!='passed' or test.get('name')!=name
                    or test.get('cleanup_confirmed') is not True
                    or test.get('operation_id')!=proof['operation_id']
                    or digest(canonical(test))!=proof['receipt_sha256']):
                raise ValueError
        except (KeyError,TypeError,ValueError,UnicodeError):
            raise DevelopmentError('family_test_proof_changed') from None
    work_id=development_work_id(operation_id)
    if planner['configuration'].get('work_id')!=work_id:
        raise DevelopmentError('family_work_identity_changed')
    execution_hashes=[];expected_runtime=set()
    with worker.store.work_lock(work_id):
        stored=worker.store.status(work_id);work=stored['work']
        if work_id!=op['payload']['parent_binding']['child_scope']['work_id']:
            raise DevelopmentError('family_work_identity_changed')
        if not stored['turns']:raise DevelopmentError('family_cleanup_unconfirmed')
        guard=DevelopmentRuntimeGuard(worker.runtime,worker.control,op['scope'],operation_id=operation_id)
        for turn in stored['turns']:
            result=worker.store.turn_result(turn['id'])
            if (turn['status']!='completed' or turn.get('requires_review')
                    or type(result.get('verification')) is not dict
                    or any(result['verification'].get(key) is not True for key in
                        ('tool_free','cleanup_confirmed','session_verified','turn_completed','exit_zero'))
                    or result['verification'].get('authentication')!='chatgpt'):
                raise DevelopmentError('family_cleanup_unconfirmed')
            record=worker.runtime.find(guard.configuration(work,turn['id']))
            _finished(record,manifest,worker.control);expected_runtime.add(record['id'])
            execution_hashes.append(digest(canonical(record['receipt'])))
        history=planner.get('history');final=planner.get('step')
        if (type(history) is not list or any(type(step) is not dict or step.get('kind') not in
                ('read','patch','test') for step in history) or type(final) is not dict
                or type(final.get('action')) is not dict or final['action'].get('kind')!='final'
                or final.get('tool_finished') is not True):
            raise DevelopmentError('family_audit_receipt_changed')
        steps=[{'step_id':s['step_id'],'kind':s['kind'],'model_turn_id':s.get('model_turn_id')} for s in history]
        steps.append({'step_id':final['id'],'kind':'final','model_turn_id':final.get('model_turn_id')})
        turns={turn['id']:turn for turn in stored['turns']}
        if (len(steps)!=len(turns) or len({step['model_turn_id'] for step in steps})!=len(steps)
                or {step['model_turn_id'] for step in steps}!=set(turns)
                or any(turns[step['model_turn_id']]['request_id']!='dev-model-'+step['step_id'] for step in steps)
                or final.get('request_id')!='dev-model-'+final['id']):
            raise DevelopmentError('family_audit_receipt_changed')
        for step in steps:
            labels=(sorted(required) if step['kind']=='test' else
                    ['patch_file'] if step['kind']=='patch' else ['read_file'])
            records=[worker.runtime.find(RuntimeJournal.configuration(op['scope'],work_id,step['step_id'],
                     label,provider='local_broker')) for label in labels]
            records=[record for record in records if record is not None]
            if len(records)!=1:raise DevelopmentError('family_cleanup_unconfirmed')
            _finished(records[0],manifest,worker.control);expected_runtime.add(records[0]['id'])
            execution_hashes.append(digest(canonical(records[0]['receipt'])))
        rows=worker.runtime.db.execute("SELECT id FROM executions WHERE json_extract(configuration,'$.work_id')=? LIMIT 130",(work_id,)).fetchall()
        if len(rows)>128 or {row['id'] for row in rows}!=expected_runtime:
            raise DevelopmentError('family_cleanup_unconfirmed')
    if authorize() is not True:raise DevelopmentError('family_authorization_changed')
    audit={'operation_id':operation_id,'manifest_sha256':manifest_hash,'snapshot_sha256':snapshot['snapshot_sha256'],
           'base_sha':manifest['base_sha'],'tests':sorted(required),
           'cleanup_receipts_sha256':digest(canonical(sorted(execution_hashes)))}
    return {'audit':audit,'audit_sha256':digest(canonical(audit)),'files':snapshot['files'],
            'candidates':snapshot['candidates'],'read_bytes':snapshot['read_bytes']}
