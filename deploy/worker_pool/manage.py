"""Offline preparation and explicit additive root installation, never activation."""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import sys

if __package__:
    from . import package as p
else:
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    import pool_package as p


def command(argv):
    try:return subprocess.run(argv,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        timeout=60,env={'PATH':'/usr/bin:/bin','LANG':'C','GIT_CONFIG_NOSYSTEM':'1',
            'GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_SYSTEM':'/dev/null'}).stdout
    except (OSError,subprocess.SubprocessError):p.fail('pool_system_command_failed')


def prepare_addon(source,output,*,codex,bwrap,gh,codex_version,gh_version,python_pin,
                  unix_owner,board_owner,board_endpoint,unit_limits):
    source=p.no_symlinks(source);output=p.no_symlinks(output)
    if output.exists():p.fail('pool_output_exists')
    def git(*args):return command(['/usr/bin/git','-c','core.fsmonitor=false','-c','core.hooksPath=/dev/null',
        '-c','core.attributesFile=/dev/null','-C',str(source),*args])
    if git('status','--porcelain'):p.fail('pool_clean_source_required')
    revision=git('rev-parse','HEAD').decode().strip();tracked=git('ls-tree','-r','--name-only','HEAD').decode().splitlines()
    names={name:'source/'+name.removeprefix('integrations/codex-personal/') for name in tracked
        if re.fullmatch(r'integrations/codex-personal/blackoutburst_agents/[A-Za-z0-9_]+\.py',name)}
    names.update({'connect.py':'source/connect.py','deploy/worker_pool/package.py':'pool_package.py',
        'deploy/worker_pool/manage.py':'pool_manage.py','deploy/worker_pool/entry.py':'pool_entry.py',
        'deploy/worker_pool/unit.service.in':'unit.service.in'})
    if not set(names)<=set(tracked):p.fail('pool_source_incomplete')
    version=re.findall(r'^version = "(0\.[0-9]+\.[0-9]+)"$',git('show',revision+':integrations/codex-personal/pyproject.toml').decode(),re.M)
    if len(version)!=1:p.fail('pool_source_version_missing')
    data={target:git('show',revision+':'+name) for name,target in names.items()}
    for target,path in (('tools/codex',codex),('tools/codex-resources/bwrap',bwrap),('tools/gh',gh)):
        data[target]=p.read_regular(path);p.elf(data[target],'x86_64')
    manifest=p.validate_manifest({'schema':p.SCHEMA,'revision':revision,'adapter_version':version[0],
        'architecture':'x86_64','unix_owner':unix_owner,'board_owner':board_owner,'board_endpoint':board_endpoint,
        'python':python_pin,'codex_version':codex_version,'gh_version':gh_version,'unit_limits':unit_limits,
        'files':{name:{'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()} for name,raw in data.items()}})
    stage=Path(tempfile.mkdtemp(prefix='.pool-bundle-',dir=output.parent))
    try:
        for name,raw in {**data,'manifest.json':p.encoded(manifest)}.items():
            target=stage/name;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            _new_file(target,raw,0o600)
        p.load_bundle(stage);os.rename(stage,output);_fsync(output.parent)
    finally:
        if stage.exists():shutil.rmtree(stage)
    return {'status':'prepared','bundle_id':p.manifest_id(manifest),'activated':False,'execution_qualified':False}


def plan_addon(directory):
    manifest=p.load_bundle(directory);owner=manifest['unix_owner']
    return {'status':'plan','bundle_id':p.manifest_id(manifest),'release':str(p.RELEASES/p.manifest_id(manifest)),
        'private_root':str(p.owner_root(owner)),'service':p.unit_name(owner),'unit_limits':manifest['unit_limits'],
        'actions':['install_new_public_release','create_new_private_pool_directories','write_disabled_pool_unit','daemon_reload'],
        'human_steps':['normal_owner_cli_authentication','private_canonical_development_anchor_exists',
            'pair_explicit_selected_coder_reviewer_profiles_connections','grant_exact_resources_and_free_runtime_capacity',
            'normal_owner_github_authentication_with_write_access',
            'provide_new_local_repository_and_approved_verifiers','review_explicit_batch_and_workflow_plan','explicit_start_new_unit'],
        'activated':False,'execution_qualified':False}


def _fsync(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)
def _new_file(path,raw,mode):
    p.no_symlinks(path);fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    try:
        with os.fdopen(fd,'wb',closefd=False) as stream:stream.write(raw);stream.flush();os.fsync(fd)
        os.fchmod(fd,mode)
    finally:os.close(fd)
    _fsync(path.parent)
def _mkdir(path,mode=0o755):
    p.no_symlinks(path)
    if not path.exists():path.mkdir(mode=mode);_fsync(path.parent)
    p.protected(path)
def _unit_state(unit):
    fields=('LoadState','ActiveState','SubState','UnitFileState','MainPID','ControlPID','DropInPaths','FragmentPath')
    raw=command(['/usr/bin/systemctl','show','--no-pager','--property='+','.join(fields),unit])
    if len(raw)>8192:p.fail('pool_unit_state_invalid')
    values={}
    for line in raw.decode('utf-8','strict').splitlines():
        key,value=line.split('=',1)
        if key in values or key not in fields:p.fail('pool_unit_state_invalid')
        values[key]=value
    if set(values)!=set(fields):p.fail('pool_unit_state_invalid')
    return values
def _inactive(unit):
    state=_unit_state(unit)
    if (state['LoadState']!='loaded' or state['ActiveState']!='inactive' or state['SubState']!='dead'
            or state['UnitFileState']!='disabled' or state['MainPID']!='0' or state['ControlPID']!='0'
            or state['DropInPaths'] or state['FragmentPath']!=str(p.UNIT_DIR/unit)):p.fail('pool_unit_not_inactive_disabled')
def _root_context(manifest):
    if os.geteuid()!=0:p.fail('pool_administrator_required')
    if platform.system()!='Linux' or platform.machine()!=manifest['architecture']:p.fail('pool_platform_mismatch')
    try:account=pwd.getpwnam(manifest['unix_owner'])
    except KeyError:p.fail('pool_existing_account_required')
    if account.pw_uid==0 or account.pw_gid==0:p.fail('pool_existing_account_required')
    normal=p.no_symlinks(account.pw_dir);info=normal.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=account.pw_uid or stat_mode(info)!=0o700:p.fail('pool_normal_home_unconfirmed')
    python=Path(manifest['python']['path']);p.protected(python)
    raw=p.read_regular(python);p.elf(raw,manifest['architecture'])
    if hashlib.sha256(raw).hexdigest()!=manifest['python']['sha256']:p.fail('pool_python_changed')
    return account
def stat_mode(info):return info.st_mode&0o7777
def _root_lock(fd):
    info=os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=0 or stat_mode(info)!=0o600:p.fail('pool_install_lock_untrusted')
def _absent_unit(unit):
    for root in (p.UNIT_DIR,Path('/run/systemd/system'),Path('/usr/lib/systemd/system')):
        if not root.exists():continue
        for index,path in enumerate(root.rglob('*')):
            if index>=25000:p.fail('pool_unit_metadata_limit')
            if (path.name in (unit,unit+'.d') or path.is_symlink() and Path(os.readlink(path)).name==unit):
                p.fail('pool_unit_already_exists')
    state=_unit_state(unit)
    if state!={'LoadState':'not-found','ActiveState':'inactive','SubState':'dead','UnitFileState':'',
            'MainPID':'0','ControlPID':'0','DropInPaths':'','FragmentPath':''}:p.fail('pool_unit_already_exists')


def install_addon(directory,*,apply=False,expected_bundle_id=None):
    plan=plan_addon(directory)
    if not apply:return plan
    if expected_bundle_id is None or p.sha(expected_bundle_id)!=plan['bundle_id']:p.fail('pool_approved_bundle_required')
    manifest=p.load_bundle(directory);account=_root_context(manifest);unit=p.unit_name(manifest['unix_owner'])
    if p.manifest_id(manifest)!=plan['bundle_id']:p.fail('pool_bundle_selection_changed')
    _absent_unit(unit)
    root=p.owner_root(manifest['unix_owner']);release=p.RELEASES/plan['bundle_id']
    if root.exists() or root.is_symlink() or release.exists() or release.is_symlink():p.fail('pool_existing_layout_refused')
    for path in (p.BASE,p.BASE/'owners',p.BASE/'installations',p.RELEASES.parent,p.RELEASES):_mkdir(path)
    lock=os.open(p.BASE/'install.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:p.fail('pool_install_busy')
        _root_lock(lock)
        _absent_unit(unit)
        if root.exists() or release.exists():p.fail('pool_existing_layout_refused')
        receipt=p.BASE/'installations'/(plan['bundle_id']+'.json')
        request={**plan,'schema':p.SCHEMA,'status':'installing','unix_owner':manifest['unix_owner'],
            'uid':account.pw_uid,'gid':account.pw_gid,'normal_home':account.pw_dir,'unit_sha256':None}
        _new_file(receipt,p.encoded(request),0o600)
        stage=Path(tempfile.mkdtemp(prefix='.pool-release-',dir=p.RELEASES))
        try:
            for name in ('manifest.json',*manifest['files']):
                target=stage/name;target.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
                _new_file(target,p.read_regular(Path(directory)/name),0o755 if name.startswith('tools/') else 0o444)
            for parent,dirs,_ in os.walk(stage):
                for name in dirs:os.chmod(Path(parent)/name,0o755)
            copied=p.load_bundle(stage)
            if copied!=manifest or p.manifest_id(copied)!=plan['bundle_id']:p.fail('pool_bundle_selection_changed')
            os.chmod(stage,0o755)
            os.rename(stage,release);_fsync(p.RELEASES)
        finally:
            if stage.exists():shutil.rmtree(stage)
        root.mkdir(mode=0o700);os.chown(root,account.pw_uid,account.pw_gid)
        for name in ('batches','connections','repositories','checkouts','git-state','verifiers','config'):
            path=root/name;path.mkdir(mode=0o700);os.chown(path,account.pw_uid,account.pw_gid);_fsync(path)
        _fsync(root);_fsync(root.parent)
        rendered=p.render_unit(p.read_regular(release/'unit.service.in').decode(),manifest,release,account.pw_dir)
        _new_file(p.UNIT_DIR/unit,rendered.encode(),0o644)
        command(['/usr/bin/systemctl','daemon-reload']);_inactive(unit)
        request.update(status='installed_disabled',unit_sha256=hashlib.sha256(rendered.encode()).hexdigest())
        _new_file(receipt.with_suffix('.complete.json'),p.encoded(request),0o600)
        return {**plan,'status':'installed_disabled','receipt':str(receipt.with_suffix('.complete.json'))}
    finally:os.close(lock)


def rollback_addon(receipt,*,apply=False):
    receipt=p.no_symlinks(receipt);p.protected(receipt)
    value=p.decode(p.read_regular(receipt,128*1024))
    if (type(value) is not dict or value.get('schema')!=p.SCHEMA or value.get('status')!='installed_disabled'
            or receipt!=p.BASE/'installations'/(value.get('bundle_id','')+'.complete.json')):p.fail('pool_rollback_receipt_invalid')
    p.sha(value['bundle_id']);p.sha(value['unit_sha256'])
    unit=p.unit_name(value['unix_owner']);path=p.UNIT_DIR/unit
    p.protected(path)
    if hashlib.sha256(p.read_regular(path)).hexdigest()!=value['unit_sha256']:p.fail('pool_unit_changed')
    result={'status':'rollback_plan','service':unit,'preserve_private_state':True,'activated':False}
    if not apply:return result
    if os.geteuid()!=0:p.fail('pool_administrator_required')
    _inactive(unit)
    if hashlib.sha256(p.read_regular(path)).hexdigest()!=value['unit_sha256']:p.fail('pool_unit_changed')
    path.unlink();_fsync(p.UNIT_DIR);command(['/usr/bin/systemctl','daemon-reload'])
    return {**result,'status':'new_unit_removed'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='action',required=True)
    build=commands.add_parser('prepare');build.add_argument('--source',required=True);build.add_argument('--output',required=True);build.add_argument('--spec',required=True)
    for name in ('plan','install','rollback'):
        command_parser=commands.add_parser(name);command_parser.add_argument('path')
        if name!='plan':command_parser.add_argument('--apply',action='store_true')
        if name=='install':command_parser.add_argument('--expected-bundle-id')
    args=parser.parse_args(argv)
    try:
        if args.action=='prepare':result=prepare_addon(args.source,args.output,**p.decode(p.read_regular(args.spec,128*1024)))
        elif args.action=='plan':result=plan_addon(args.path)
        elif args.action=='install':result=install_addon(args.path,apply=args.apply,expected_bundle_id=args.expected_bundle_id)
        else:result=rollback_addon(args.path,apply=args.apply)
        print(p.encoded(result).decode().strip());return 0
    except (p.PackageError,OSError,ValueError,TypeError):print('{"status":"refused"}');return 2

if __name__=='__main__':raise SystemExit(main())
