"""Identity-only Runtime finish from already durable controller receipts.

No registration, grants, source read, broker replay, backend construction or
admission. Missing/incomplete receipts retain needs_review. This path does not
advance the planner or turn a recovered operation into an acceptance.
"""
from .dev_journal import PrivateDirectory,decode,DevelopmentJournal
from .dev_manifest import DevelopmentError,canonical,digest,validate_manifest
from .dev_planner import _directory_pin,parse_action
from .dev_planner_journal import PlannerJournal
from .dev_runtime import DevelopmentRuntimeGuard,ToolRuntimeGuard,development_work_id


def _execution_profile(broker,config):
    if broker.get('schema') == 3:
        if (config.get('execution_profile') != 'linux-python-execution-v1'
                or config.get('linux_execution_plan_sha256') != broker.get('linux_execution_plan_sha256')):
            raise DevelopmentError('development_cleanup_identity_changed')
        return True
    if broker.get('schema') != 1 or 'execution_profile' in config:
        raise DevelopmentError('development_input_preparation_is_not_cleanup_proof')
    return False


def _effect_receipt(step,manifest,broker,*,configuration=None,workflow_id=None):
    linux=_execution_profile(broker,configuration or {})
    action=parse_action(canonical(step['action']).decode('utf-8'),manifest,65536)
    kind=action['kind'];receipt=step['receipt']
    if kind in ('patch','test'):
        entry=broker['operations'].get(digest(step['action_key'].encode('ascii')))
        if entry is None or entry['kind']!=kind:
            raise DevelopmentError('development_cleanup_receipt_missing')
        if kind=='patch':
            request={'kind':'patch','path':action['path'],'before_sha256':action['expected_sha256'],
                     'after_sha256':digest(action['content'].encode('utf-8')),'bytes':len(action['content'].encode('utf-8'))}
        else:
            suites=[suite for suite in manifest['tests'] if suite['name']==action['name']]
            if len(suites)!=1:raise DevelopmentError('development_cleanup_receipt_missing')
            request={'kind':'test',**suites[0]}
        if entry['request_hash']!=digest(canonical(request)):
            raise DevelopmentError('development_cleanup_identity_changed')
        if linux and kind=='test':
            record=broker['linux_execution'].get(entry['id'])
            if record is None:
                # Only this process's pre-dispatch receipt proves non-launch.
                if entry['phase']!='complete' or entry['receipt'].get('result')!='not_started':
                    raise DevelopmentError('development_cleanup_receipt_missing')
            else:
                if (record['reservation']['workflow_id']!=workflow_id
                        or record['reservation']['action_key_sha256']!=digest(step['action_key'].encode('ascii'))
                        or record['reservation']['action_sha256']!=digest(canonical(action))):
                    raise DevelopmentError('development_cleanup_identity_changed')
                if record['phase']!='broker_admitted':
                    from .linux_test_backend import lookup_linux_native_cleanup
                    lookup_linux_native_cleanup(configuration['pins']['backend']['path'],manifest=manifest,
                        record=record,operation_id=entry['id'],workflow_id=workflow_id,
                        expected_plan_sha256=configuration['linux_execution_plan_sha256'])
                    return 'blocked'  # Cleanup is not a recovered source/test admission.
        if entry['phase']!='complete':
            raise DevelopmentError('development_cleanup_receipt_missing')
        raw=entry['receipt']
        if kind=='test' and raw.get('cleanup_confirmed') is not True:
            raise DevelopmentError('development_cleanup_receipt_missing')
        if kind=='test' and raw.get('result')=='not_started':
            dispatch=entry.get('admission',{}).get('dispatch')
            if (dispatch not in ('pending','dispatched')
                    or dispatch=='dispatched' and type(raw.get('backend_admission')) is not dict
                    or raw.get('started') is not False
                    or raw.get('admission_sha256')!=digest(canonical(entry['admission']))):
                raise DevelopmentError('development_cleanup_identity_changed')
            if dispatch=='dispatched':
                from .dev_broker import TestRequest,validate_backend_not_started
                selected=suites[0]
                native_request=TestRequest(entry['id'],digest(canonical(manifest)),manifest['root'],
                    tuple(manifest['root_identity']),selected['name'],selected['timeout_seconds'],selected['max_output_bytes'])
                validate_backend_not_started(native_request,selected['backend_id'],raw['backend_admission'])
            if receipt is not None and receipt.get('receipt')!=raw:
                raise DevelopmentError('development_cleanup_identity_changed')
            return 'blocked'
        if receipt is None:return 'blocked'  # No post-effect inspection receipt.
        if (receipt if kind=='patch' else receipt.get('receipt'))!=raw:
            raise DevelopmentError('development_cleanup_identity_changed')
        return 'completed'
    if kind=='final' and receipt is None:
        # Final only inspects local diff/test receipts; it never dispatches a
        # model, test or patch. A refusal before its inspection receipt can
        # therefore finish only as blocked. Do not confuse a named test whose
        # Runtime label is also "read_file" with this non-dispatching action.
        if digest(step['action_key'].encode('ascii')) in broker['operations']:
            raise DevelopmentError('development_cleanup_identity_changed')
        return 'blocked'
    if receipt is None:raise DevelopmentError('development_cleanup_receipt_missing')
    if kind=='read':
        if (set(receipt)!={'path','bytes','sha256','content'} or receipt['path']!=action['path']
                or type(receipt['content']) is not str or receipt['bytes']!=len(receipt['content'].encode('utf-8'))
                or receipt['sha256']!=digest(receipt['content'].encode('utf-8'))):
            raise DevelopmentError('development_cleanup_identity_changed')
    elif (kind!='final' or receipt.get('evidence_kind')!='inspected_snapshot'
            or type(receipt.get('tests')) is not dict or type(receipt.get('diff')) is not dict):
        raise DevelopmentError('development_cleanup_identity_changed')
    return 'completed'


