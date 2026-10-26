#!/usr/bin/env python3
"""Installed explicit finite batch entry, with no Host or automatic activation."""
import argparse
import json
import os
from pathlib import Path
import platform
import pwd
import re
import signal
import stat
import sys
import threading

if __package__:
    from . import package as p
else:
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    import pool_package as p


def _planner_qualification_diagnostic(error):
    """Revalidate every exported fact; arbitrary exception payloads stay private."""
    try:
        if type(error.code) is not str or error.code not in {'linux_planner_cli_version_changed','linux_planner_canary_output_invalid',
                'linux_planner_canary_created_file','linux_planner_native_policy_unqualified',
                'linux_planner_qualification_cleanup_unconfirmed','linux_planner_policy_changed'}:
            return None
        supplied = vars(error).get('_linux_planner_canary_diagnostic')
        if type(supplied) is not dict or len(supplied) not in (3,4):
            return None
        value = dict(supplied)
        if type(value.get('checks')) is not dict:
            return None
        value['checks'] = dict(value['checks'])
        checks = {'inside_read','inside_write_denied','outside_read_denied',
                  'outside_write_denied','network_loopback_denied'}
        phases = {'cli_version','host_fixtures','canary_pins','canary_execution',
                  'canary_result','cleanup','final_pins'}
        if (any(type(key) is not str for key in value)
                or set(value) not in ({'checks','cleanup_confirmed','phase'},
                                     {'checks','cleanup_confirmed','phase','errno'})
                or any(type(key) is not str or type(item) is not bool for key,item in value['checks'].items())
                or value['checks'] and set(value['checks']) != checks
                or type(value['cleanup_confirmed']) is not bool
                or type(value['phase']) is not str or value['phase'] not in phases
                or 'errno' in value and (type(value['errno']) is not int or not 1 <= value['errno'] <= 4095)):
            return None
        return {'status':'qualification_diagnostic','kind':'linux_planner_canary',
                'checks':dict(value['checks']),'cleanup_confirmed':value['cleanup_confirmed'],
                'phase':value['phase'],**({'errno':value['errno']} if 'errno' in value else {})}
    except (AttributeError,TypeError,ValueError,RuntimeError):
        return None


def owner_context(manifest,owner):
    if owner!=manifest['unix_owner'] or os.geteuid()==0:p.fail('pool_owner_login_required')
    account=pwd.getpwnam(owner)
    if (account.pw_uid!=os.geteuid() or account.pw_uid!=os.getuid()
            or account.pw_gid!=os.getegid() or account.pw_gid!=os.getgid()
            or any(group!=account.pw_gid for group in os.getgroups())
            or os.environ.get('HOME') not in (None,account.pw_dir)
            or os.environ.get('CODEX_HOME') or str(Path(sys.executable))!=manifest['python']['path']
            or platform.system()!='Linux' or platform.machine()!=manifest['architecture']):p.fail('pool_owner_context_mismatch')
    if any(os.environ.get(key) for key in ('OPENAI_API_KEY','OPENAI_BASE_URL','AZURE_OPENAI_API_KEY',
            'ANTHROPIC_API_KEY','SSH_AUTH_SOCK','GH_TOKEN','GITHUB_TOKEN')):p.fail('pool_owner_environment_refused')
    info=p.no_symlinks(account.pw_dir).stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=account.pw_uid or stat.S_IMODE(info.st_mode)!=0o700:p.fail('pool_owner_context_mismatch')
    return account


def in_installed_unit(manifest,release,account):
    unit=p.unit_name(manifest['unix_owner']);path=p.UNIT_DIR/unit
    p.protected(path)
    expected=p.render_unit(p.read_regular(release/'unit.service.in').decode(),manifest,release,account.pw_dir).encode()
    if p.read_regular(path)!=expected:p.fail('pool_installed_unit_changed')
    raw=current_cgroup().decode('utf-8','strict')
    if not any(unit in line.split(':',2)[-1].split('/') for line in raw.splitlines()):p.fail('pool_installed_unit_required')


def current_cgroup():
    """Only this live PID's procfs data: pseudo-files legitimately have size 0."""
    pid=os.getpid();descriptors=[]
    try:
        proc=os.open('/proc',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);descriptors.append(proc)
        process=os.open(str(pid),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=proc);descriptors.append(process)
        parent=os.fstat(process)
        fd=os.open('cgroup',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=process);descriptors.append(fd)
        before=os.fstat(fd)
        def pin(value):return value.st_dev,value.st_ino,value.st_mode,value.st_uid,value.st_gid
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1:p.fail('pool_cgroup_unconfirmed')
        chunks=[];size=0
        while size<=65536:
            raw=os.read(fd,min(8192,65537-size))
            if not raw:break
            chunks.append(raw);size+=len(raw)
        if (not 0<size<=65536 or os.getpid()!=pid or pin(before)!=pin(os.fstat(fd))
                or pin(before)!=pin(os.stat('cgroup',dir_fd=process,follow_symlinks=False))
                or pin(parent)!=pin(os.stat(str(pid),dir_fd=proc,follow_symlinks=False))):p.fail('pool_cgroup_unconfirmed')
        return b''.join(chunks)
    finally:
        for descriptor in reversed(descriptors):os.close(descriptor)


