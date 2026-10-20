"""Finite, explicit catalogue source change. No worker, service or journal opener.

The protected caller supplies source and stopped-child guards. Their successful
observations are current checks, never authority serialized in the proposal.
A local purpose fences startup before genuine human Stop ACKs are settled; only
then is the separate remote replacement CAS fixed. Historical receipts are not
fresh authority and no old abandonment checkpoint is rewritten.
"""
from copy import deepcopy
import json

from .host_config import HostError,digest
from .managed_host import _opaque,_sha
from .server_controller import launch_catalog
from .server_control_protocol import arguments,response,entry,catalog_fence

LOCAL_METHOD='catalog_source_transition'
SCHEMA='blackoutburst-catalog-source-transition-v1'
EXECUTED_SCHEMA='blackoutburst-catalog-source-transition-v2'


def encoded(value,limit=32768):
    try:
        data=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)
        if len(data.encode('ascii'))>limit:raise ValueError
        return data
    except (TypeError,ValueError,RecursionError,UnicodeError):raise HostError('catalog_transition_invalid') from None


def proposal(value):
    keys={'schema','request_key','host_id','host_instance','configuration_sha256','old_release_sha256',
        'new_release_sha256','old_catalog','new_catalog','preserved_sha256','closures','proposal_sha256'}
    if type(value) is dict and value.get('schema')==EXECUTED_SCHEMA:keys.add('executed_closures')
    if type(value) is not dict or set(value)!=keys or value['schema'] not in (SCHEMA,EXECUTED_SCHEMA):
        raise HostError('catalog_transition_invalid')
    encoded(value)
    for k in ('request_key','host_id','host_instance'):_opaque(value[k])
    for k in ('configuration_sha256','old_release_sha256','new_release_sha256','preserved_sha256','proposal_sha256'):_sha(value[k])
    if value['old_release_sha256']==value['new_release_sha256']:raise HostError('catalog_transition_invalid')
    for name in ('old_catalog','new_catalog'):
        rows=value[name]
        if type(rows) is not list or not 1<=len(rows)<=16:raise HostError('catalog_transition_invalid')
        for row in rows:entry(row)
        if rows!=sorted(rows,key=lambda r:r['catalog_key']) or len({r['catalog_key'] for r in rows})!=len(rows):
            raise HostError('catalog_transition_invalid')
    if len(value['old_catalog'])!=len(value['new_catalog']):raise HostError('catalog_transition_invalid')
    for old,new in zip(value['old_catalog'],value['new_catalog']):
        if (any(old[k]!=new[k] for k in ('catalog_key','profile_id','profile_version','mode'))
                or old['launchspec_sha256']==new['launchspec_sha256']):raise HostError('catalog_transition_invalid')
    closures=value['closures']
    if type(closures) is not list or len(closures)>32:raise HostError('catalog_transition_invalid')
    for row in closures:
        if (type(row) is not dict or set(row)!={'intent_id','request_key','revision','proposal_sha256'}
                or type(row['revision']) is not int or row['revision']!=6):raise HostError('catalog_transition_invalid')
        _opaque(row['intent_id']);_opaque(row['request_key']);_sha(row['proposal_sha256'])
    if len({r['intent_id'] for r in closures})!=len(closures):raise HostError('catalog_transition_invalid')
    executed=value.get('executed_closures',[])
    if type(executed) is not list or len(executed)>32:raise HostError('catalog_transition_invalid')
    for row in executed:
        if (type(row) is not dict or set(row)!={'intent_id','request_key','revision','proposal_sha256'}
                or type(row['revision']) is not int or row['revision']!=5):raise HostError('catalog_transition_invalid')
        _opaque(row['intent_id']);_opaque(row['request_key']);_sha(row['proposal_sha256'])
    if (len({r['intent_id'] for r in executed})!=len(executed)
            or {r['intent_id'] for r in executed}&{r['intent_id'] for r in closures}):
        raise HostError('catalog_transition_invalid')
    if digest({k:v for k,v in value.items() if k!='proposal_sha256'})!=value['proposal_sha256']:
        raise HostError('catalog_transition_invalid')
    return deepcopy(value)