def reconcile_cleanup(worker,operation_id):
    op=worker.journal.get(operation_id)
    with worker.journal.lock(op['scope']):
        op=worker.journal.get(operation_id);worker._pin_controller(op)
        config=op['configuration'];pins=config['pins'];scope=op['scope']
        for pin in pins.values():
            if _directory_pin(pin['path'])!=pin:raise DevelopmentError('development_directory_changed')
        with PrivateDirectory(pins['broker']['path']) as private:
            planner=decode(private.read('planner.json',2*1024*1024))
            manifest=validate_manifest(decode(private.read('manifest.json',128*1024)))
            broker=decode(private.read('state.json',2*1024*1024))
        if type(broker) is not dict:
            raise DevelopmentError('development_input_preparation_is_not_cleanup_proof')
        _execution_profile(broker,config)
        if (digest(canonical(manifest))!=config['manifest_hash'] or planner.get('operation_id')!=operation_id
                or planner.get('configuration',{}).get('scope')!=scope
                or planner['configuration'].get('manifest_hash')!=config['manifest_hash']
                or planner['configuration'].get('work_id')!=development_work_id(operation_id)):
            raise DevelopmentError('development_cleanup_identity_changed')
        validator=object.__new__(PlannerJournal);validator.operation_id=operation_id
        validator.configuration=planner['configuration'];validator.fingerprint=digest(canonical(validator.configuration))
        validator.validate(planner)
        validator=object.__new__(DevelopmentJournal);validator._manifest=manifest;validator.manifest_hash=config['manifest_hash']
        validator.validate_state(broker)
        finished=0;work_id=development_work_id(operation_id)
        with worker.store.work_lock(work_id):
            stored=worker.store.status(work_id);work=stored['work']
            model=DevelopmentRuntimeGuard(worker.runtime,worker.control,scope,operation_id=operation_id)
            expected={}
            for turn in stored['turns']:
                configuration=model.configuration(work,turn['id']);expected[digest(canonical(configuration))]=configuration
            history={entry['step_id']:entry for entry in planner['history']}
            step=planner['step'];current_id=None if step is None or step['action'] is None else step['id']
            rows=worker.runtime.db.execute("SELECT id FROM executions WHERE json_extract(configuration,'$.work_id')=? LIMIT 130",(work_id,)).fetchall()
            if len(rows)>128:raise DevelopmentError('development_cleanup_identity_changed')
            tools=[];history_seen=set()
            for row in rows:
                record=worker.runtime.get(row['id']);configuration=record['configuration']
                if configuration['provider']=='codex_chatgpt':
                    if expected.get(digest(canonical(configuration)))!=configuration:
                        raise DevelopmentError('development_cleanup_identity_changed')
                    continue
                tid,label=configuration['turn_id'],configuration['model']
                if tid==current_id:
                    action=step['action'];kind=action['kind'];wanted=action.get('name') if kind=='test' else 'patch_file' if kind=='patch' else 'read_file'
                    if label!=wanted:raise DevelopmentError('development_cleanup_identity_changed')
                elif tid in history:
                    if tid in history_seen:raise DevelopmentError('development_cleanup_identity_changed')
                    history_seen.add(tid)
                    kind=history[tid]['kind']
                    labels={suite['name'] for suite in manifest['tests']} if kind=='test' else {'patch_file'} if kind=='patch' else {'read_file'}
                    if label not in labels:raise DevelopmentError('development_cleanup_identity_changed')
                    if record['phase']!='finished':raise DevelopmentError('development_cleanup_receipt_missing')
                else:raise DevelopmentError('development_cleanup_identity_changed')
                guard=ToolRuntimeGuard(worker.runtime,worker.control,scope,action_label=label,operation_id=operation_id)
                tool_work={**work,'model':label}
                if guard.configuration(tool_work,tid)!=configuration:raise DevelopmentError('development_cleanup_identity_changed')
                tools.append((guard,tool_work,record))
            if history_seen!=set(history):raise DevelopmentError('development_cleanup_receipt_missing')
            # Validate all unsettled local effects before attempting any finish.
            statuses={}
            for guard,tool_work,record in tools:
                if record['phase']!='finished':statuses[record['id']]=_effect_receipt(step,manifest,broker,
                    configuration=config,workflow_id=operation_id)
            for turn in stored['turns']:
                if turn['status'] not in ('completed','blocked','cancelled'):
                    raise DevelopmentError('development_cleanup_receipt_missing')
                result=worker.store.turn_result(turn['id'])
                if result.get('verification',{}).get('cleanup_confirmed') is not True:
                    raise DevelopmentError('development_cleanup_receipt_missing')
                model.confirm_cached(work,turn['id'],result);finished+=1
            for guard,tool_work,record in tools:
                guard.operation=record
                if record['phase']=='finished':
                    receipt=record.get('receipt',{})
                    if receipt.get('cleanup_confirmed') is not True:raise DevelopmentError('development_cleanup_receipt_missing')
                    status=receipt.get('outcome')
                else:status=statuses[record['id']]
                guard.finish_action(tool_work,record['configuration']['turn_id'],status=status,cleanup_confirmed=True);finished+=1
            # Every actual local Runtime record was associated and confirmed.
            for row in rows:
                record=worker.runtime.get(row['id'])
                if record['phase']!='finished' or record['receipt'].get('cleanup_confirmed') is not True:
                    raise DevelopmentError('development_cleanup_receipt_missing')
        return {'operation_id':operation_id,'cleanup_confirmed':True,'receipts_confirmed':finished,
                'status':'needs_review','reason':'development_identity_cleanup_only'}
