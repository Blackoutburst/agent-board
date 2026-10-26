"""Explicit nine-case Linux test-backend qualification, never a model or account.

Execute only from externally pinned launcher/held qualifier bytes after review.
All fixtures/private receipts are retained; no recursive cleanup or activation.
This recipe does not qualify planner CLI permissions or an owner subscription.
"""
import argparse
import hashlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys

ALLOWED_SOURCES = ('__init__.py', 'board_reader.py', 'board_worker.py', 'client_lifecycle.py', 'cloud_workflow.py', 'codex_turn.py', 'context.py', 'context_cache.py', 'decision_memory.py', 'dev_broker.py', 'dev_fence.py', 'dev_journal.py', 'dev_manifest.py', 'dev_planner.py', 'dev_planner_journal.py', 'dev_runtime.py', 'dev_test_backend.py', 'dev_test_supervisor.py', 'development_authorization.py', 'development_cleanup.py', 'development_cli.py', 'development_client.py', 'development_delivery.py', 'development_frame.py', 'development_git_authority.py', 'development_pool.py', 'development_pool_delivery.py', 'development_pool_selection.py', 'development_review_handoff.py', 'development_worker.py', 'event_journal.py', 'family_audit.py', 'family_cli.py', 'family_client.py', 'family_journal.py', 'family_plan.py', 'family_sources.py', 'family_worker.py', 'finite_pool_batch.py', 'git_delivery.py', 'git_publication.py', 'git_publication_guardian.py', 'git_workspace.py', 'host_config.py', 'linux_execution_qualification.py', 'linux_execution_state.py', 'linux_input_admission.py', 'linux_input_plan.py', 'linux_input_snapshot.py', 'linux_input_state.py', 'linux_planner_policy.py', 'linux_pool_preparation.py', 'linux_review_binding.py', 'linux_test_backend.py', 'linux_test_namespace.py', 'linux_test_protocol.py', 'linux_test_supervisor.py', 'message_abandon_protocol.py', 'message_admission_seal.py', 'message_config.py', 'message_journal.py', 'message_scope.py', 'model_policy.py', 'persistent_transport.py', 'planner_policy.py', 'pool_journal.py', 'protected_public_pins.py', 'review_client.py', 'review_lifecycle.py', 'review_lifecycle_client.py', 'review_pool.py', 'review_worker.py', 'run_store.py', 'runner.py', 'runtime_admission.py', 'runtime_client.py', 'runtime_guard.py', 'runtime_journal.py', 'subscription_probe.py', 'team_role_contract.py', 'team_role_pool.py', 'team_writer_source.py', 'work_journal.py', 'worker_client.py', 'worker_pool.py')
MAX_FILE=256*1024
MAX_TOTAL=4*1024*1024


class Refused(Exception):
    def __init__(self,code):self.code=code;super().__init__(code)


def require(condition,code):
    if not condition:raise Refused(code)


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
def sha(data):return hashlib.sha256(data).hexdigest()


def directory(path):
    require(type(path) is str and path.startswith('/') and str(Path(path))==path
            and all(part not in ('','.','..') for part in path.split('/')[1:]),'public_path_invalid')
    descriptor=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in Path(path).parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            os.close(descriptor);descriptor=child
        result=descriptor;descriptor=None;return result
    finally:
        if descriptor is not None:os.close(descriptor)


def held_at(parent,name,maximum):
    require(type(name) is str and '/' not in name and name not in ('','.','..'),'public_path_invalid')
    descriptor=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
    try:
        before=os.fstat(descriptor)
        def pin(info):return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_uid,info.st_mode,info.st_nlink)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_uid in (0,os.geteuid())
                and not before.st_mode&0o022 and 0<=before.st_size<=maximum,'public_file_invalid')
        data=b''
        while len(data)<=maximum:
            part=os.read(descriptor,min(65536,maximum+1-len(data)))
            if not part:break
            data+=part
        require(len(data)<=maximum and pin(os.fstat(descriptor))==pin(before)
                and pin(os.stat(name,dir_fd=parent,follow_symlinks=False))==pin(before),'public_file_changed')
        return data
    finally:os.close(descriptor)


def held_path(path,maximum):
    parent=directory(str(Path(path).parent))
    try:return held_at(parent,Path(path).name,maximum)
    finally:os.close(parent)


def decode(data):
    def unique(pairs):
        value={}
        for key,item in pairs:
            require(key not in value,'duplicate_inventory_key');value[key]=item
        return value
    try:return json.loads(data.decode('utf-8'),object_pairs_hook=unique,
        parse_constant=lambda _:(_ for _ in ()).throw(Refused('inventory_invalid')))
    except (UnicodeError,ValueError,RecursionError):raise Refused('inventory_invalid') from None