def receipt(p,value):
    p=proposal(p)
    if (type(value) is not dict or set(value)!={'schema','proposal_sha256','catalog_version','instances_sha256'}
            or value['schema']!='blackoutburst-catalog-source-transition-receipt-v1'
            or value['proposal_sha256']!=p['proposal_sha256'] or type(value['catalog_version']) is not int
            or not 2<=value['catalog_version']<2**63):raise HostError('catalog_transition_invalid')
    _sha(value['instances_sha256']);encoded(value,4096)
    return deepcopy(value)


class CatalogSourceTransition:
    def __init__(self,host,client,*,source_guard,stopped_guard,abandonment=None,executed_disposition=None):
        if not callable(source_guard) or not callable(stopped_guard):raise HostError('catalog_transition_guard_required')
        self.host,self.client=host,client
        self.source_guard,self.stopped_guard,self.abandonment=source_guard,stopped_guard,abandonment
        self.executed_disposition=executed_disposition
        if executed_disposition is not None:
            from .message_executed_disposition import MessageExecutedDisposition
            if not isinstance(executed_disposition,MessageExecutedDisposition) or executed_disposition.host is not host:
                raise HostError('catalog_transition_closure_invalid')
        if abandonment is not None:
            from .message_admission_abandonment import MessageAdmissionAbandonment
            if not isinstance(abandonment,MessageAdmissionAbandonment) or abandonment.host is not host:
                raise HostError('catalog_transition_closure_invalid')

    def _guard(self,p=None,*,children=True):
        self.host._require_lock()
        if children and self.stopped_guard() is not True:raise HostError('catalog_transition_children_unconfirmed')
        if p is not None:
            p=proposal(p)
            if self.source_guard(p['old_release_sha256'],p['new_release_sha256'],p['configuration_sha256']) is not True:
                raise HostError('catalog_transition_sources_unconfirmed')
            self.host._catalog_source_validate(p)
        else:self.host._catalog_source_clean()

    def _closure_rows(self):
        self.host._check_abandonments()
        if self.host._db.execute('PRAGMA user_version').fetchone()[0] not in (3,4):return []
        historical=set()
        for row in self.host.catalog_source_transitions():
            if row['state']=='complete':historical.update(c['intent_id'] for c in row['arguments']['closures'])
        records=[self.host._abandon_record(intent_id=r[0]) for r in self.host._db.execute(
            'SELECT id FROM host_message_abandonments ORDER BY rowid LIMIT 33')]
        if len(records)>32:raise HostError('catalog_transition_closure_invalid')
        result=[]
        for r in records:
            if r['phase']!='complete':raise HostError('catalog_transition_closure_invalid')
            if r['id'] in historical:continue
            if self.abandonment is None:raise HostError('catalog_transition_closure_required')
            status=self.abandonment.status(intent_id=r['id'])
            p=self.abandonment.proposal(r['request_key'])
            if status!={'intent_id':r['id'],'revision':6,'phase':'complete','reason':'message_abandon_complete'}:
                raise HostError('catalog_transition_closure_invalid')
            result.append({'intent_id':r['id'],'request_key':r['request_key'],'revision':6,'proposal_sha256':p['proposal_sha256']})
        return result

    def _executed_closure_rows(self):
        self.host._check_executed_dispositions()
        if self.host._db.execute('PRAGMA user_version').fetchone()[0]!=4:return []
        historical=set()
        for row in self.host.catalog_source_transitions():
            if row['state']=='complete':
                historical.update(c['intent_id'] for c in row['arguments'].get('executed_closures',[]))
        rows=self.host._db.execute('SELECT id FROM host_message_dispositions ORDER BY rowid LIMIT 33').fetchall()
        if len(rows)>32:raise HostError('catalog_transition_closure_invalid')
        result=[]
        for raw in rows:
            record=self.host._executed_record(intent_id=raw[0])
            if record['phase']!='complete':raise HostError('catalog_transition_closure_invalid')
            if record['id'] in historical:continue
            if self.executed_disposition is None:raise HostError('catalog_transition_closure_required')
            status=self.executed_disposition.status(intent_id=record['id'])
            saved=self.executed_disposition.proposal(record['request_key'])
            if (status.get('intent_id')!=record['id'] or status.get('revision')!=5
                    or status.get('phase')!='complete' or saved!=record['proposal']):
                raise HostError('catalog_transition_closure_invalid')
            result.append({'intent_id':record['id'],'request_key':record['request_key'],
                'revision':5,'proposal_sha256':saved['proposal_sha256']})
        return result

    def plan(self,*,old_release_sha256,new_release_sha256,request_key):
        self.host.require_no_catalog_source_transition()
        self._guard();_sha(old_release_sha256);_sha(new_release_sha256);_opaque(request_key)
        c=self.host.control();cfg=c['configuration']
        value={'schema':SCHEMA,'request_key':request_key,'host_id':self.host.managed_settings()['host_id'],
            'host_instance':c['instance_id'],'configuration_sha256':c['hash'],
            'old_release_sha256':old_release_sha256,'new_release_sha256':new_release_sha256,
            'old_catalog':sorted(launch_catalog(cfg,old_release_sha256),key=lambda r:r['catalog_key']),
            'new_catalog':sorted(launch_catalog(cfg,new_release_sha256),key=lambda r:r['catalog_key']),
            'preserved_sha256':self.host._catalog_source_preserved(),'closures':self._closure_rows()}
        if self.host._db.execute('PRAGMA user_version').fetchone()[0]==4:
            value['schema']=EXECUTED_SCHEMA
            value['executed_closures']=self._executed_closure_rows()
        value['proposal_sha256']=digest(value);p=proposal(value);self._guard(p)
        return p

    def _poll(self,p,*,new=False):
        self._guard(p)
        snapshot=self.client.call('poll_instance_commands',{'host_id':p['host_id'],'limit':20})
        self._guard(p);response('poll_instance_commands',snapshot)
        expected=p['new_catalog'] if new else p['old_catalog']
        if snapshot['host_id']!=p['host_id'] or not snapshot['enabled'] or snapshot['catalog_version']<1:
            raise HostError('catalog_transition_remote_conflict')
        rows=sorted(snapshot['instances'],key=lambda r:r['catalog_key'])
        if len(rows)!=len(expected):raise HostError('catalog_transition_remote_conflict')
        for i,e in zip(rows,expected):
            local=self.host.managed_target(e['catalog_key'])
            if (any(i[k]!=e[k] for k in e) or not i['active'] or i['id']!=local['instance_id']
                    or i['host_id']!=p['host_id'] or i['can_start'] is not True or i['control_reason']!=''):
                raise HostError('catalog_transition_remote_conflict')
        return snapshot

    @staticmethod
    def _stops(snapshot,*,settled=False):
        for i in snapshot['instances']:
            c=i['current_command']
            if (i['desired_state']!='stopped' or c is None or c['action']!='stop'
                    or c['status'] not in (('complete',) if settled else ('queued','accepted','complete'))
                    or c['budget']!=0 or c['generation']!=i['generation']
                    or c['instance_version']!=i['version'] or c['launchspec_sha256']!=i['launchspec_sha256']):
                raise HostError('catalog_transition_human_stop_required')
            if settled and (i['observed_state']!='stopped' or not i['cleanup_confirmed']):
                raise HostError('catalog_transition_stop_unconfirmed')

    def _effect(self,p,key,method,args):
        self._guard(p)
        effect=self.host.begin_managed_effect(key,method,args)
        if effect['state']=='complete':return effect['result']
        if effect['state']!='pending':raise HostError('catalog_transition_effect_conflict')
        value=self.client.call(method,effect['arguments'])
        self._guard(p);response(method,value)
        return self.host.finish_managed_effect(key,value)['result']

    def _finite_stop(self,p):
        """At most one ACK and report per target; no start/restart or worker."""
        p=proposal(p);self._guard(p)
        intent=self.host.managed_effect('source-'+digest(p['request_key']))
        if (intent is None or intent['method']!=LOCAL_METHOD or intent['state']!='pending'
                or intent['arguments']!=p or self.host.managed_effect('replace-'+p['proposal_sha256']) is not None):
            raise HostError('catalog_transition_intent_required')
        state=self._poll(p);self._stops(state)
        for i in state['instances']:
            c=i['current_command']
            self.host.queue_managed_command({'id':c['id'],'target_id':i['catalog_key'],'instance_id':i['id'],
                **{k:c[k] for k in ('generation','action','launchspec_sha256','budget')}})
            self.host.apply_managed_command(c['id'])
            if c['status']=='queued':
                self._effect(p,'ack-'+c['id'],'ack_instance_command',{'host_id':p['host_id'],
                    'command_id':c['id'],'expected_revision':c['revision'],'idempotency_key':'managed-ack-'+c['id']})
        state=self._poll(p);self._stops(state)
        # Recover only exact Stop-bound effects, never a historical ready/start.
        current={i['command_id']:i for i in state['instances']}
        effects=self.host.pending_managed_effects()
        if len(effects)>33:raise HostError('catalog_transition_unsettled')
        seen=set()
        for effect in effects:
            if effect['method']==LOCAL_METHOD:continue
            marker=(effect['method'],effect['arguments'].get('command_id'))
            if marker in seen:raise HostError('catalog_transition_unsettled')
            seen.add(marker)
            args=effect['arguments'];i=current.get(args.get('command_id'))
            if (effect['method'] not in ('ack_instance_command','report_instance_state') or i is None
                    or (effect['method']=='report_instance_state' and
                        (args['state']!='stopped' or args['cleanup_confirmed'] is not True
                         or (args['instance_id']!=i['id'] or any(args[k]!=i[k] for k in ('generation','launchspec_sha256')))))):
                raise HostError('catalog_transition_unsettled')
            self._effect(p,effect['id'],effect['method'],args)
        state=self._poll(p);self._stops(state)
        for i in state['instances']:
            if i['observed_state']=='stopped' and i['cleanup_confirmed'] and i['current_command']['status']=='complete':continue
            args={'host_id':p['host_id'],'instance_id':i['id'],
                'expected_observation_revision':i['observation_revision'],'generation':i['generation'],
                'launchspec_sha256':i['launchspec_sha256'],'command_id':i['command_id'],
                'state':'stopped','cleanup_confirmed':True,'reason':'stopped'}
            key='report-'+digest(args);args['idempotency_key']=key
            self._effect(p,key,'report_instance_state',args)
        final=self._poll(p);self._stops(final,settled=True)
        return final

    def _phase(self,row):
        if row['state']=='complete':return 4,'complete'
        if row['state']!='pending':raise HostError('catalog_transition_effect_conflict')
        remote=self.host.managed_effect('replace-'+row['arguments']['proposal_sha256'])
        if remote is None:return 1,'stop_pending'
        if remote['method']!='replace_server_catalog':raise HostError('catalog_transition_effect_conflict')
        if remote['state']=='pending':return 2,'replace_pending'
        if remote['state']=='complete':return 3,'commit_pending'
        raise HostError('catalog_transition_effect_conflict')

    def status(self,intent_id):
        _opaque(intent_id);row=self.host.managed_effect(intent_id)
        if row is None or row['method']!=LOCAL_METHOD:raise HostError('catalog_transition_absent')
        self._guard(row['arguments']);revision,phase=self._phase(row)
        return {'intent_id':intent_id,'revision':revision,'phase':phase,
                'proposal_sha256':row['arguments']['proposal_sha256']}

    def _consume(self,p):
        # Fresh.status below verifies the original closure in this Host TX.
        # The post-closure child helper is separately checked before the TX.
        self._guard(p,children=False)
        closures=self._closure_rows()
        if p['schema']==EXECUTED_SCHEMA:
            return {'closures':closures,'executed_closures':self._executed_closure_rows()}
        return closures

    def resolve(self,value,*,expected_revision=0):
        p=proposal(value);self._guard(p)
        if type(expected_revision) is not int or expected_revision!=0:raise HostError('catalog_transition_revision_conflict')
        identifier='source-'+digest(p['request_key'])
        if self.host.managed_effect(identifier) is not None:raise HostError('catalog_transition_revision_conflict')
        record=self.host.begin_catalog_source_transition(p,consume_closures=lambda:self._consume(p))
        return self._resume(record)

    def resume(self,intent_id,*,expected_revision,expected_proposal_sha256):
        _opaque(intent_id);_sha(expected_proposal_sha256)
        row=self.host.managed_effect(intent_id)
        if row is None or row['method']!=LOCAL_METHOD:raise HostError('catalog_transition_absent')
        revision,phase=self._phase(row)
        if (type(expected_revision) is not int or revision!=expected_revision
                or row['arguments']['proposal_sha256']!=expected_proposal_sha256):
            raise HostError('catalog_transition_revision_conflict')
        return self._resume(row)

    def _resume(self,record):
        intent_id=record['id'];p=record['arguments'];self._guard(p)
        if record['state']=='complete':return receipt(p,record['result'])
        if record['state']!='pending':raise HostError('catalog_transition_effect_conflict')
        remote_key='replace-'+p['proposal_sha256']
        remote=self.host.managed_effect(remote_key)
        if remote is None:
            final=self._finite_stop(p)
            self.host._catalog_source_settled(except_ids=(intent_id,))
            args={'host_id':p['host_id'],'expected_catalog_version':final['catalog_version'],
                'expected_host_version':final['host_version'],'expected_instances_sha256':catalog_fence(final['instances']),
                'entries':p['new_catalog'],'idempotency_key':remote_key}
            remote=self.host.begin_managed_effect(remote_key,'replace_server_catalog',args)
        if remote['method']!='replace_server_catalog' or remote['arguments']['entries']!=p['new_catalog']:
            raise HostError('catalog_transition_effect_conflict')
        result=self._effect(p,remote_key,'replace_server_catalog',remote['arguments'])
        snapshot=self._poll(p,new=True)
        if (snapshot['catalog_version']!=result['catalog_version']
                or snapshot['host_version']!=remote['arguments']['expected_host_version']
                or snapshot['catalog_version']!=remote['arguments']['expected_catalog_version']+1):raise HostError('catalog_transition_remote_conflict')
        # Receipt is historical transport only: re-observe the unchanged Stop
        # fence, IDs/generation/new hashes from the server immediately before CAS.
        by_id={i['id']:i for i in result['instances']}
        if len(by_id)!=len(snapshot['instances']):raise HostError('catalog_transition_remote_conflict')
        for i in snapshot['instances']:
            old=by_id.get(i['id'])
            if (old is None or catalog_fence([old])!=catalog_fence([i])
                    or i['desired_state']!='stopped' or i['observed_state']!='unknown' or i['cleanup_confirmed']):
                raise HostError('catalog_transition_remote_conflict')
        self._guard(p)
        done={'schema':'blackoutburst-catalog-source-transition-receipt-v1','proposal_sha256':p['proposal_sha256'],
              'catalog_version':snapshot['catalog_version'],'instances_sha256':catalog_fence(snapshot['instances'])}
        return self.host.commit_catalog_source_transition(intent_id,remote_key,done,snapshot,recheck=lambda:self._guard(p,children=False))['result']
