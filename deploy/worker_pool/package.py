"""Credential-free additive pool bundles and installed public file checks."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct

RELEASES=Path('/opt/blackoutburst-agent-pool/releases')
BASE=Path('/var/lib/blackoutburst-agent-pool')
UNIT_DIR=Path('/etc/systemd/system')
OWNERS=('bb-agent-thescreamer','bb-agent-blackout')
SCHEMA='worker-pool-addon-v1'
MAX_FILE=384*1024*1024
REQUIRED_SDK_MODULES=('__init__','finite_pool_batch','linux_pool_preparation','cloud_workflow',
    'development_pool_delivery','review_pool','review_client','review_worker','linux_review_binding',
    'review_lifecycle','review_lifecycle_client','development_client','development_frame',
    'development_authorization')
CONTINUATION_SDK_MODULES=('continuation','continuation_journal','continuation_runner',
    'continuation_pool_configuration','continuation_pool_service','development_provider_continuation',
    'development_cycle_driver','development_checkpoint_proof','private_development_checkpoint',
    'development_aggregate_delivery','development_aggregate_review_source',
    'continuation_aggregate_review','continuation_human_handoff')


def continuation_supported(version):
    if type(version) is not str or re.fullmatch(r'0\.[0-9]+\.[0-9]+',version) is None:fail()
    return tuple(int(part) for part in version.split('.')) >= (0,45,0)


def required_sdk_modules(version):
    # The installer must still verify a receipted 0.34 release during a stopped
    # upgrade. Only releases using protected pins require the added module.
    if type(version) is not str or re.fullmatch(r'0\.[0-9]+\.[0-9]+',version) is None:fail()
    return REQUIRED_SDK_MODULES + (('protected_public_pins',) if
        tuple(int(part) for part in version.split('.')) >= (0,35,0) else ()) + (
        CONTINUATION_SDK_MODULES if continuation_supported(version) else ())


class PackageError(Exception):
    def __init__(self,code):self.code=code;super().__init__(code)


def fail(code='invalid_pool_addon'):raise PackageError(code)
def encoded(value):return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True)+'\n').encode()
def decode(raw):
    def pairs(items):
        value={}
        for key,item in items:
            if key in value:fail()
            value[key]=item
        return value
    try:return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:fail())
    except (ValueError,TypeError,UnicodeError,RecursionError):fail()
def sha(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}',value) is None:fail()
    return value
def absolute(value):
    if (type(value) is not str or re.fullmatch('/[A-Za-z0-9_./-]+',value) is None
            or value=='/' or str(Path(value))!=value or '..' in Path(value).parts or len(value)>1024
            or len(Path(value).parts)>128):fail('invalid_pool_path')
    return Path(value)
def no_symlinks(path):
    path=absolute(str(path))
    if any(item.is_symlink() for item in (path,*path.parents)):fail('pool_symlink_refused')
    return path
def read_regular(path,maximum=MAX_FILE):
    path=no_symlinks(path);fd=None;nodes=[]
    def directory_pin(info):return info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid
    def held():
        for index,(descriptor,name,initial) in enumerate(nodes):
            linked=os.stat('/' if index==0 else name,follow_symlinks=False,
                **({} if index==0 else {'dir_fd':nodes[index-1][0]}))
            if directory_pin(initial)!=directory_pin(os.fstat(descriptor)) or directory_pin(initial)!=directory_pin(linked):fail('pool_parent_changed')
    try:
        for name in [None]+list(path.parent.parts[1:]):
            descriptor=os.open('/' if name is None else name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,
                **({} if name is None else {'dir_fd':nodes[-1][0]}))
            nodes.append((descriptor,name,os.fstat(descriptor)))
        held()
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=nodes[-1][0]);before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or not 0<before.st_size<=maximum:fail('invalid_pool_file')
        parts=[];total=0
        while total<=maximum:
            raw=os.read(fd,min(65536,maximum+1-total))
            if not raw:break
            parts.append(raw);total+=len(raw)
        after=os.fstat(fd);named=os.stat(path.name,dir_fd=nodes[-1][0],follow_symlinks=False)
        def pin(item):return (item.st_dev,item.st_ino,item.st_mode,item.st_uid,item.st_gid,item.st_nlink,item.st_size,item.st_mtime_ns,item.st_ctime_ns)
        if total!=before.st_size or total>maximum or pin(before)!=pin(after) or pin(after)!=pin(named):fail('pool_file_changed')
        held();return b''.join(parts)
    except OSError:fail('pool_file_unavailable')
    finally:
        if fd is not None:os.close(fd)
        for descriptor,_,_ in reversed(nodes):os.close(descriptor)
def protected(path):
    path=no_symlinks(path)
    for item in (path,*path.parents):
        info=item.lstat()
        if info.st_uid!=0 or info.st_mode&0o022:fail('pool_public_install_untrusted')
def elf(raw,architecture):
    if (architecture!='x86_64' or len(raw)<64 or raw[:7]!=b'\x7fELF\x02\x01\x01'
            or raw[7] not in (0,3) or struct.unpack_from('<H',raw,16)[0] not in (2,3)
            or struct.unpack_from('<H',raw,18)[0]!=62):fail('pool_linux_elf_required')
def owner_root(owner):
    if owner not in OWNERS:fail('pool_owner_unknown')
    return BASE/'owners'/owner
def unit_name(owner):owner_root(owner);return 'blackoutburst-agent-pool-'+owner+'.service'
def continuation_unit_name(owner,*,timer=False):
    owner_root(owner)
    return 'blackoutburst-agent-continuation-'+owner+('.timer' if timer else '.service')
def continuation_timer_name(owner):return continuation_unit_name(owner,timer=True)
def limits(value):
    bounds={'memory_bytes':(512*1024*1024,64*1024**3),'memory_high_bytes':(256*1024*1024,64*1024**3),
        'tasks':(128,2048),'cpu_percent':(25,800),'lifetime_seconds':(120,14500),'stop_seconds':(30,90)}
    if type(value) is not dict or set(value)!=set(bounds):fail('pool_unit_limits_required')
    for key,(low,high) in bounds.items():
        if type(value[key]) is not int or not low<=value[key]<=high:fail('pool_unit_limits_required')
    if value['memory_high_bytes']>=value['memory_bytes']:fail('pool_unit_limits_required')
    return value
def validate_manifest(value):
    fields={'schema','revision','adapter_version','architecture','unix_owner','board_owner','board_endpoint',
            'python','codex_version','gh_version','unit_limits','files'}
    if type(value) is not dict or set(value)!=fields or value['schema']!=SCHEMA:fail()
    if (type(value['revision']) is not str or re.fullmatch('[0-9a-f]{40}',value['revision']) is None
            or type(value['adapter_version']) is not str or re.fullmatch(r'0\.[0-9]+\.[0-9]+',value['adapter_version']) is None
            or value['architecture']!='x86_64' or value['unix_owner'] not in OWNERS
            or type(value['board_owner']) is not str or re.fullmatch('[A-Za-z0-9_.:-]{1,200}',value['board_owner']) is None
            or type(value['board_endpoint']) is not str
            or re.fullmatch(r'https://[a-z0-9.-]+(?::[0-9]{1,5})?/mcp',value['board_endpoint']) is None):fail()
    for key in ('codex_version','gh_version'):
        if type(value[key]) is not str or re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9.-]+)?',value[key]) is None:fail()
    python=value['python']
    if type(python) is not dict or set(python)!={'path','sha256'}:fail()
    sha(python['sha256']);absolute(python['path'])
    if re.fullmatch(r'/usr/bin/python3\.[0-9]+',python['path']) is None:fail('pool_system_python_required')
    limits(value['unit_limits']);files=value['files']
    required={'pool_entry.py','pool_package.py','pool_manage.py','unit.service.in','source/connect.py',
        'tools/codex','tools/codex-resources/bwrap','tools/gh'}|{
        'source/blackoutburst_agents/'+name+'.py' for name in required_sdk_modules(value['adapter_version'])}
    if continuation_supported(value['adapter_version']):
        required.update(('continuation.service.in','continuation.timer.in'))
    if type(files) is not dict or not required<=set(files) or not 10<=len(files)<=400:fail('pool_inventory_invalid')
    total=0
    for name,pin in files.items():
        allowed=(name in required or re.fullmatch(r'source/blackoutburst_agents/[A-Za-z0-9_]+\.py',name))
        if (not allowed or type(pin) is not dict or set(pin)!={'size','sha256'}
                or type(pin['size']) is not int or not 0<pin['size']<=MAX_FILE):fail('pool_inventory_invalid')
        sha(pin['sha256']);total+=pin['size']
        if not name.startswith('tools/') and pin['size']>2*1024*1024:fail()
    if total>512*1024*1024:fail('pool_bundle_limit')
    return value
def manifest_id(value):return hashlib.sha256(encoded(validate_manifest(value))).hexdigest()
def load_bundle(directory):
    directory=no_symlinks(directory)
    value=validate_manifest(decode(read_regular(directory/'manifest.json',256*1024)))
    actual=set();count=0
    for parent,dirs,files in os.walk(directory,followlinks=False):
        for name in dirs+files:
            count+=1
            if count>600:fail('pool_inventory_invalid')
            no_symlinks(Path(parent)/name)
        actual.update(str((Path(parent)/name).relative_to(directory)) for name in files)
    if actual!=set(value['files'])|{'manifest.json'}:fail('pool_inventory_mismatch')
    for name,pin in value['files'].items():
        raw=read_regular(directory/name)
        if len(raw)!=pin['size'] or hashlib.sha256(raw).hexdigest()!=pin['sha256']:fail('pool_inventory_changed')
        if name.startswith('tools/'):elf(raw,value['architecture'])
    return value
def verify_release(directory):
    directory=no_symlinks(directory);value=load_bundle(directory)
    if directory!=RELEASES/manifest_id(value):fail('unexpected_pool_release')
    for name in ('manifest.json',*value['files']):
        protected(directory/name)
        if stat.S_IMODE((directory/name).stat().st_mode)!=(0o755 if name.startswith('tools/') else 0o444):fail('pool_release_mode_changed')
    path=Path(value['python']['path']);protected(path)
    raw=read_regular(path);elf(raw,value['architecture'])
    if hashlib.sha256(raw).hexdigest()!=value['python']['sha256']:fail('pool_python_changed')
    return value
def render_unit(template,value,release,normal_home):
    if release!=RELEASES/manifest_id(value):fail('unexpected_pool_release')
    root=owner_root(value['unix_owner']);normal_home=absolute(str(normal_home));cap=value['unit_limits']
    mapping={'@USER@':value['unix_owner'],'@ROOT@':str(root),'@RELEASE@':str(release),
        '@PYTHON@':value['python']['path'],'@NORMAL_HOME@':str(normal_home),
        '@MEMORY@':str(cap['memory_bytes']),'@TASKS@':str(cap['tasks']),
        '@CPU@':str(cap['cpu_percent']),'@LIFETIME@':str(cap['lifetime_seconds']),
        '@MEMORY_HIGH@':str(cap['memory_high_bytes']),'@STOP@':str(cap['stop_seconds'])}
    for key,item in mapping.items():template=template.replace(key,item)
    if '@' in template:fail('invalid_pool_unit_template')
    return template


def render_continuation_unit(template,value,release,normal_home):
    if not continuation_supported(value['adapter_version']):fail('pool_continuation_version_required')
    template=template.replace('@CONTINUATION_SERVICE@',continuation_unit_name(value['unix_owner']))
    return render_unit(template,value,release,normal_home)