class HeldPackage(importlib.abc.MetaPathFinder,importlib.abc.Loader):
    def __init__(self,package,sources):self.package,self.sources=package,sources
    def find_spec(self,fullname,path=None,target=None):
        if fullname=='blackoutburst_agents':return importlib.util.spec_from_loader(fullname,self,is_package=True)
        if fullname.startswith('blackoutburst_agents.'):
            name=fullname.removeprefix('blackoutburst_agents.')
            require('.' not in name and name+'.py' in self.sources,'source_module_not_pinned')
            return importlib.util.spec_from_loader(fullname,self)
        return None
    def create_module(self,spec):return None
    def exec_module(self,module):
        filename='__init__.py' if module.__name__=='blackoutburst_agents' else module.__name__.split('.')[-1]+'.py'
        module.__file__=self.package+'/'+filename
        if filename=='__init__.py':module.__path__=[self.package]
        exec(compile(self.sources[filename],module.__file__,'exec',dont_inherit=True),module.__dict__)


def load_sources(package,inventory_path,expected):
    raw=held_path(inventory_path,64*1024)
    require(sha(raw)==expected,'source_inventory_pin_mismatch')
    inventory=decode(raw)
    require(type(inventory) is dict and set(inventory)=={'schema','files'}
            and inventory['schema']=='blackoutburst-linux-policy-source-inventory-v1'
            and type(inventory['files']) is dict and set(inventory['files'])==set(ALLOWED_SOURCES)
            and canonical(inventory)==raw,'source_inventory_invalid')
    root=directory(package)
    try:
        sources,total={},0
        for name in ALLOWED_SOURCES:
            value=inventory['files'][name]
            require(type(value) is dict and set(value)=={'sha256','bytes'}
                    and type(value['bytes']) is int and 0<=value['bytes']<=MAX_FILE
                    and type(value['sha256']) is str and re.fullmatch('[0-9a-f]{64}',value['sha256']) is not None,
                    'source_inventory_invalid')
            data=held_at(root,name,min(MAX_FILE,MAX_TOTAL-total));total+=len(data)
            require(len(data)==value['bytes'] and sha(data)==value['sha256'],'source_pin_mismatch')
            sources[name]=data
        return inventory,sources
    finally:os.close(root)


