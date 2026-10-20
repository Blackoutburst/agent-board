"""Synchronous owner-capacity gate for one tool-free model turn.

There is no listener or process watcher. Heartbeats run only from the transport's
cancellation callback. A lost control response cancels; a lost finish response
retains local review state. Cached private results may reconcile cleanup, never
restart inference.
"""
import hashlib
import json
import math
import re
import time

from .runtime_client import REASONS
from .runtime_journal import RuntimeJournal
from .run_store import StoreError,RunStore


class RuntimeControlError(StoreError):
    pass


class RuntimeAdmissionRefused(RuntimeControlError):
    """One durable negative admission; no execution/cleanup receipt exists."""


class RuntimeGuard:
    def __init__(self, journal, client, scope, *, clock=time.monotonic):
        self.journal, self.client, self.scope, self.clock = journal, client, dict(scope), clock
        if client.endpoint != scope['board_endpoint'] or client.session_id != scope['board_session_id']:
            raise RuntimeControlError('runtime_scope_mismatch')
        self.operation = None
        self.last_heartbeat = 0
        self.control_failed = False

    def configuration(self, work, turn_id):
        identity={key:self.scope[key] for key in ('board_endpoint','board_session_id','agent_id')}
        if work.get('decision_memory') is not None:
            identity['decision_memory']=RunStore._guard_value(work['decision_memory'])
        local_agent = 'board-agent-' + hashlib.sha256(json.dumps(identity,ensure_ascii=False,sort_keys=True,
            separators=(',',':'),allow_nan=False).encode()).hexdigest()
        if (any(work.get(key) != self.scope[key] for key in ('owner_id','project_id','ticket_id'))
                or work.get('agent_id') != local_agent):
            raise RuntimeControlError('runtime_scope_mismatch')
        return RuntimeJournal.configuration(self.scope, work['id'], turn_id, work['model'])

    def expected(self, config):
        return {**{key:config['scope'][key] for key in ('owner_id','agent_id','project_id','ticket_id')},
            'session_id':config['scope']['board_session_id'],
            **{key:config[key] for key in ('work_id','turn_id','provider','model')}}

    def reservation(self, config):
        return 'reserve_execution', {'idempotency_key':self.operation['key'],
            **{key:config['scope'][key] for key in ('project_id','ticket_id')},
            **{key:config[key] for key in ('work_id','turn_id','provider','model')}}

    def metadata(self, value):
        if not isinstance(value, dict) or self.operation is None:
            raise RuntimeControlError('runtime_receipt_invalid')
        config = self.operation['configuration']
        expected = self.expected(config)
        if any(value.get(key) != item for key,item in expected.items()):
            raise RuntimeControlError('runtime_scope_mismatch')
        if (not isinstance(value.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',value['id'])
                or type(value.get('revision')) is not int or value['revision']<1
                or value.get('state') not in ('reserved','running','finished','needs_review')
                or any(type(value.get(key)) is not bool for key in ('stop_requested','should_stop','cleanup_confirmed'))
                or type(value.get('profile_version')) is not int or value['profile_version']<1
                or not isinstance(value.get('device_id'),str) or not value['device_id']):
            raise RuntimeControlError('runtime_receipt_invalid')
        previous=self.operation.get('receipt')
        if previous and any(value.get(key)!=previous.get(key) for key in ('id','device_id','profile_version')):
            raise RuntimeControlError('runtime_identity_changed')
        # Never persist arbitrary remote fields or diagnostics.
        fields={*expected,'id','revision','state','stop_requested','should_stop','cleanup_confirmed','profile_version','device_id'}
        result={key:value[key] for key in fields}
        for key in ('reason','outcome','control_reason'):
            item=value.get(key,'')
            allowed = REASONS if key=='reason' else ({'', 'completed', 'blocked', 'cancelled'} if key=='outcome'
                else {'', 'finished', 'stop_requested', 'cleanup_unconfirmed', 'authorization_changed', 'owner_paused'})
            if not isinstance(item,str) or item not in allowed:
                raise RuntimeControlError('runtime_receipt_invalid')
            result[key]=item
        return result

    def save(self, phase, receipt=None, *, start_confirmed=False):
        self.operation=self.journal.update(self.operation['id'],self.operation['revision'],phase,
            receipt if receipt is not None else self.operation.get('receipt'),start_confirmed=start_confirmed)

    def review(self):
        if self.operation and self.operation['phase'] not in ('finished','not_admitted','abandoned_never_started'):
            try:self.save('needs_review')
            except Exception:pass

    def reject_admission(self,result):
        # Only the explicit message protocol implements negative admission.
        return None

    def before(self, work, turn_id):
        self.control_failed=False
        self.operation,created=self.journal.begin(self.configuration(work,turn_id))
        if not created:
            raise RuntimeControlError('runtime_existing_turn_requires_review')
        try:
            config=self.operation['configuration']
            name, arguments = self.reservation(config)
            result=self.client.call(name,arguments)
            self.reject_admission(result)
            if not isinstance(result,dict) or result.get('reserved') is not True or type(result.get('retried')) is not bool:
                raise RuntimeControlError('runtime_reservation_unconfirmed')
            receipt=self.metadata(result.get('execution'))
            self.save('reserved',receipt)
            if result['retried'] or receipt['state']!='reserved' or receipt['should_stop']:
                raise RuntimeControlError('runtime_reservation_requires_review')
            self.save('starting')
            started=self.metadata(self.client.call('start_execution',{'execution_id':receipt['id'],
                'expected_revision':receipt['revision']}))
            if started['state']!='running' or started['should_stop']:
                raise RuntimeControlError('runtime_start_unconfirmed')
            self.save('active',started,start_confirmed=True)
            self.last_heartbeat=self.clock()
        except RuntimeAdmissionRefused:
            raise
        except Exception:
            self.review()
            raise RuntimeControlError('runtime_start_requires_review') from None

    def should_cancel(self):
        if self.control_failed:return True
        try:
            now=self.clock()
            if not math.isfinite(now):raise ValueError
            receipt=self.operation['receipt']
            if now-self.last_heartbeat<5:return bool(receipt['should_stop'])
            current=self.metadata(self.client.call('heartbeat_execution',{'execution_id':receipt['id'],
                'expected_revision':receipt['revision']}))
            self.save('active',current)
            self.last_heartbeat=now
            return current['should_stop'] or current['state']!='running'
        except Exception:
            self.control_failed=True
            self.review()
            return True

    def finish(self, work, turn_id, result):
        config=self.configuration(work,turn_id)
        if self.operation is None or self.operation['configuration']!=config:
            raise RuntimeControlError('runtime_scope_mismatch')
        if self.operation['phase'] in ('not_admitted','abandoned_never_started'):
            raise RuntimeControlError('runtime_not_admitted')
        try:
            found=self.client.call('lookup_execution',{'idempotency_key':self.operation['key']})
            if not isinstance(found,dict) or found.get('found') is not True:
                raise RuntimeControlError('runtime_reservation_unconfirmed')
            receipt=self.metadata(found.get('execution'))
            verification=result.get('verification')
            cleanup=(isinstance(verification,dict) and verification.get('cleanup_confirmed') is True
                and result.get('cleanup_failed') is not True
                and result.get('reason') not in ('cleanup_failed','runner_error','interrupted'))
            status=result.get('status')
            if status not in ('completed','blocked','cancelled'):
                raise RuntimeControlError('runtime_result_invalid')
            reason=result.get('reason','')
            if status=='completed':reason='completed'
            elif reason not in REASONS:reason='execution_failed'
            if not cleanup and reason not in ('quota_or_rate_limit','authentication_failed'):
                reason='cleanup_unconfirmed'
            if receipt['state'] not in ('reserved','finished') and not self.operation['start_confirmed']:
                raise RuntimeControlError('runtime_start_unconfirmed')
            if receipt['state']=='finished':
                if (not cleanup or receipt['cleanup_confirmed'] is not True or receipt.get('outcome')!=status
                        or receipt.get('reason')!=reason):
                    raise RuntimeControlError('runtime_finish_conflict')
            else:
                receipt=self.metadata(self.client.call('finish_execution',{'execution_id':receipt['id'],
                    'expected_revision':receipt['revision'],'status':status,'cleanup_confirmed':cleanup,'reason':reason}))
                if (receipt['cleanup_confirmed']!=cleanup or receipt.get('outcome')!=status or receipt.get('reason')!=reason
                        or receipt['state']!=('finished' if cleanup else 'needs_review')):
                    raise RuntimeControlError('runtime_finish_unconfirmed')
            if self.operation['phase']=='finished':return
            if self.operation['phase'] not in ('active','needs_review'):self.review()
            self.save('finished' if cleanup else 'needs_review',receipt)
            if not cleanup:raise RuntimeControlError('runtime_cleanup_unconfirmed')
        except Exception:
            self.review()
            raise RuntimeControlError('runtime_finish_requires_review') from None

    def confirm_cached(self, work, turn_id, result):
        self.operation=self.journal.find(self.configuration(work,turn_id))
        if self.operation is None:raise RuntimeControlError('runtime_cached_turn_unregistered')
        if self.operation['phase']=='not_admitted':
            if result!={'status':'blocked','reason':'runtime_capacity_exhausted','text':'','uncertain':False,
                    'verification':{'runtime_not_admitted':True}}:
                raise RuntimeControlError('runtime_refusal_result_conflict')
            raise RuntimeAdmissionRefused('runtime_capacity_exhausted')
        return self.finish(work,turn_id,result)