def ensure_development_anchor(account):
    """Create only absent canonical directories; inspect no journal/lock contents."""
    base=p.no_symlinks(account.pw_dir);descriptors=[];created=[];chain=[]
    def check(info):
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:
            p.fail('pool_anchor_directory_untrusted')
    def identity(info):return info.st_dev,info.st_ino,info.st_uid,info.st_mode
    def held():
        for index,(fd,name,pin) in enumerate(chain):
            current=os.stat(base if index==0 else name,**({} if index==0 else {'dir_fd':chain[index-1][0]}),follow_symlinks=False)
            if identity(os.fstat(fd))!=pin or identity(current)!=pin:p.fail('pool_anchor_directory_changed')
    try:
        fd=os.open(base,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);descriptors.append(fd)
        info=os.fstat(fd);check(info);chain.append((fd,None,identity(info)));path=base
        for name in ('.local','state','blackoutburst-agents','development'):
            held()
            try:os.mkdir(name,0o700,dir_fd=fd);os.fsync(fd);created.append(str(path/name))
            except FileExistsError:pass
            fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);descriptors.append(fd)
            info=os.fstat(fd);check(info);chain.append((fd,name,identity(info)));path=path/name
        held()
        return {'status':'anchor_directories_ready','anchor':str(path),'created':created,'changed_existing':False}
    finally:
        for descriptor in reversed(descriptors):os.close(descriptor)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release',required=True);parser.add_argument('--owner',required=True)
    parser.add_argument('action',choices=('batch-plan','workflow-plan','run-batch','run-workflow','init-development-anchor'))
    parser.add_argument('--batch');parser.add_argument('--workflow')
    args=parser.parse_args(argv)
    try:
        release=p.absolute(args.release);manifest=p.verify_release(release);account=owner_context(manifest,args.owner)
        if args.action=='init-development-anchor':
            result=ensure_development_anchor(account);print(json.dumps(result,sort_keys=True,separators=(',',':')));return 0
        root=p.owner_root(args.owner)
        sys.path.insert(0,str(release/'source'))
        from blackoutburst_agents.finite_pool_batch import FiniteLinuxPoolBatch,load_batch
        from blackoutburst_agents.dev_manifest import DevelopmentError
        from blackoutburst_agents.planner_policy import PlannerPolicyError
        if args.batch is None:p.fail('pool_batch_configuration_required')
        batch_path=p.absolute(args.batch)
        if not batch_path.is_relative_to(root/'config'):p.fail('pool_configuration_path_refused')
        if args.action in ('batch-plan','workflow-plan'):
            batch,pin=load_batch(batch_path,root=root,owner_id=manifest['board_owner'],board_endpoint=manifest['board_endpoint'],
                max_seconds=manifest['unit_limits']['lifetime_seconds']-90)
            result={'status':'plan','batch_id':batch['batch_id'],'configuration_sha256':pin['sha256'],
                'jobs':[{key:job[key] for key in ('name','agent_id','project_id','ticket_id','resource','model')} for job in batch['jobs']],
                'activated':False,'execution_qualified':False}
            if args.action=='workflow-plan':
                from blackoutburst_agents.cloud_workflow import load_workflow
                if args.workflow is None:p.fail('pool_workflow_configuration_required')
                workflow,workflow_pin=load_workflow(p.absolute(args.workflow),root=root,batch=batch)
                result['workflow_configuration_sha256']=workflow_pin['sha256']
                result['workflow']=[dict(name=item['name'],paths=item['paths'],reviewer_agent_id=item['reviewer']['agent_id'],
                    reviewer_model=item['reviewer']['model']) for item in workflow['jobs']]
                result['destination']='https://github.com/Blackoutburst/agent-board'
        else:
            in_installed_unit(manifest,release,account)
            stop=threading.Event()
            previous={number:signal.signal(number,lambda *_:stop.set()) for number in (signal.SIGTERM,signal.SIGINT)}
            try:
                batch=FiniteLinuxPoolBatch(root,owner_id=manifest['board_owner'],board_endpoint=manifest['board_endpoint'],
                    executable=release/'tools/codex',cli_version='codex-cli '+manifest['codex_version'],
                    interpreter=manifest['python']['path'],unshare='/usr/bin/unshare',
                    max_seconds=manifest['unit_limits']['lifetime_seconds']-90)
                workflow=None
                if args.action=='run-workflow':
                    from blackoutburst_agents.cloud_workflow import CloudLinuxWorkflow
                    if args.workflow is None:p.fail('pool_workflow_configuration_required')
                    selected,_=load_batch(batch_path,root=root,owner_id=manifest['board_owner'],board_endpoint=manifest['board_endpoint'],
                        max_seconds=manifest['unit_limits']['lifetime_seconds']-90)
                    workflow=CloudLinuxWorkflow(p.absolute(args.workflow),root=root,batch=selected,owner_id=manifest['board_owner'],
                        board_endpoint=manifest['board_endpoint'],gh_executable=release/'tools/gh',python_executable=manifest['python']['path'])
                elif args.workflow is not None:p.fail('pool_workflow_mode_required')
                result=batch.run(batch_path,should_stop=stop.is_set,workflow=workflow)
            finally:
                for number,handler in previous.items():signal.signal(number,handler)
        print(json.dumps(result,sort_keys=True,separators=(',',':')))
        return 0 if result['status'] in ('complete','plan') else 3
    except Exception as error:
        reason='finite_pool_entry_refused'
        if isinstance(error,p.PackageError) or 'DevelopmentError' in locals() and isinstance(error,DevelopmentError):
            if type(error.code) is str and re.fullmatch('[a-z][a-z0-9_]{0,119}',error.code):reason=error.code
        print(json.dumps({'status':'needs_review','reason':reason},sort_keys=True,separators=(',',':')))
        if 'PlannerPolicyError' in locals() and type(error) is PlannerPolicyError:
            diagnostic = _planner_qualification_diagnostic(error)
            if diagnostic is not None:
                print(json.dumps(diagnostic,sort_keys=True,separators=(',',':')))
        return 3

if __name__=='__main__':raise SystemExit(main())
