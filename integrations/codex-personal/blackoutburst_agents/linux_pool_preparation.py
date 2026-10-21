"""Explicit operator preparation of one already-submitted Linux Pool job.

No dispatch, claim, Runtime, model or publication. Native qualification is an
explicit effect of prepare(), not of construction, status or an exact retry.
"""
from contextlib import closing, contextmanager
from copy import deepcopy
import fcntl
import os
from pathlib import Path
import re

from .development_authorization import DevelopmentAuthorization
from .development_client import DevelopmentBoardClient
from .development_pool import DevelopmentPoolDriver
from .development_pool_selection import REVISION, file_document
from .dev_journal import PrivateDirectory, DevelopmentJournal, decode
from .dev_manifest import (ACTIONS, Budgets, DevelopmentError, NamedTest,
    RepositoryFiles, canonical, check_content, digest, identity, opaque, relative_path, sha256)
from .dev_planner import _directory_pin
from .dev_planner_journal import PlannerBudgets
from .git_workspace import LocalGitWorkspaceController
from .linux_input_snapshot import _path
from .linux_planner_policy import (LinuxPlannerPolicy, _executable_pin,
    linux_source_pins, prepare_linux_planner_policy)
from .linux_test_backend import (SupervisedLinuxNamedPythonBackend,
    pin_linux_execution_plan, prepare_linux_backend, prepare_linux_execution_manifest)
from .linux_test_namespace import read_public
from .linux_test_protocol import linux_execution_backend_id
from .pool_journal import uuid_value
from .worker_pool import WorkerPool

MAX_DOCUMENT = 128 * 1024
SCHEMA = 'linux-pool-preparation-v1'


def fail(code='linux_pool_preparation_changed'):
    raise DevelopmentError(code)


