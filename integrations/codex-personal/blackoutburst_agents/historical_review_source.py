"""Host-only admission of one NEW review of an immutable published aggregate.

Historical source/test/Stop records are evidence, never an author permit. This
module never opens an author worker, planner, test backend or publication API.
The ordinary lifecycle/worker own the new claim, context, Runtime and opinion.
It deliberately is not part of either native/family execution source roster.
"""
from contextlib import ExitStack
from copy import deepcopy
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote

from .client_lifecycle import own_client
from .context import ContextScope, build_context, _profile
from .delivery_review_material import render_review_material, validate_review_evidence
from .development_pool_delivery import _historical_operation
from .development_pool_selection import file_document, job_identity
from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, canonical, digest,sha256
from .dev_planner import _directory_pin
from .git_delivery import LocalGitDeliveryController
from .git_publication import GitHubDraftPublisher, GitHubPublicationGrant, HOST, API, REMOTE, sha
from .git_workspace import LocalGitWorkspaceController, LocalRepositoryGrant
from .pool_journal import PoolJournal,uuid_value
from .review_lifecycle_client import DeliveryReviewLifecycleClient
from .review_worker import ReviewRuntimeGuard, _agent_id
from .runtime_client import RuntimeBoardClient
from .runtime_journal import RuntimeJournal

MAX_RECORD=8*1024*1024
KIND='historical-aggregate-review-source-v1'


def require(value,code='historical_review_source_changed'):
    if not value:raise DevelopmentError(code)


def document(path,maximum=MAX_RECORD):
    raw,pin=file_document(path,maximum);value=decode(raw)
    require(canonical(value)==raw)
    return value,pin


def rows(path,query,parameters=()):
    """Existing DELETE-mode databases only; no constructors or migrations."""
    raw,pin=file_document(path,32*1024*1024)
    require(raw[:16]==b'SQLite format 3\0')
    require(not any(os.path.lexists(str(path)+suffix) for suffix in ('-wal','-shm','-journal')),
        'historical_review_writer_pending')
    db=sqlite3.connect(Path(path).as_uri()+'?mode=ro&immutable=1',uri=True,timeout=0)
    try:
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
        result=[dict(row) for row in db.execute(query,parameters).fetchmany(3)]
        require(len(result)<=1,'historical_review_ambiguous')
    finally:db.close()
    require(file_document(path,32*1024*1024)[1]==pin)
    return result,pin


