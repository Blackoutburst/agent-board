"""Concrete, thread-owned historical delivery/opinion input; no reviewer replay.

Historical data is never current publication, done or merge authority. The
existing Git guard can perform authenticated readonly cleanup lookups through
its concrete DevelopmentDeliveryProof. No reviewer RPC or model is invoked.
"""
from contextlib import ExitStack
import threading

from .dev_journal import decode
from .dev_manifest import canonical,digest
from .development_delivery import DevelopmentDeliveryProof
from .git_delivery import LocalGitDeliveryController
from .git_workspace import _operation
from .review_worker import DeliveryReviewWorker,ReviewRuntimeGuard,_agent_id,_opinion
from .run_store import RunStore
from .runtime_journal import RuntimeJournal
from .team_role_contract import fail
from .work_journal import WorkJournal


class TeamWriterSource:
    """A code factory opens exact existing handles on its caller's thread.

    Own their lifetime using an ExitStack. Passing metadata, callbacks or a
    successful flag in place of these concrete SDK components is rejected.
    """
    def __init__(self,resources,deliveries,review_worker,*,delivery_operation_id,review_export_id,review_operation_id):
        if (type(resources) is not ExitStack or type(deliveries) is not LocalGitDeliveryController
                or type(deliveries.test_guard) is not DevelopmentDeliveryProof
                or type(review_worker) is not DeliveryReviewWorker
                or type(review_worker.journal) is not WorkJournal or type(review_worker.run_store) is not RunStore
                or type(review_worker.runtime_journal) is not RuntimeJournal):fail('team_writer_concrete_source_required')
        self.resources,self.deliveries,self.worker=resources,deliveries,review_worker
        self.delivery_id=_operation(delivery_operation_id);self.export_id=_operation(review_export_id)
        self.review_id=_operation(review_operation_id);self.thread=threading.get_ident();self.closed=False
        self.fingerprint=None
        try:self.fingerprint=digest(canonical(self.read()))
        except BaseException:self.close();raise

    def __enter__(self):
        try:self.read();return self
        except BaseException:self.close();raise
    def __exit__(self,*_):self.close()
    def __reduce_ex__(self,_):raise TypeError('Live team sources cannot be serialized')

    def read(self):
        if self.closed or threading.get_ident()!=self.thread:fail('team_writer_source_thread_required')
        worker=self.worker;op=worker.journal.get(self.review_id);config=op['configuration'];payload=op['payload']
        if op['stage']!='complete' or config.get('kind')!='delivery_review_v1':fail('team_writer_review_incomplete')
        selection=config['selection'];scope=op['scope']
        if (scope['agent_id']==selection['author_agent_id'] or payload['opinion'].get('selection')!=selection
                or payload['opinion']!={**_opinion(payload['result_text']),'selection':selection}):fail()
        work=worker.run_store.get_work(payload['work_id']);status=worker.run_store.status(work['id'])
        if (work['agent_id']!=_agent_id(scope,selection,op['id'])
                or any(work[key]!=scope[key] for key in ('owner_id','project_id','ticket_id'))
                or work['model']!=config['model'] or work['workspace']!=config['workspace']
                or work['executable']!=config['executable'] or len(status['turns'])!=1):fail()
        turn=status['turns'][0];result=worker.run_store.turn_result(turn['id'])
        if (turn['id']!=payload['turn_id'] or turn['request_id']!=payload['run_request_id'] or turn['status']!='completed'
                or turn.get('requires_review') or result.get('status')!='completed' or result.get('uncertain') is True
                or result.get('cleanup_failed') is True or result.get('text')!=payload['result_text']
                or type(result.get('verification')) is not dict
                or any(result['verification'].get(key) is not True for key in ('cleanup_confirmed','tool_free'))):fail()
        guard=ReviewRuntimeGuard(worker.runtime_journal,worker.runtime_client,scope,selection,op['id'])
        record=worker.runtime_journal.find(guard.configuration(work,turn['id']))
        if record is None or record['phase']!='finished' or record['start_confirmed']!=1:fail()
        rows=worker.runtime_journal.db.execute('SELECT id FROM executions LIMIT 2').fetchall()
        if len(rows)!=1 or rows[0]['id']!=record['id']:fail()
        guard.operation=record;receipt=guard.metadata(record['receipt'])
        if receipt['state']!='finished' or receipt['cleanup_confirmed'] is not True or receipt['outcome']!='completed':fail()
        with self.deliveries._record(self.deliveries.review_root,self.export_id) as (private,_):
            exported=decode(private.read('review.json',32768))
        if exported['request']['scope']!=scope or exported['request']['selection']!=selection:fail()
        if self.deliveries.review_guard(self.delivery_id,self.export_id,scope=scope,selection=selection,
                context={'fingerprint':selection['context_sha256']},material=config['material'],workspace=exported['workspace']) is not True:fail()
        if worker.journal.get(self.review_id)!=op or worker.runtime_journal.get(record['id'])!=record:fail()
        value={'historical_data':True,'delivery_operation_id':self.delivery_id,'review_operation_id':self.review_id,
            'review_export_id':self.export_id,'owner_id':scope['owner_id'],'project_id':scope['project_id'],
            'ticket_id':scope['ticket_id'],'reviewer_agent_id':scope['agent_id'],'selection':selection,
            'material':config['material'],'opinion':{key:payload['opinion'][key] for key in ('verdict','text')},
            'review_record_sha256':digest(canonical(op)),'runtime_record_sha256':digest(canonical(record)),
            'result_sha256':digest(canonical(result))}
        if self.fingerprint is not None and digest(canonical(value))!=self.fingerprint:fail('team_writer_source_changed')
        return decode(canonical(value))

    def close(self):
        if threading.get_ident()!=self.thread:fail('team_writer_source_thread_required')
        if not self.closed:self.closed=True;self.resources.close()