class LinuxPoolJobPreparer:
    """Protected local handles and operator arguments, never Board instructions."""
    def __init__(self, directory, *, pool, driver, workspaces, driver_id='development'):
        if (type(pool) is not WorkerPool or type(driver) is not DevelopmentPoolDriver
                or type(workspaces) is not LocalGitWorkspaceController):
            fail('linux_pool_concrete_handles_required')
        self.directory = _path(os.fspath(directory))
        with PrivateDirectory(self.directory, create=True):pass
        self.pin = _directory_pin(self.directory)
        self.pool,self.driver,self.workspaces,self.driver_id = pool,driver,workspaces,opaque(driver_id)
        binding = pool.drivers.get(driver_id)
        if (binding is None or binding.revision != REVISION
                or getattr(binding.factory,'__self__',None) is not driver
                or workspaces.grant['owner_id'] != driver.selections.fixed['owner_id']):
            fail('linux_pool_driver_mismatch')
        self.fixed = deepcopy(driver.selections.fixed)
        self.grant,self.git_pins = deepcopy(workspaces.grant),deepcopy(workspaces.pins)
        self.connection = deepcopy(driver.selections.connection_pin)

    def _job(self, operation_id, project_id, ticket_id):
        uuid_value(operation_id)
        with self.pool.mutex:
            self.pool._ready();state=self.pool.journal.read()
            job=deepcopy(self.pool._job(state,operation_id))
            if (state['stopped'] or self.pool.admissions_blocked or job['state']!='queued'
                    or job['stop_requested'] or job['driver_id']!=self.driver_id
                    or job['driver_revision']!=REVISION
                    or job['payload']!={'project_id':project_id,'ticket_id':ticket_id}):
                fail('linux_pool_job_not_queued')
            return state['id'],job

    def _local(self, request):
        with PrivateDirectory(self.directory):pass
        if (_directory_pin(self.directory)!=self.pin or self.driver.selections.fixed!=self.fixed
                or self.workspaces.grant!=self.grant or self.workspaces.pins!=self.git_pins
                or self.driver.selections.connection_pin!=self.connection
                or file_document(self.driver.selections.connection_file)[1]!=self.connection):fail()
        self.workspaces._roots()
        pool_id,job=self._job(request['job']['id'],request['project_id'],request['ticket_id'])
        if pool_id!=request['pool_id'] or job!=request['job']:fail('linux_pool_job_changed')
        for name in ('executable','interpreter','unshare'):
            if _executable_pin(request[name])!=request['tools'][name]:fail()
        if linux_source_pins()!=request['sources']:fail()
        read_public(request['verifier_path'],maximum=65536,expected=request['verifier_pin'])

    @contextmanager
    def _locked(self, operation_id, *, create=False):
        uuid_value(operation_id)
        if _directory_pin(self.directory)!=self.pin:fail()
        fresh=False
        with PrivateDirectory(self.directory) as parent:
            if create:
                try:os.mkdir(operation_id,0o700,dir_fd=parent.fd);os.fsync(parent.fd);fresh=True
                except FileExistsError:pass
        with PrivateDirectory(Path(self.directory)/operation_id) as private:
            descriptor=None
            try:
                flags=os.O_RDWR|os.O_NOFOLLOW|(os.O_CREAT|os.O_EXCL if fresh else 0)
                descriptor=os.open('preparation.lock',flags,0o600,dir_fd=private.fd)
                private.validate_file(descriptor);pin=identity(os.fstat(descriptor))
                if fresh:os.fsync(descriptor);os.fsync(private.fd)
                try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:fail('linux_pool_preparation_busy')
                def held():
                    private.verify();private.validate_file(descriptor)
                    if (identity(os.fstat(descriptor))!=pin or identity(os.stat('preparation.lock',
                            dir_fd=private.fd,follow_symlinks=False))!=pin):fail()
                held();yield private,fresh,held
            except FileNotFoundError:fail('linux_pool_preparation_incomplete')
            finally:
                if descriptor is not None:os.close(descriptor)

    @staticmethod
    def _record(private, name, value):
        raw=canonical(value)
        if len(raw)>MAX_DOCUMENT:fail('linux_pool_preparation_limit')
        private.create(name,raw)

    @staticmethod
    def _read(private,name):
        raw=private.read(name,MAX_DOCUMENT);value=decode(raw)
        if canonical(value)!=raw:fail()
        return value

    def _files(self, operation_id):
        root=Path(self.directory)/operation_id
        return [root/name for name in ('selection.json','instruction.txt','scripts/verifier.py',
            'broker/manifest.json','broker/state.json','backend/execution-plan.json','backend/backend-manifest.json',
            'backend/qualification.json','policy/planner-plan.json','policy/planner-qualification.json')]+[
                Path(self.driver.selections.directory)/(operation_id+'.json')]

    def _complete(self, value, request_sha, operation_id, session_id):
        files={str(path) for path in self._files(operation_id)}
        if (type(value) is not dict or set(value)!={'schema','request_sha256','files','binding'}
                or value['schema']!=SCHEMA or value['request_sha256']!=request_sha
                or type(value['files']) is not dict or set(value['files'])!=files
                or type(value['binding']) is not dict):fail()
        binding=value['binding'];registry=str(Path(self.driver.selections.directory)/(operation_id+'.json'))
        if (set(binding)!={'operation_id','session_id','selection_sha256','status'}
                or binding['operation_id']!=operation_id or binding['session_id']!=session_id
                or binding['status']!='bound'):fail()
        sha256(binding['selection_sha256'])
        if value['files'][registry].get('sha256')!=binding['selection_sha256']:fail()
        return value

    def _authority(self, client, registration, request, *, expected_scope=None):
        try:
            manifest_scope={key:request['scope'][key] for key in ('owner_id','agent_id','project_id','ticket_id')}
            manifest_scope.update(session_id=request['job']['session_id'],
                device_id=registration['identity']['device_id'],profile_version=registration['profile_version'])
        except (KeyError,TypeError):fail('linux_pool_registration_changed')
        if expected_scope is not None and manifest_scope!=expected_scope:fail('linux_pool_registration_changed')
        floor={'scope':manifest_scope,'actions':request['actions'],'resource':request['resource']}
        return DevelopmentAuthorization(client,scope=request['scope'],manifest=floor,registration=registration,
            stop_requested=lambda:not self._available(request))

    def _available(self, request):
        # Authorization invokes this at each read bracket. Full source/tool/Git
        # validation belongs to _fresh and native guards, not a stop predicate.
        try:
            pool_id,job=self._job(request['job']['id'],request['project_id'],request['ticket_id'])
            return pool_id==request['pool_id'] and job==request['job']
        except Exception:return False

    @contextmanager
    def _registered(self, request, *, expected_scope=None):
        with closing(DevelopmentBoardClient.from_connection_file(self.driver.selections.connection_file,
                session_id=request['job']['session_id'],timeout=3)) as client:
            if (list(client.connection_identity)!=self.connection['identity']
                    or client.endpoint!=self.fixed['board_endpoint']
                    or client.session_id!=request['job']['session_id']):fail('linux_pool_registration_changed')
            yield self._authority(client,client.register_initial(),request,expected_scope=expected_scope)

    def _fresh(self, authority, request, baseline=None):
        self._local(request)
        observed=authority.snapshot()
        ticket=observed['ticket']
        if (ticket['status']!='todo' or ticket['claimant'] is not None
                or ticket.get('delegation') is not None or ticket.get('claim_expired') is True
                or ticket.get('resource')!=request['resource']):fail('linux_pool_ticket_not_ready')
        selected={'context_sha256':observed['context']['fingerprint'],
                  'policy_sha256':digest(canonical(observed['policy']))}
        if baseline is not None and selected!=baseline:fail('linux_pool_selection_changed')
        return selected

    def prepare(self, operation_id, *, project_id, ticket_id, paths, resource, actions,
                verifier_path, verifier_sha256, test_name, test_timeout_seconds, test_max_output_bytes,
                executable, cli_version, interpreter, unshare, instruction, model,
                planner_budgets, broker_budgets):
        """One explicit clone + native backend/policy qualification + binding."""
        if type(planner_budgets) is not PlannerBudgets or type(broker_budgets) is not Budgets:
            fail('linux_pool_explicit_budgets_required')
        project_id,ticket_id,resource,model=(opaque(value) for value in (project_id,ticket_id,resource,model))
        opaque(model,'invalid_development_model',128)
        if type(cli_version) is not str or re.fullmatch(r'codex-cli [0-9][A-Za-z0-9._+-]{0,79}',cli_version) is None:
            fail('linux_planner_cli_version_invalid')
        if (type(paths) not in (list,tuple) or not 1<=len(paths)<=128 or len(set(paths))!=len(paths)
                or type(actions) not in (list,tuple) or not {'repo.read_file','repo.run_test'}<=set(actions)
                or not set(actions)<=ACTIONS or len(set(actions))!=len(actions)):fail('linux_pool_inputs_invalid')
        for path in paths:relative_path(path)
        instruction=check_content(instruction.encode('utf-8','strict')) if type(instruction) is str else ''
        if not instruction.strip() or len(instruction.encode())>16384:fail('invalid_planner_instruction')
        verifier_path=_path(os.fspath(verifier_path));sha256(verifier_sha256)
        if not verifier_path.endswith('.py'):fail('linux_pool_verifier_invalid')
        verifier,verifier_pin=read_public(verifier_path,maximum=65536)
        if digest(verifier)!=verifier_sha256:fail('linux_pool_verifier_changed')
        check_content(verifier)
        test=NamedTest(test_name,'linux-preparation-placeholder',test_timeout_seconds,test_max_output_bytes)
        test.document()
        if test_timeout_seconds>60:fail('linux_pool_verifier_invalid')
        tools={name:_executable_pin(os.fspath(value)) for name,value in
            (('executable',executable),('interpreter',interpreter),('unshare',unshare))}
        pool_id,job=self._job(operation_id,project_id,ticket_id)
        request={'schema':SCHEMA,'pool_id':pool_id,'job':job,'scope':{**self.fixed,
            'project_id':project_id,'ticket_id':ticket_id,'board_session_id':job['session_id']},
            'project_id':project_id,'ticket_id':ticket_id,'grant':self.grant,'git_pins':self.git_pins,
            'resource':resource,'actions':sorted(actions),'paths':list(paths),'verifier_path':verifier_path,
            'verifier_pin':verifier_pin,'verifier_sha256':verifier_sha256,'test':test.document(),
            **{key:tools[key]['path'] for key in tools},'tools':tools,'cli_version':cli_version,
            'instruction':instruction,'model':model,'planner_budgets':planner_budgets.document(),
            'broker_budgets':broker_budgets.document(),'connection':self.connection,'sources':linux_source_pins()}
        raw=canonical(request)
        if len(raw)>MAX_DOCUMENT:fail('linux_pool_preparation_limit')
        request=decode(raw);request_sha=digest(raw)
        with self._locked(operation_id,create=True) as (private,fresh,held):
            if not fresh:
                intent=self._read(private,'intent.json')
                if intent!={'request':request,'request_sha256':request_sha}:fail('linux_pool_request_conflict')
                try:complete=self._read(private,'complete.json')
                except DevelopmentError:fail('linux_pool_preparation_incomplete')
                self._local(request);held()
                self._complete(complete,request_sha,operation_id,job['session_id'])
                for path,pin in complete['files'].items():
                    if file_document(path,512*1024)[1]!=pin:fail()
                return {**complete['binding'],'preparation_sha256':request_sha,'historical':True}
            self._record(private,'intent.json',{'request':request,'request_sha256':request_sha})
            stage='intent'
            try:
                self._local(request);held()
                self._record(private,'01-registration-intent.json',{'request_sha256':request_sha})
                with self._registered(request) as authority:
                    baseline=self._fresh(authority,request)
                    manifest_scope=authority.manifest['scope']
                    self._record(private,'registration.json',{'scope':manifest_scope,'baseline':baseline})
                    def authorize(given,action,wanted):
                        try:
                            held();self._fresh(authority,request,baseline)
                            return given==manifest_scope and action in request['actions'] and wanted==request['resource']
                        except Exception:return False
                    stage='git'
                    self._record(private,'02-git-intent.json',{'request_sha256':request_sha,'scope':manifest_scope})
                    self.workspaces.plan(operation_id=operation_id,scope=manifest_scope)
                    self.workspaces.materialize(operation_id)
                    inputs=self.workspaces.manifest_inputs(operation_id)
                    self._fresh(authority,request,baseline);held()
                    stage='resources'
                    self._record(private,'03-resources-intent.json',{'request_sha256':request_sha,'inputs':inputs})
                    scripts=private.path/'scripts';os.mkdir(scripts,0o700);os.fsync(private.fd)
                    if digest(verifier)!=request['verifier_sha256']:fail('linux_pool_verifier_changed')
                    with PrivateDirectory(scripts) as script_dir:script_dir.create('verifier.py',verifier)
                    planner=private.path/'planner';os.mkdir(planner,0o700);os.fsync(private.fd)
                    selected_test=request['test']
                    plan=pin_linux_execution_plan(root=inputs['root'],scripts_root=str(scripts),
                        scripts={selected_test['name']:{'path':'verifier.py','timeout_seconds':selected_test['timeout_seconds'],
                            'max_output_bytes':selected_test['max_output_bytes']}},interpreter=request['interpreter'],unshare=request['unshare'])
                    suite=NamedTest(selected_test['name'],linux_execution_backend_id(digest(canonical(plan))),
                        selected_test['timeout_seconds'],selected_test['max_output_bytes'])
                    broker,backend,policy=(private.path/name for name in ('broker','backend','policy'))
                    prepare_linux_execution_manifest(broker,execution_plan=plan,root=inputs['root'],
                        base_sha=inputs['base_sha'],scope=manifest_scope,paths=request['paths'],resource=request['resource'],
                        authorize_scope=authorize,tests=[suite],actions=request['actions'],budgets=Budgets(**request['broker_budgets']))
                    with closing(DevelopmentJournal(broker)) as journal:manifest=deepcopy(journal.manifest)
                    prepare_linux_backend(backend,manifest=manifest,plan=plan)
                    prepare_linux_planner_policy(policy,workspace=planner,checkout=inputs['root'],
                        executable=request['executable'],cli_version=request['cli_version'],interpreter=request['interpreter'])
                    stage='backend-qualification'
                    self._fresh(authority,request,baseline);held()
                    self._record(private,'04-backend-qualification-intent.json',{'plan_sha256':digest(canonical(plan))})
                    with closing(SupervisedLinuxNamedPythonBackend(backend,manifest=manifest,authorize_scope=authorize)) as concrete:
                        concrete.qualify()
                        if not concrete.qualified_for(digest(canonical(manifest)),suite.document()):fail('linux_pool_backend_unqualified')
                        environment=concrete.environment
                with self._registered(request,expected_scope=manifest_scope) as authority:
                    stage='policy-qualification'
                    self._fresh(authority,request,baseline);held()
                    self._record(private,'05-policy-qualification-intent.json',{'request_sha256':request_sha})
                    with LinuxPlannerPolicy(policy) as concrete:
                        concrete.qualify();concrete.validate_for(request['executable'],str(planner))
                        if concrete.environment!=environment:fail('linux_pool_environment_changed')
                stage='binding'
                held();self._local(request)
                self._record(private,'06-binding-intent.json',{'request_sha256':request_sha})
                with self._registered(request,expected_scope=manifest_scope) as final:
                    self._fresh(final,request,baseline)
                    if self.workspaces.manifest_inputs(operation_id)!=inputs:fail()
                    with closing(RepositoryFiles(manifest['root'],manifest['root_identity'])) as repository:
                        for path,pin in manifest['files'].items():
                            data,_=repository.read(path,manifest['budgets']['max_file_bytes'])
                            if digest(data)!=pin['sha256'] or len(data)!=pin['bytes']:fail('linux_pool_source_changed')
                    private.create('instruction.txt',request['instruction'].encode())
                    selection={'schema':1,'operation_id':job['id'],'worker_id':job['worker_id'],'session_id':job['session_id'],
                        'scope':request['scope'],'broker_directory':str(broker),'backend_directory':str(backend),
                        'planner_policy_directory':str(policy),'instruction_file':str(private.path/'instruction.txt'),
                        'model':request['model'],'budgets':request['planner_budgets']}
                    self._record(private,'selection.json',selection)
                    self._fresh(final,request,baseline);held()
                    # Dispatch and Stop share this short local metadata bracket.
                    # All registrations, RPC, Git and qualification are outside.
                    with self.pool.mutex:
                        current_pool,current_job=self._job(operation_id,request['project_id'],request['ticket_id'])
                        if current_pool!=pool_id or current_job!=job:fail('linux_pool_job_changed')
                        bound=self.driver.bind_selection(self.pool._public(job),private.path/'selection.json')
                complete={'schema':SCHEMA,'request_sha256':request_sha,
                    'files':{str(path):file_document(path,512*1024)[1] for path in self._files(operation_id)},'binding':bound}
                self._complete(complete,request_sha,operation_id,job['session_id'])
                self._record(private,'complete.json',complete);held()
                return {**bound,'preparation_sha256':request_sha,'historical':False}
            except Exception as error:
                reason=error.code if isinstance(error,DevelopmentError) else 'linux_pool_preparation_failed'
                try:self._record(private,'failed.json',{'request_sha256':request_sha,'stage':stage,'reason':reason})
                except Exception:pass
                raise

    def status(self, operation_id):
        """Historical metadata only; no constructor, grant, qualification or bind."""
        with self._locked(operation_id) as (private,_,held):
            intent=self._read(private,'intent.json');held()
            if (type(intent) is not dict or set(intent)!={'request','request_sha256'}
                    or digest(canonical(intent['request']))!=intent['request_sha256']
                    or intent['request']['job']['id']!=operation_id):fail()
            names=set(os.listdir(private.fd))
            result={'operation_id':operation_id,'preparation_sha256':intent['request_sha256'],
                    'status':'prepared' if 'complete.json' in names else 'needs_review'}
            if 'complete.json' in names:
                self._complete(self._read(private,'complete.json'),intent['request_sha256'],operation_id,intent['request']['job']['session_id'])
            if 'failed.json' in names:result['failure']=self._read(private,'failed.json')
            return result