class HistoricalAggregateReadView:
    """Read the exact published head while the target branch may advance.

    Receipt/material base_sha remains the immutable sealed base, never the
    current branch tip or evidence that a merge into that tip was tested.
    """
    def __init_subclass__(cls,**kwargs):raise TypeError('Closed historical read view')

    def __init__(self,source,publication_root,gh,interpreter):
        require(type(source) is HistoricalAggregateReviewSource)
        self.source=source;self.root=Path(publication_root)
        self.git,self.gh,self.python=source.workspaces.git,Path(gh),Path(interpreter)
        from . import git_publication
        self.guardian=Path(git_publication.__file__).with_name('git_publication_guardian.py')
        self.deliveries,self.workspaces=source.deliveries,source.workspaces
        self.grant=source.grant;self._grant=GitHubPublicationGrant(**self.grant)
        self.timeout=30;self.pins=self._pins()
        value,_=document(self.root/source.operation_id/'publication.json',65536)
        self.authority_pin=deepcopy(value['request']['authority'])
        self.request=deepcopy(value['request']);self.publication=deepcopy(value)
        self._roots()

    _pins=GitHubDraftPublisher._pins
    _tool=staticmethod(GitHubDraftPublisher._tool)
    _environment=GitHubDraftPublisher._environment
    _api=GitHubDraftPublisher._api
    _auth=GitHubDraftPublisher._auth
    _read=GitHubDraftPublisher._read
    _transport=GitHubDraftPublisher._transport
    _body=staticmethod(GitHubDraftPublisher._body)
    _lookup_pr=GitHubDraftPublisher._lookup_pr

    def _remote(self,private,value):
        path=self._transport(private,value)
        base='refs/heads/'+self.grant['base_branch'];head='refs/heads/'+value['request']['branch']
        code,out,_=self._command([str(self.git),'ls-remote','--refs',REMOTE,base,head],cwd=path,git=True)
        require(code==0 and out,'git_publication_remote_unavailable')
        seen={}
        try:
            for line in out.decode('ascii','strict').splitlines():
                oid,ref=line.split('\t')
                require(ref in (base,head) and ref not in seen,'git_publication_remote_invalid')
                seen[ref]=sha(oid)
        except (ValueError,UnicodeError):raise DevelopmentError('git_publication_remote_invalid') from None
        require(base in seen,'git_publication_remote_invalid')
        require(seen.get(head)==value['request']['delivery_sha'],'git_publication_branch_unconfirmed')
        return seen[head]

    def _pr(self,value,row):
        # The observed base tip may differ from ls-remote or the sealed base.
        # Normalize only a private copy for the existing strict identity checks;
        # neither that observation nor the copy updates any historical receipt.
        require(type(row) is dict and type(row.get('base')) is dict,'git_publication_pr_mismatch')
        sha(row['base'].get('sha'))
        historical=deepcopy(row);historical['base']['sha']=self.grant['base_commit']
        return GitHubDraftPublisher._pr(self,value,historical)

    def _roots(self):
        self.source._local()
        require(self.pins==self._pins() and self.grant==self._grant.document())

    def _confirm_launch(self,argv,*,cwd,git):
        directory=self.root/self.source.operation_id
        query=API+'/pulls?state=all&per_page=100&head='+quote('Blackoutburst:'+self.request['branch'],safe='')+'&base='+quote(self.grant['base_branch'],safe='')
        allowed=([str(self.git),'ls-remote','--refs',REMOTE,'refs/heads/'+self.grant['base_branch'],
            'refs/heads/'+self.request['branch']] if git else None)
        commands=[[str(self.gh),'auth','status','--hostname',HOST]]+[
            [str(self.gh),'api','--hostname',HOST,'--method','GET',path] for path in ('user',API,query)]
        require(type(git) is bool and (argv==allowed and Path(cwd)==directory/'transport' if git
            else argv in commands and Path(cwd)==directory),'historical_review_command_refused')

    def _command(self,argv,*,cwd,git=False):
        self._confirm_launch(argv,cwd=cwd,git=git)
        return GitHubDraftPublisher._command(self,argv,cwd=cwd,git=git)

    def publish(self,*args,**kwargs):raise DevelopmentError('git_publication_readonly_view')
    def _authorize_effect(self,*args,**kwargs):raise DevelopmentError('git_publication_readonly_view')


