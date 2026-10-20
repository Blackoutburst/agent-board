"""Exact Message3 disposition CAS and inert routing; synthetic journals only."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

from blackoutburst_agents.host_config import HostError,digest
from blackoutburst_agents.message_journal import MessageJournal,validate_executed_disposition
from blackoutburst_agents.message_worker import MessageWorker


class ExecutedTerminalTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve();self.messages=MessageJournal(self.root/'messages')
        self.addCleanup(self.messages.close)
        self.reader=self.messages.reader('target','a'*64,'now',50)
        self.messages.bind_feed(self.reader['id'],dict(feed_id='b'*32,start_at='now',limit=50,human_only=True,checkpoint=0))
        self.delivery=self.messages.ingest(self.reader['id'],dict(feed_id='b'*32,delivery_id='c'*32,
            checkpoint=0,messages=[{}]*3,human_only=True),[
                dict(message_id=f'message-{n}',thread_id='thread',message_hash='d'*64) for n in range(3)])
        self.operations=[]
        for initial in self.delivery['operations']:
            op=self.messages.bind_worker(initial['id'],str(uuid.uuid4()))
            op=self.messages.update(op['id'],op['revision'],'prepared',prompt='PRIVATE_ORIGINAL_PROMPT',
                context_sha256='e'*64,context_refs={'private':'PRIVATE_ORIGINAL_CONTEXT'})
            op=self.messages.update(op['id'],op['revision'],'running',**op['payload'])
            self.operations.append(op)
        op=self.operations[0]
        self.before=self.messages.update(op['id'],op['revision'],'needs_review',
            **{**op['payload'],'reason':'message_result_requires_review'})
        self.intent=str(uuid.uuid4());self.proof_sha='f'*64

    def snapshot(self):
        return (self.messages.db.execute('PRAGMA user_version').fetchone()[0],
            tuple(self.messages.db.iterdump()))

    def dispose(self,*,expected=None,payload_sha=None,intent=None,proof=None):
        row={k:v for k,v in self.before.items() if k!='payload'} if expected is None else expected
        return self.messages._dispose_executed_blocked(row,
            payload_sha256=payload_sha or digest(self.before['payload']),intent_id=intent or self.intent,
            proof_sha256=proof or self.proof_sha)

    def test_terminal_keeps_original_payload_and_only_marks_an_explicit_disposition(self):
        other=[self.messages.operation(o['id']) for o in self.operations[1:]]
        result=self.dispose()
        self.assertEqual(self.messages.db.execute('PRAGMA user_version').fetchone()[0],3)
        marker=validate_executed_disposition(result)
        payload=dict(result['payload']);payload.pop('disposition')
        self.assertEqual(payload,self.before['payload'])
        self.assertEqual(marker,{'schema':'message-executed-disposition-v1','intent_id':self.intent,
            'proof_sha256':self.proof_sha,'original_payload_sha256':digest(payload),
            'reason':'message_executed_blocked_disposed'})
        expected={**self.before,'state':'executed_disposed','revision':self.before['revision']+1,
            'payload':{**self.before['payload'],'disposition':marker}}
        self.assertEqual(result,expected)
        self.assertEqual([self.messages.operation(o['id']) for o in self.operations[1:]],other)
        self.assertEqual(self.messages.delivery(self.delivery['id'])['state'],'pending')
        self.assertEqual(self.messages.db.execute('SELECT checkpoint FROM readers').fetchone()[0],0)

    def test_exact_cas_hash_and_intent_fields_refuse_without_mutating(self):
        original={k:v for k,v in self.before.items() if k!='payload'}
        for args in ({'expected':{**original,'revision':True}},
                {'expected':{**original,'revision':original['revision']-1}},
                {'expected':{**original,'work_id':str(uuid.uuid4())}},
                {'expected':{**original,'unknown':'PRIVATE'}},
                {'payload_sha':'0'*64},{'intent':'not-uuid'},{'proof':'NOT-SHA'}):
            with self.subTest(args=args):
                before=self.snapshot()
                with self.assertRaises(HostError):self.dispose(**args)
                self.assertEqual(self.snapshot(),before)

    def test_state_and_reason_are_exact_and_generic_mutation_cannot_create_or_change_terminal(self):
        op=self.operations[1]
        row={k:v for k,v in op.items() if k!='payload'}
        with self.assertRaises(HostError):self.dispose(expected=row,payload_sha=digest(op['payload']))
        before=self.snapshot()
        with self.assertRaisesRegex(HostError,'explicit_message_disposition_required'):
            self.messages.update(self.before['id'],self.before['revision'],'executed_disposed',**self.before['payload'])
        self.assertEqual(self.snapshot(),before)
        terminal=self.dispose();before=self.snapshot()
        for state in ('executed_disposed','complete','ignored','needs_review'):
            with self.assertRaises(HostError):
                self.messages.update(terminal['id'],terminal['revision'],state,**terminal['payload'])
        with self.assertRaises(HostError):self.messages.bind_worker(terminal['id'],terminal['worker_id'])
        with self.assertRaises(HostError):self.dispose()
        self.assertEqual(self.snapshot(),before)

    def test_unknown_and_rehashed_markers_or_lost_reason_are_not_adopted(self):
        terminal=self.dispose();original=json.dumps(terminal['payload'])
        for change in ('unknown','hash','reason','intent','payload','list_payload'):
            with self.subTest(change=change):
                payload=deepcopy(terminal['payload'])
                if change=='unknown':payload['disposition']['authority']=True
                elif change=='hash':payload['disposition']['original_payload_sha256']='0'*64
                elif change=='reason':payload['reason']='message_executed_blocked_disposed'
                elif change=='intent':payload['disposition']['intent_id']='noncanonical'
                elif change=='payload':payload['prompt']='PRIVATE_CHANGED'
                else:payload=list(payload.items())
                self.messages.db.execute('UPDATE operations SET payload_json=? WHERE id=?',
                    (json.dumps(payload),terminal['id']))
                with self.assertRaises(HostError):self.messages.operation(terminal['id'])
                self.messages.db.execute('UPDATE operations SET payload_json=? WHERE id=?',(original,terminal['id']))

    def test_trigger_or_temp_shadow_refuses_before_writer(self):
        for ddl in (
                'CREATE TRIGGER bad AFTER UPDATE ON operations BEGIN DELETE FROM readers; END',
                'CREATE TEMP VIEW operations AS SELECT * FROM main.operations'):
            self.messages.db.execute(ddl);before=self.snapshot()
            try:
                with self.assertRaises(HostError):self.dispose()
                self.assertEqual(self.snapshot(),before)
            finally:
                self.messages.db.execute('DROP '+('TRIGGER bad' if 'TRIGGER' in ddl else 'VIEW temp.operations'))

    def test_legacy_abandonment_does_not_downgrade_and_both_histories_survive_ack(self):
        terminal=self.dispose()
        for op in self.operations[1:]:
            expected={k:v for k,v in op.items() if k!='payload'}
            result=self.messages._abandon(expected,digest(op['payload']),str(uuid.uuid4()),'a'*64)
            self.assertEqual(result['state'],'abandoned')
            self.assertEqual(self.messages.db.execute('PRAGMA user_version').fetchone()[0],3)
        self.assertEqual(self.messages.operation(terminal['id']),terminal)
        self.messages.begin_ack(self.delivery['id'])
        ack=dict(feed_id='b'*32,delivery_id='c'*32,checkpoint=1,acknowledged=True)
        self.messages.finish_ack(self.delivery['id'],ack)
        self.assertEqual(self.messages.operation(terminal['id']),terminal)
        self.assertEqual(self.messages.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],3)
        self.assertEqual(self.messages.finish_ack(self.delivery['id'],ack)['state'],'acked')
        self.assertEqual(self.messages.operation(terminal['id']),terminal)

    def test_terminal_worker_receipt_is_closed_and_never_registers_or_executes(self):
        terminal=self.dispose()
        def forbidden(*args,**kwargs):raise AssertionError('no execution or client use')
        client=type('Client',(),{'register':forbidden,'call':forbidden})()
        worker=MessageWorker(self.messages,None,client,runtime_guard_factory=forbidden,runner_factory=forbidden)
        before=self.snapshot()
        receipt=worker.run(terminal['id'],{},'owner',should_cancel=forbidden)
        self.assertEqual(receipt,{'status':'executed_disposed','reason':'message_executed_blocked_disposed',
            'worker_operation_id':terminal['worker_id'],'message_operation_id':terminal['id'],
            'disposition_intent_id':self.intent,'proof_sha256':self.proof_sha})
        self.assertNotIn('PRIVATE',json.dumps(receipt))
        self.assertEqual(self.snapshot(),before)

    def test_new_reader_refuses_rewound_marker_and_previous_reader_refuses_message3(self):
        self.dispose();self.messages.close()
        repo=Path(__file__).resolve().parents[3]
        if shutil.which('git') is None or not (repo/'.git').exists():self.skipTest('public Git parent needed for exact legacy reader')
        source=subprocess.run(['git','show',
            'b6a0795089c3c038b94211160a676ea19cccf6e5:integrations/codex-personal/blackoutburst_agents/message_journal.py'],
            cwd=repo,check=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5).stdout
        namespace={'__name__':'synthetic_legacy_message_reader','__package__':'blackoutburst_agents'}
        exec(compile(source,'<public-b6-message-reader>','exec'),namespace)
        path=self.root/'messages'/'messages.sqlite3';before=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(HostError,'unsupported_message_journal'):namespace['MessageJournal'](self.root/'messages')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
        self.messages=MessageJournal(self.root/'messages');self.addCleanup(self.messages.close)
        self.messages.db.execute('PRAGMA user_version=2');self.messages.close()
        with self.assertRaises(HostError):MessageJournal(self.root/'messages')


if __name__=='__main__':unittest.main()