def parse_arguments(argv):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-synthetic-backend-matrix',action='store_true',required=True)
    for name in ('package','source-inventory','expected-source-inventory-sha256','interpreter',
                 'expected-interpreter-sha256','unshare','expected-unshare-sha256','expected-node',
                 'expected-release','expected-uid','root','output'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args(argv)
    for name in ('expected_source_inventory_sha256','expected_interpreter_sha256','expected_unshare_sha256'):
        require(re.fullmatch('[0-9a-f]{64}',getattr(args,name)) is not None,'explicit_source_toolchain_pins_required')
    require(re.fullmatch('[1-9][0-9]{0,9}',args.expected_uid) is not None,'unprivileged_expected_uid_required')
    return args


def real_guard(args):
    require(type(globals().get('VERIFIED_RUNNER_SHA256')) is str
            and re.fullmatch('[0-9a-f]{64}',VERIFIED_RUNNER_SHA256) is not None,'held_runner_launcher_required')
    require(sys.flags.isolated==1 and sys.flags.no_site==1 and sys.dont_write_bytecode,'isolated_python_required')
    require(sys.gettrace() is None and sys.getprofile() is None
            and not any(name=='pytest' or name.startswith('unittest.mock') or name.startswith('blackoutburst_agents')
                        for name in sys.modules),'uninstrumented_fresh_process_required')
    observed=os.uname()
    require(sys.platform=='linux' and observed.sysname=='Linux' and observed.machine=='x86_64','real_linux_x86_64_required')
    require(os.getuid()==os.geteuid()==int(args.expected_uid) and os.getgid()==os.getegid()!=0,
            'unprivileged_expected_uid_required')
    require(observed.nodename==args.expected_node and observed.release==args.expected_release,'host_identity_changed')
    require(sys.executable==args.interpreter and sys.version_info>=(3,10),'explicit_python_required')


def write_report(path,value):
    data=canonical(value)+b'\n';require(len(data)<=16384,'report_bound_exceeded')
    parent=directory(str(Path(path).parent));descriptor=None
    try:
        descriptor=os.open(Path(path).name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        view=memoryview(data)
        while view:
            count=os.write(descriptor,view);require(count>0,'report_write_failed');view=view[count:]
        os.fsync(descriptor);os.fsync(parent)
    finally:
        if descriptor is not None:os.close(descriptor)
        os.close(parent)


def fixture_root(path):
    root=Path(path);parent=directory(str(root.parent))
    try:os.mkdir(root.name,0o700,dir_fd=parent);os.fsync(parent)
    finally:os.close(parent)
    descriptor=directory(str(root))
    try:
        for name in ('inputs','scripts'):os.mkdir(name,0o700,dir_fd=descriptor)
        os.fsync(descriptor)
    finally:os.close(descriptor)
    return root


def main(argv):
    args=parse_arguments(argv)
    report={'schema':'blackoutburst-linux-execution-backend-qualification-v1','result':'refused',
        'scope':'synthetic_Linux_test_backend_only','model_calls':0,'account_rpc_calls':0,'mcp_calls':0,
        'planner_policy_qualified':False,'owner_subscription_qualified':False,'owner_service_activated':False,
        'broker_snapshot_admitted':False,'fixtures_retained':True,'cleanup_confirmed':False,
        'source_execution_from_held_bytes':False}
    backend=None
    try:
        real_guard(args);report['runner_sha256']=VERIFIED_RUNNER_SHA256
        inventory,sources=load_sources(args.package,args.source_inventory,args.expected_source_inventory_sha256)
        loader=HeldPackage(args.package,sources);sys.meta_path.insert(0,loader)
        from blackoutburst_agents.linux_planner_policy import (LINUX_EXECUTION_SOURCES,_executable_pin,
            linux_execution_identity,linux_source_pins)
        from blackoutburst_agents.dev_manifest import NamedTest,SCOPE_FIELDS,canonical as product_canonical,digest
        from blackoutburst_agents.dev_journal import PrivateDirectory
        from blackoutburst_agents.linux_test_backend import (SupervisedLinuxNamedPythonBackend,pin_linux_execution_plan,
            prepare_linux_execution_manifest,prepare_linux_backend)
        from blackoutburst_agents.linux_test_protocol import linux_execution_backend_id
        from blackoutburst_agents.linux_execution_qualification import validate_qualification_evidence
        require(tuple(LINUX_EXECUTION_SOURCES)==ALLOWED_SOURCES,'source_roster_changed')
        require(canonical(linux_source_pins()['files'])==canonical(inventory['files']),'source_pin_mismatch')
        require(_executable_pin(args.interpreter)['sha256']==args.expected_interpreter_sha256
                and _executable_pin(args.unshare)['sha256']==args.expected_unshare_sha256,'toolchain_pin_mismatch')
        current=linux_execution_identity()
        report.update(environment_sha256=sha(canonical(current)),source_inventory_sha256=sha(canonical(inventory)),
            interpreter_sha256=args.expected_interpreter_sha256,unshare_sha256=args.expected_unshare_sha256,
            source_execution_from_held_bytes=True,host=current['host'],uid=current['uid'])
        root=fixture_root(args.root)
        with PrivateDirectory(root/'inputs') as inputs:inputs.create('qualification.json',b'{"value":42}')
        with PrivateDirectory(root/'scripts') as scripts:scripts.create('check.py',
            b'import json\nfrom pathlib import Path\nassert json.loads(Path("qualification.json").read_text()) == {"value":42}\n')
        plan=pin_linux_execution_plan(root=str(root/'inputs'),scripts_root=str(root/'scripts'),
            scripts={'qualification':{'path':'check.py','timeout_seconds':5,'max_output_bytes':4096}},
            interpreter=args.interpreter,unshare=args.unshare)
        plan_hash=digest(product_canonical(plan));scope={name:'synthetic-qualification' for name in SCOPE_FIELDS}
        scope['profile_version']=1
        authorization=lambda actual,action,resource:(actual==scope and resource=='repo:synthetic-linux-qualification'
            and action in {'repo.read_file','repo.run_test','repo.patch_file'})
        prepare_linux_execution_manifest(root/'journal',execution_plan=plan,root=str(root/'inputs'),
            base_sha='a'*40,scope=scope,paths=['qualification.json'],resource='repo:synthetic-linux-qualification',
            authorize_scope=authorization,tests=[NamedTest('qualification',linux_execution_backend_id(plan_hash),5,4096)])
        with PrivateDirectory(root/'journal') as journal:manifest=decode(journal.read('manifest.json',128*1024))
        prepare_linux_backend(root/'backend',manifest=manifest,plan=plan)
        backend=SupervisedLinuxNamedPythonBackend(root/'backend',manifest=manifest,authorize_scope=authorization)
        receipt=backend.qualify()
        require(validate_qualification_evidence(backend,receipt),'matrix_evidence_refused')
        require(canonical(linux_source_pins()['files'])==canonical(inventory['files']),'source_changed_during_qualification')
        require(linux_execution_identity()==current,'environment_changed_during_qualification')
        report.update(result='qualified_synthetic_backend_fixture',checks=receipt['checks'],
            cleanup_confirmed=receipt['cleanup_confirmed'],plan_sha256=plan_hash,receipt_sha256=sha(canonical(receipt)),
            case_proof_sha256=receipt['native_receipts'])
    except Exception as error:
        code=getattr(error,'code','qualification_refused')
        report['result']='refused';report['reason']=code if type(code) is str and re.fullmatch('[a-z][a-z0-9_]{0,99}',code) else 'qualification_refused'
    finally:
        if backend is not None:backend.close()
    write_report(args.output,report)
    print(json.dumps({key:report[key] for key in ('result','cleanup_confirmed','fixtures_retained')},sort_keys=True))
    return 0 if report['result']=='qualified_synthetic_backend_fixture' else 2


if __name__=='__main__':
    try:raise SystemExit(main(sys.argv[1:]))
    except (Refused,OSError,ValueError,UnicodeError) as error:
        reason=error.code if isinstance(error,Refused) else 'qualification_report_refused'
        print(json.dumps({'result':'refused','reason':reason},sort_keys=True));raise SystemExit(2)