class HistoricalAggregateReviewSource:
    """Protected artifact reader plus a separately recorded fresh admission.

    Construction performs bounded public Git/Runtime reads, registers the NEW
    selected reviewer session and records its current Board context. A pure
    plan must not construct this class. It never claims work or starts a model.

Expected source hash/head and all paths are selected by the trusted host
operator. They grant no Board or model authority. The new job must be queued;
old Runtime is either actually finished or provably never admitted after an
attributed transactional claim revocation. No old record is repaired/adopted.
"""
    def __init_subclass__(cls,**kwargs):raise TypeError('Closed historical review source')
    def __reduce_ex__(self,protocol):raise TypeError('Live historical source cannot be serialized')
    def __copy__(self):raise TypeError('Live historical source cannot be copied')
    def __deepcopy__(self,memo):raise TypeError('Live historical source cannot be copied')

    def __init__(self,directory,*,prior_review_directory,prior_job_id,source_directory,
                 publication_root,workspace_state_root,git_executable,expected_source_sha256,expected_delivery_sha,
                 expected_ticket_version,revocation_event_id,job,connection_file,owner_id,agent_id,project_id,ticket_id,board_endpoint,
                 gh_executable,interpreter):
        self.directory=Path(directory);self.prior=Path(prior_review_directory)
        require(not os.path.lexists(self.directory),'historical_review_fresh_admission_required')
        self.prior_job=uuid_value(prior_job_id)
        self.new_job=job_identity(job)
        require(job.get('state')=='queued' and job.get('driver_id')=='review'
            and job.get('driver_revision')=='delivery-review-pool-v1'
            and ('payload' not in job or job['payload']=={'project_id':project_id,'ticket_id':ticket_id}))
        require(type(expected_ticket_version) is int and 1<=expected_ticket_version<2**63)
        self.expected_ticket_version=expected_ticket_version
        require(type(revocation_event_id) is int and 1<=revocation_event_id<2**63)
        self.revocation_event_id=revocation_event_id
        self.connection_file=str(Path(connection_file));_,self.connection_pin=file_document(self.connection_file)
        self.scope=dict(owner_id=owner_id,agent_id=agent_id,project_id=project_id,ticket_id=ticket_id,
            board_endpoint=board_endpoint,board_session_id=self.new_job['session_id'])
        source_path=Path(source_directory)/'source.json'
        source,self.source_pin=document(source_path)
        require(self.source_pin['sha256']==expected_source_sha256)
        self.receipt=deepcopy(source['receipt']);self.operation_id=uuid_value(self.receipt['operation_id'])
        self.original_frame_sha256=sha256(source['frame_sha256'])
        require(self.receipt['schema']=='published-aggregate-review-source-v1'
            and self.receipt['status']=='complete' and self.receipt['accepted'] is False
            and self.receipt['delivery_sha']==expected_delivery_sha
            and self.receipt['author_scope']['board_endpoint']==board_endpoint
            and all(self.receipt['author_scope'][k]==self.scope[k] for k in ('owner_id','project_id','ticket_id'))
            and self.receipt['author_scope']['agent_id']!=agent_id)
        self.old,self.old_pin=document(self.prior/'selections'/(prior_job_id+'.json'),128*1024)
        selected=self.old['selection'];old_job=selected['operation_id']
        require(old_job==prior_job_id and selected['source_operation_id']==self.operation_id
            and self.old['source']['receipt']==self.receipt and self.old['source']['directory']==str(source_directory)
            and self.old['source']['kind']=='published-aggregate-review-source-v1'
            and self.new_job['session_id']!=selected['session_id'] and self.new_job['id']!=old_job
            and all(selected['scope'][k]==self.scope[k] for k in ('owner_id','project_id','ticket_id','board_endpoint')))
        self.old_scope=deepcopy(selected['scope'])
        grant=self.old['source']['local_grant'];git_pins=self.old['source']['git_pins']
        # The host selects the original state path. Its physical identity and
        # every Git/root pin must equal the already recorded source descriptor.
        aggregate=Path(source_directory).parent
        forbidden=(self.prior,aggregate,Path(publication_root),Path(grant['repository']),
            Path(grant['workspace_root']),Path(workspace_state_root))
        require(self.directory.is_absolute() and not any(self.directory.is_relative_to(p) or p.is_relative_to(self.directory)
            for p in forbidden),'historical_review_roots_overlap')
        self.workspaces=LocalGitWorkspaceController(workspace_state_root,
            grant=LocalRepositoryGrant(**grant),git_executable=git_executable)
        require(self.workspaces.pins==git_pins)
        self.paths={name:aggregate/name for name in ('deliveries','reviews')}
        self.grant=deepcopy(self.old['source']['grant'])
        require(self.grant==GitHubPublicationGrant(**self.grant).document()
            and self.grant['owner_id']==owner_id and self.grant['base_commit']==self.receipt['original_public_base'])
        self.aggregate,self.aggregate_pin=document(aggregate/'aggregate.json')
        self.delivery,self.delivery_pin=document(self.paths['deliveries']/self.operation_id/'delivery.json')
        self.workspace,self.workspace_pin=document(self.workspaces.state_root/self.operation_id/'workspace.json')
        self.deliveries=LocalGitDeliveryController(self.workspaces,self.paths['deliveries'],self.paths['reviews'],
            test_guard=lambda **_:False)
        self.pins={'source':self.source_pin,'aggregate':self.aggregate_pin,'delivery':self.delivery_pin,
            'workspace':self.workspace_pin,'old_selection':self.old_pin}
        ledger,ledger_pin=document(Path(source_directory)/'continuation-review.json',128*1024)
        require(ledger['phase']=='running' and ledger['result'] is None
            and ledger['request']['source_operation_id']==self.operation_id
            and ledger['request']['directory']==str(self.prior)
            and ledger['job']['id']==prior_job_id and ledger['job']['session_id']==self.old_scope['board_session_id'])
        self.pins['old_ledger']=ledger_pin
        self._local()
        self.reader=HistoricalAggregateReadView(self,publication_root,gh_executable,interpreter)
        self._public()
        require(self.material()==self.old['material'],'historical_review_material_changed')
        # Cleanup inspection precedes registration, claim and any new turn.
        self.previous=self._previous()
        self.registration,self.context,self.claim_gate=self._admission()
        with PrivateDirectory(self.directory,create=True) as private:
            self.admission={'schema':'historical-aggregate-review-admission-v1','job':self.new_job,
                'scope':self.scope,'profile_version':self.registration['profile']['version'],
                'device_id':self.registration['identity']['device_id'],
                'ticket_version':self.context['sources']['ticket']['version'],
                'context_sha256':self.context['fingerprint'],'source_sha256':self.source_pin['sha256'],
                'original_frame_sha256':self.original_frame_sha256,'delivery_sha':self.receipt['delivery_sha'],
                'previous':self.previous,'claim_gate':self.claim_gate,'pins':deepcopy(self.pins)}
            private.create('admission.json',canonical(self.admission))
        self.admission_pin=file_document(self.directory/'admission.json')[1]

    def _local(self):
        for pin in getattr(self,'pins',{}).values():require(file_document(pin['path'],32*1024*1024)[1]==pin)
        for pin in getattr(self,'previous',{}).get('files',[]):
            require(file_document(pin['path'],32*1024*1024)[1]==pin)
        receipt=self.receipt;a=self.aggregate;d=self.delivery
        recorded=document(self.source_pin['path'])[0]
        require(recorded=={'receipt':receipt,'frame_sha256':self.original_frame_sha256})
        require(a['phase']=='complete' and a['capture']==d['snapshot'] and a['seal']==d['seal']
            and a['request']['original_grant']==self.workspaces.grant
            and a['request']['operation_id']==self.operation_id
            and a['completion']==receipt['completion'] and d['phase']=='sealed'
            and digest(canonical(a['capture']))==receipt['snapshot_sha256']
            and digest(canonical(a['seal']['test_evidence']))==receipt['test_evidence_sha256']
            and all(a['seal'][k]==receipt[k] for k in ('delivery_sha','tree_sha','snapshot_sha256'))
            and a['seal']['base_commit']==receipt['original_public_base']
            and all(d['request']['scope'][k]==receipt['author_scope'][k]
                for k in ('owner_id','agent_id','project_id','ticket_id'))
            and d['request']['scope']['session_id']==receipt['author_scope']['board_session_id'])
        validate_review_evidence(a['seal']['test_evidence'])
        with PrivateDirectory(self.paths['deliveries']/self.operation_id) as private:
            self.deliveries._validate(private,d,self.workspace,Path(d['request']['checkout']))
            self.deliveries._commit_proof(Path(d['request']['checkout']),d)
        old_review=self.old['review']
        with PrivateDirectory(self.paths['reviews']/self.prior_job) as private:
            require(document(private.path/'review.json')[0]==old_review)
            self.deliveries._review_validate(private,old_review,old_review['request'],d)
        require(old_review['request']['operation_id']==self.operation_id
            and old_review['request']['scope']==self.old_scope
            and old_review['request']['selection']['delivery_sha']==receipt['delivery_sha']
            and old_review['request']['selection']['material_sha256']==digest(self.old['material'].encode()))
        if hasattr(self,'admission_pin'):
            require(file_document(self.directory/'admission.json')[1]==self.admission_pin
                and document(self.directory/'admission.json')[0]==self.admission)

    def _public(self):
        reader=self.reader;reader._roots()
        with PrivateDirectory(reader.root/self.operation_id) as private:
            publication=reader._read(private)
            require(publication==reader.publication and publication['phase']=='complete'
                and publication['request_sha256']==self.receipt['publication_request_sha256']
                and publication['request']['delivery_sha']==self.receipt['delivery_sha']
                and publication['request']['tree_sha']==self.receipt['tree_sha'])
            reader._auth(private.path,expected_login=publication['login'])
            require(reader._remote(private,publication)==self.receipt['delivery_sha'])
            require(reader._lookup_pr(private,publication)==publication['pr']==self.receipt['pull_request'])
            reader._transport(private,publication)
        self._local()

    def _old_control(self,record):
        selected=self.old['selection']
        require(file_document(self.old['connection']['path'])[1]==self.old['connection'])
        with ExitStack() as resources:
            client=own_client(resources,DeliveryReviewLifecycleClient.from_connection_file(
                self.old['connection']['path'],session_id=selected['session_id'],timeout=3))
            require(client.endpoint==self.old_scope['board_endpoint'])
            control=own_client(resources,RuntimeBoardClient.from_worker(client))
            return control.call('lookup_execution',{'idempotency_key':record['key']})

    def _previous(self):
        state,pool_pin=document(self.prior/'pool/pool-state.json',512*1024);PoolJournal.validate(state)
        require(len(state['jobs'])==1,'historical_review_ambiguous');job=state['jobs'][0]
        require(job['id']==self.prior_job and job['session_id']==self.old_scope['board_session_id']
            and job['driver_revision']=='delivery-review-pool-v1'
            and job['payload']=={k:self.scope[k] for k in ('project_id','ticket_id')}
            and job['state'] in ('settled','retained') and job['execution_active'] is not True,
            'historical_review_previous_unresolved')
        base=self.prior/'pool/workers'/job['worker_id']
        op=_historical_operation(base/'work',_directory_pin(base/'work'),job['id'])
        require(op['stage']=='needs_review' and op['scope']==self.old_scope)
        works,wp=rows(base/'runs/runs.sqlite3','SELECT * FROM works LIMIT 2')
        turns,tp=rows(base/'runs/runs.sqlite3','SELECT * FROM turns LIMIT 2')
        runtime,rp=rows(base/'runtime/runtime.sqlite3','SELECT * FROM executions LIMIT 2')
        require(len(works)==len(turns)==len(runtime)==1,'historical_review_previous_unresolved')
        work,turn,record=works[0],turns[0],RuntimeJournal.decode(runtime[0])
        self.previous_runtime=deepcopy(record)
        selection=op['configuration']['selection'];result=decode(turn['result_json'].encode())
        require(work['id']==job['id'] and work['agent_id']==_agent_id(self.old_scope,selection,job['id'])
            and all(work[k]==self.scope[k] for k in ('owner_id','project_id','ticket_id'))
            and turn['work_id']==job['id'] and turn['request_id']=='delivery-review-'+job['id']
            and turn['status'] in ('completed','blocked','cancelled')
            and record['configuration']==RuntimeJournal.configuration(self.old_scope,job['id'],turn['id'],work['model'])
            and record['key']==RuntimeJournal.key(record['configuration']))
        stop,stop_pin=document(self.prior/'lifecycle'/job['id']/'stop.json')
        require(stop=={'operation_id':job['id'],'stop':True}
            and not any(os.path.lexists(self.prior/'lifecycle'/job['id']/(n+'.json'))
                for n in ('publication-intent','published')),'historical_review_previous_publication_unknown')
        found=self._old_control(record)
        if record['phase']=='finished':
            guard=ReviewRuntimeGuard(None,None,self.old_scope,selection,job['id']);guard.operation=record
            require(job['state']=='settled' and job['cleanup_confirmed'] is True
                and job['execution_active'] is False and result.get('cleanup_failed') is not True
                and result.get('verification',{}).get('cleanup_confirmed') is True
                and found.get('found') is True and guard.metadata(found.get('execution'))==record['receipt']
                and record['receipt']['state']=='finished' and record['receipt']['cleanup_confirmed'] is True,
                'historical_review_previous_unresolved')
            kind='finished'
        else:
            require(record['phase']=='needs_review' and record['receipt'] is None and not record['start_confirmed']
                and job['stop_requested'] is True
                and turn['pid'] is None and turn['provider_session_id'] is None and work['provider_session_id'] is None
                and result=={'status':'blocked','reason':'runtime_start_requires_review','text':'','uncertain':True,
                    'verification':{'cleanup_confirmed':True}} and found=={'found':False},
                'historical_review_previous_unresolved')
            kind='retired_before_transport'
        return {'kind':kind,'operation_id':job['id'],'session_id':job['session_id'],
            'runtime_key':record['key'],'runtime_pin':rp,'runs_pin':wp,'worker_sha256':digest(canonical(op)),
            'pool_sha256':digest(canonical(state)),
            'files':[rp,wp,pool_pin,stop_pin,file_document(base/'work/work-journal.sqlite3',32*1024*1024)[1]]}

    def _admission(self):
        with ExitStack() as resources:
            client=own_client(resources,DeliveryReviewLifecycleClient.from_connection_file(
                self.connection_file,session_id=self.scope['board_session_id'],timeout=3))
            require(client.endpoint==self.scope['board_endpoint'])
            client.set_effect_guard(self._local);registration=client.register_initial(skills=['review'])
            _profile(registration,ContextScope(**{k:self.scope[k] for k in ('owner_id','agent_id','project_id','ticket_id')}))
            require(registration['identity']['id']==self.scope['agent_id'])
            policy=client.call('get_work',{'project_id':self.scope['project_id'],'limit':1})['policy']
            require(all(policy.get(k) is True for k in ('enabled','allow_read','allow_requests')),
                'historical_review_permission_denied')
            ticket=client.call('get_ticket',{'ticket_id':self.scope['ticket_id']})['ticket']
            require(ticket['status']=='review' and ticket['claimant'] is None
                and ticket['version']==self.expected_ticket_version
                and not ticket.get('blocked_by') and not ticket.get('waiting_on_human')
                and ticket.get('assignee_agent_id') in (None,self.scope['agent_id']),
                'historical_review_ticket_changed')
            gate=self._gate(client,ticket)
            context=build_context(client.call,scope=ContextScope(**{k:self.scope[k] for k in ('owner_id','agent_id','project_id','ticket_id')}),
                registration=registration)
            return registration,context,gate

    def _gate(self,client,ticket):
        require(ticket['version']==self.expected_ticket_version and ticket['claimant'] is None,
            'historical_review_ticket_changed')
        claimed,_=document(self.prior/'lifecycle'/self.prior_job/'claimed.json')
        require(claimed['version']+1==ticket['version']
            and claimed['claimant']['session_id']==self.old_scope['board_session_id'],
            'historical_review_revocation_required')
        events=client.call('list_activity',{'ticket_id':self.scope['ticket_id'],
            'after':self.revocation_event_id-1,'limit':1,'order':'asc'})['items']
        require(len(events)==1,'historical_review_revocation_required')
        event=events[0];data=event.get('detail',{});actor=event.get('actor',{})
        require(event.get('id')==self.revocation_event_id and event.get('event')=='managed_agent_claim_released'
            and event.get('ticket_id')==self.scope['ticket_id'] and event.get('project_id')==self.scope['project_id']
            and data.get('source_session_id')==self.old_scope['board_session_id']
            and data.get('target_profile_id')==self.old_scope['agent_id']
            and data.get('source_profile_version')==self.old['selection']['registration']['profile']['version']
            and data.get('expected_version')==claimed['version'] and data.get('version')==ticket['version']
            and actor.get('kind')=='agent' and actor.get('owner_id')==self.scope['owner_id']
            and actor.get('agent_id')==data.get('manager_profile_id'),'historical_review_revocation_required')
        # Lookup follows the transactionally issued claim revocation and the
        # fresh unclaimed ticket read. The old session can no longer reserve.
        if self.previous['kind']=='retired_before_transport':
            require(self._old_control(self.previous_runtime)=={'found':False},
                'historical_review_previous_unresolved')
        return {'event_id':event['id'],'ticket_version':ticket['version'],
            'manager_agent_id':actor['agent_id'],'source_session_id':data['source_session_id']}

    def admission_version(self,client,scope):
        require(scope==self.scope and file_document(self.connection_file)[1]==self.connection_pin)
        context=build_context(client.call,scope=ContextScope(**{k:scope[k] for k in ('owner_id','agent_id','project_id','ticket_id')}),
            registration=client.register())
        require(context['fingerprint']==self.admission['context_sha256'],'historical_review_context_changed')
        require(self._previous()==self.previous,
            'historical_review_previous_changed')
        require(self._gate(client,client.call('get_ticket',{'ticket_id':scope['ticket_id']})['ticket'])==self.claim_gate)
        return self.admission['ticket_version']

    def status(self,operation_id):
        require(operation_id==self.operation_id);self._local();self._public()
        return deepcopy(self.receipt)

    def descriptor(self,operation_id):
        return {'kind':KIND,'directory':str(self.directory),'pins':deepcopy(self.pins),
            'local_grant':deepcopy(self.workspaces.grant),'git_pins':deepcopy(self.workspaces.pins),
            'grant':deepcopy(self.grant),'operation_id':operation_id,'receipt':self.status(operation_id),
            'admission':deepcopy(self.admission),'admission_pin':deepcopy(self.admission_pin)}

    def verify_tests(self,*,operation_id,scope,snapshot,evidence,expected_receipt):
        self._local()
        require(operation_id==self.operation_id and expected_receipt==self.receipt
            and snapshot==self.aggregate['capture'] and scope==self.aggregate['capture']['request']['scope']
            and evidence==self.aggregate['seal']['test_evidence'])
        return True

    def material(self):
        changes=self.aggregate['capture']['request']['changes'];files=[]
        entries={r['path']:r for r in self.aggregate['capture']['files']}
        for change in changes:
            entry=entries[change['path']]
            after=file_document(self.paths['deliveries']/self.operation_id/entry['payload'],16*1024*1024)[0]
            before=self.workspaces._git(['show',self.receipt['original_public_base']+':'+change['path']],cwd=self.workspaces.repository)
            require(digest(after)==entry['sha256'])
            files.append(dict(path=change['path'],mode=entry['mode'],before=before,after=after))
        return render_review_material(operation_id=self.operation_id,base_commit=self.receipt['original_public_base'],
            delivery_sha=self.receipt['delivery_sha'],tree_sha=self.receipt['tree_sha'],snapshot_sha256=self.receipt['snapshot_sha256'],
            test_evidence=self.aggregate['seal']['test_evidence'],files=files,pull_request=self.receipt['pull_request'])
