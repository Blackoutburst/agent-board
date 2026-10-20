"""Source-only update of an existing, stopped owner installation.

No account, owner-private file, host journal or service activation is changed.
All releases and private recovery records are retained, including on refusal.
Linux renameat2 exchanges preserve unexpected unit inodes instead of unlinking
them through a check-then-replace race. There is no overwrite fallback.
"""
from contextlib import ExitStack
import ctypes
import errno
import hashlib
import os
from pathlib import Path
import platform
import pwd
import re
import stat
import uuid

from . import manage
from .package import (BASE, RELEASES, UNIT_DIR, OWNER_NAMES, MAX_FILE, MAX_TOTAL,
    PackageError, absolute, decode, elf, encoded, home, manifest_id, render_unit,
    unit_name, validate_manifest)

ROOT_UID=0
LEGACY_TEMPLATE='5a2af330567acf3db72458aef1d79e61d9ad19f490e6f3c637fd71178858dec0'
MANAGED_TEMPLATE='8070ba245d60251468d89d458ad8718222ec04a1949d67f7fd86d7fbe0103045'
SYSTEMD_ROOTS=tuple(map(Path,('/etc/systemd/system','/run/systemd/system',
    '/usr/lib/systemd/system','/run/systemd/generator','/run/systemd/generator.early',
    '/run/systemd/generator.late','/run/systemd/transient')))
MAX_ENTRIES=10000


def fail(code):raise PackageError(code)
def sha(data):return hashlib.sha256(data).hexdigest()
def identity(info):return (info.st_dev,info.st_ino,stat.S_IFMT(info.st_mode))
def directory_pin(info):return (*identity(info),info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode))
def file_pin(info):return (*directory_pin(info),info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def trusted(info):
    if info.st_uid!=ROOT_UID or info.st_mode&0o022:fail('untrusted_installation')


def leaf(name):
    if (type(name) is not str or not name or '/' in name or name in ('.','..')
            or any(ord(char)<32 for char in name)):
        fail('invalid_update_leaf')
    try:
        if len(name.encode('utf-8','strict'))>255:fail('invalid_update_leaf')
    except UnicodeError:fail('invalid_update_leaf')
    return name


class Directory:
    """Retain every directory descriptor from /; never follow an ancestor."""
    def __init__(self,path,*,protect=True):
        self.path=Path(path);self.nodes=[];self.protect=protect
        # Bundle/operator arguments keep package.absolute's closed grammar.
        # Traversal of public systemd directory entries must also handle real
        # template names such as user@.service.d without following links.
        if not self.path.is_absolute() or '..' in self.path.parts or len(str(self.path))>4096:
            fail('invalid_update_directory')
        for name in self.path.parts[1:]:leaf(name)
        flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC
        try:
            fd=os.open('/',flags);info=os.fstat(fd)
            self.nodes.append((fd,None,None,directory_pin(info)))
            if protect:trusted(info)
            for part in self.path.parts[1:]:
                parent=self.nodes[-1][0]
                fd=os.open(part,flags,dir_fd=parent);info=os.fstat(fd)
                self.nodes.append((fd,parent,part,directory_pin(info)))
                if protect:trusted(info)
            self.verify()
        except BaseException:
            self.close();raise

    @property
    def fd(self):return self.nodes[-1][0]
    def __enter__(self):return self
    def __exit__(self,*_):self.close()
    def verify(self):
        for fd,parent,name,pin in self.nodes:
            if directory_pin(os.fstat(fd))!=pin:fail('update_directory_changed')
            if parent is not None and directory_pin(os.stat(name,dir_fd=parent,follow_symlinks=False))!=pin:
                fail('update_ancestor_changed')
        return self
    def close(self):
        for fd,_parent,_name,pin in reversed(self.nodes):
            try:
                # An unknown/reused descriptor is never closed as cleanup.
                if identity(os.fstat(fd))==pin[:3]:os.close(fd)
            except OSError:pass
        self.nodes=[]
    def stat(self,name):
        self.verify();result=os.stat(leaf(name),dir_fd=self.fd,follow_symlinks=False);self.verify();return result
    def exists(self,name):
        try:self.stat(name);return True
        except FileNotFoundError:return False
    def read(self,name,*,maximum=MAX_FILE,owner=None,mode=None):
        self.verify();name=leaf(name);fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=self.fd)
        pin=None
        try:
            info=os.fstat(fd);pin=file_pin(info)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or not 0<=info.st_size<=maximum
                    or owner is not None and info.st_uid!=owner
                    or mode is not None and stat.S_IMODE(info.st_mode)!=mode):fail('update_file_untrusted')
            chunks=[];total=0
            while True:
                part=os.read(fd,min(1024*1024,maximum+1-total))
                if not part:break
                chunks.append(part);total+=len(part)
                if total>maximum:fail('update_file_too_large')
            if file_pin(os.fstat(fd))!=pin or file_pin(self.stat(name))!=pin:fail('update_file_changed')
            return b''.join(chunks),pin
        finally:
            if pin is not None and identity(os.fstat(fd))==pin[:3]:os.close(fd)
    def new(self,name,data,mode=0o600):
        self.verify();name=leaf(name)
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,mode,dir_fd=self.fd)
        pin=identity(os.fstat(fd))
        try:
            view=memoryview(data)
            while view:
                count=os.write(fd,view)
                if count<=0:fail('update_write_failed')
                view=view[count:]
            os.fchmod(fd,mode);os.fsync(fd)
            result=file_pin(os.fstat(fd))
            if file_pin(self.stat(name))!=result:fail('update_file_changed')
            os.fsync(self.fd);return result
        finally:
            if identity(os.fstat(fd))==pin:os.close(fd)
    def mkdir(self,name,mode=0o700):
        self.verify();os.mkdir(leaf(name),mode,dir_fd=self.fd);os.fsync(self.fd);self.verify()
        return Directory(self.path/name,protect=self.protect)


class Bundle:
    def __init__(self,path,expected,*,installed=False):
        self.stack=ExitStack();self.directories={};self.entries={};self.pins={};self.data={};self.path=absolute(str(path));self.installed=installed
        try:
            self.root=self.stack.enter_context(Directory(self.path,protect=installed));self.directories['']=self.root
            raw,pin=self.root.read('manifest.json',maximum=256*1024,owner=ROOT_UID if installed else None,
                mode=0o444 if installed else None)
            self.manifest=validate_manifest(decode(raw));self.pins['manifest.json']=pin;self.data['manifest.json']=raw
            if manifest_id(self.manifest)!=expected:fail('update_manifest_identity_changed')
            names=set(self.manifest['files'])|{'manifest.json'};dirs={''}
            for name in names:
                dirs.update(str(parent) for parent in Path(name).parents if str(parent)!='.')
            if len(names)+len(dirs)>1200:fail('update_inventory_bound')
            for name in sorted(dirs-{''},key=lambda s:(s.count('/'),s)):
                self.directories[name]=self.stack.enter_context(Directory(self.path/name,protect=installed))
            for name,directory in self.directories.items():
                expected_names={Path(item).relative_to(name).parts[0] if name else Path(item).parts[0]
                    for item in names if not name or item.startswith(name+'/')}
                self.entries[name]=expected_names
                found=set()
                with os.scandir(directory.fd) as entries:
                    for entry in entries:
                        if entry.name not in expected_names or entry.name in found:fail('update_inventory_changed')
                        found.add(entry.name)
                        if len(found)>1200:fail('update_inventory_bound')
                directory.verify()
                if found!=expected_names:fail('update_inventory_changed')
            total=0
            for name,metadata in self.manifest['files'].items():
                parent=str(Path(name).parent);directory=self.directories['' if parent=='.' else parent]
                data,pin=directory.read(Path(name).name,maximum=metadata['size'],
                    owner=ROOT_UID if installed else None,
                    mode=(0o755 if name.startswith('tools/') else 0o444) if installed else None)
                if len(data)!=metadata['size'] or sha(data)!=metadata['sha256']:fail('update_payload_pin_changed')
                if name.startswith('tools/'):elf(data,self.manifest['architecture'])
                total+=len(data)
                if total>MAX_TOTAL:fail('update_inventory_bound')
                self.data[name]=data;self.pins[name]=pin
            self.verify()
        except BaseException:self.close();raise
    def __enter__(self):return self
    def __exit__(self,*_):self.close()
    def close(self):self.stack.close()
    def verify(self):
        for name,directory in self.directories.items():
            directory.verify();found=set()
            with os.scandir(directory.fd) as entries:
                for entry in entries:
                    if entry.name not in self.entries[name] or entry.name in found:fail('update_inventory_changed')
                    found.add(entry.name)
                    if len(found)>1200:fail('update_inventory_bound')
            directory.verify()
            if found!=self.entries[name]:fail('update_inventory_changed')
        for name,pin in self.pins.items():
            parent=str(Path(name).parent);directory=self.directories['' if parent=='.' else parent]
            if file_pin(directory.stat(Path(name).name))!=pin:fail('update_payload_changed')


class Runtime:
    def __init__(self,manifest):
        path=Path(manifest['python']['path']);self.name=path.name;self.pins={}
        if path.parent!=Path('/usr/bin'):fail('explicit_system_python_required')
        self.directory=Directory(path.parent)
        try:
            data,self.pin=self.directory.read(self.name,owner=ROOT_UID)
            elf(data,manifest['architecture'])
            if sha(data)!=manifest['python']['sha256']:fail('python_pin_changed')
            self.pins[self.name]=self.pin
            systemctl,pin=self.directory.read('systemctl',maximum=8*1024*1024,owner=ROOT_UID,mode=0o755)
            elf(systemctl,manifest['architecture']);self.pins['systemctl']=pin
        except BaseException:self.directory.close();raise
    def __enter__(self):return self
    def __exit__(self,*_):self.directory.close()
    def verify(self):
        self.directory.verify()
        for name,pin in self.pins.items():
            if file_pin(self.directory.stat(name))!=pin:fail('system_runtime_pin_changed')


def _ids(old,new):
    if any(type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value) for value in (old,new)) or old==new:
        fail('full_distinct_release_ids_required')


def _compatible(old,new):
    for key in ('architecture','python','codex_version','board_endpoint','owners'):
        if old.manifest[key]!=new.manifest[key]:fail('update_runtime_or_owner_changed')
    for name in ('tools/codex','tools/codex-resources/bwrap'):
        if old.manifest['files'][name]!=new.manifest['files'][name]:fail('update_tools_changed')
    if sha(old.data['unit.service.in']) not in (LEGACY_TEMPLATE,MANAGED_TEMPLATE):fail('old_template_not_recognized')
    if sha(new.data['unit.service.in'])!=MANAGED_TEMPLATE:fail('managed_template_required')


def _receipt(directory,old_id):
    raw,pin=directory.read(old_id+'.json',maximum=16384,owner=ROOT_UID,mode=0o600)
    value=decode(raw)
    if (type(value) is not dict or set(value)!={'schema_version','bundle_id','status','created_accounts','written_units','activated'}
            or type(value['schema_version']) is not int or value['schema_version']!=1 or value['bundle_id']!=old_id
            or value['status']!='installed_disabled' or value['activated'] is not False
            or type(value['created_accounts']) is not list or len(value['created_accounts'])!=2
            or value['written_units']!=[unit_name(name) for name in OWNER_NAMES]):fail('old_installation_receipt_unconfirmed')
    accounts={}
    for item in value['created_accounts']:
        if (type(item) is not dict or set(item)!={'name','uid','gid'} or item['name'] not in OWNER_NAMES
                or item['name'] in accounts or any(type(item[k]) is not int or not 1<=item[k]<2**31 for k in ('uid','gid'))):
            fail('old_installation_receipt_unconfirmed')
        accounts[item['name']]=item
    if len({a['uid'] for a in accounts.values()})!=2 or len({a['gid'] for a in accounts.values()})!=2:
        fail('distinct_unix_owners_required')
    return value,accounts,pin


def _accounts(accounts):
    observed={}
    with Directory(BASE/'owners') as owners:
        for name in OWNER_NAMES:
            account=pwd.getpwnam(name);expected=accounts[name];info=owners.stat(name)
            if (account.pw_name!=name or account.pw_uid!=expected['uid'] or account.pw_gid!=expected['gid'] or account.pw_dir!=str(home(name))
                    or account.pw_shell!='/bin/bash' or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid!=expected['uid'] or info.st_gid!=expected['gid'] or stat.S_IMODE(info.st_mode)!=0o700):
                fail('existing_owner_identity_changed')
            observed[name]={'uid':account.pw_uid,'gid':account.pw_gid,'home':account.pw_dir,
                'shell':account.pw_shell,'home_identity':directory_pin(info)}
    return observed


def _services():
    fields={'LoadState':'loaded','ActiveState':'inactive','SubState':'dead','UnitFileState':'disabled',
        'MainPID':'0','ControlPID':'0','DropInPaths':''}
    properties=','.join((*fields,'FragmentPath','Names'))
    for name in OWNER_NAMES:
        unit=unit_name(name)
        raw=manage.command(['/usr/bin/systemctl','show','--no-pager','--property='+properties,unit])
        if len(raw)>8192:fail('unit_metadata_unconfirmed')
        values={}
        for line in raw.decode('utf-8','strict').splitlines():
            if '=' not in line:fail('unit_metadata_unconfirmed')
            key,value=line.split('=',1)
            if key in values:fail('unit_metadata_unconfirmed')
            values[key]=value
        if (set(values)!=set((*fields,'FragmentPath','Names')) or any(values[k]!=v for k,v in fields.items())
                or values['FragmentPath']!=str(UNIT_DIR/unit) or values['Names']!=unit):
            fail('units_must_be_inactive_dead_disabled_unmodified')


def _links():
    wanted={unit_name(name) for name in OWNER_NAMES};seen=0
    for root in SYSTEMD_ROOTS:
        if not root.exists():
            if root.is_symlink():fail('unknown_unit_link')
            continue
        queue=[root]
        while queue:
            path=queue.pop()
            if len(path.relative_to(root).parts)>8:fail('unit_inventory_bound')
            with Directory(path) as directory:
                with os.scandir(directory.fd) as entries:
                    for entry in entries:
                        seen+=1
                        if seen>MAX_ENTRIES:fail('unit_inventory_bound')
                        info=directory.stat(entry.name)
                        if stat.S_ISLNK(info.st_mode):
                            target=os.readlink(entry.name,dir_fd=directory.fd)
                            if entry.name in wanted or Path(target).name in wanted:fail('unknown_unit_link')
                        elif entry.name in wanted and path!=UNIT_DIR:fail('unknown_unit_override')
                        elif stat.S_ISDIR(info.st_mode):queue.append(path/entry.name)
                directory.verify()


def _rename(source_fd,source,destination_fd,destination,flags):
    # This function is mocked in pure tests. No unsupported-kernel fallback can
    # overwrite a unit after a non-atomic pathname check.
    libc=ctypes.CDLL(None,use_errno=True)
    try:function=libc.renameat2
    except AttributeError:fail('atomic_unit_exchange_unavailable')
    function.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    function.restype=ctypes.c_int
    if function(source_fd,leaf(source).encode(),destination_fd,leaf(destination).encode(),flags)!=0:
        value=ctypes.get_errno()
        if value in (errno.ENOSYS,errno.EINVAL,errno.EOPNOTSUPP):fail('atomic_unit_exchange_unavailable')
        raise OSError(value,'atomic_unit_rename_failed')


def _swap_unit(units,name,expected_pin,expected_bytes,data,operation,index,backup):
    temporary='.bb-host-'+operation+'-'+str(index)
    new_pin=units.new(temporary,data,0o644)
    if file_pin(units.stat(name))!=expected_pin:fail('unit_changed_before_exchange')
    units.verify();backup.verify();_rename(units.fd,temporary,units.fd,name,2);os.fsync(units.fd)
    actual=file_pin(units.stat(temporary))
    # renameat2 legitimately changes ctime. All other fields, held inode and
    # exact public bytes must still match; ctime is pinned again afterwards.
    current=file_pin(units.stat(name))
    if actual[:-1]!=expected_pin[:-1]:
        # Preserve the unexpected inode. Undo this exchange only while both
        # observed leaves still match; a second interference stays inspectable.
        if current[:-1]==new_pin[:-1] and file_pin(units.stat(temporary))==actual:
            _rename(units.fd,temporary,units.fd,name,2);os.fsync(units.fd)
        fail('unit_compare_exchange_lost')
    if current[:-1]!=new_pin[:-1]:fail('unit_changed_after_exchange')
    if units.read(temporary,maximum=16384,owner=ROOT_UID,mode=0o644)[0]!=expected_bytes:
        fail('unit_bytes_changed_during_exchange')
    data_after,pin_after=units.read(name,maximum=16384,owner=ROOT_UID,mode=0o644)
    if data_after!=data:fail('unit_changed_after_exchange')
    _rename(units.fd,temporary,backup.fd,'retained-'+str(index),1)
    os.fsync(units.fd);os.fsync(backup.fd);units.verify();backup.verify()
    return pin_after


def _plan(bundle,old_id,new_id):
    return {'status':'update_plan','old_release':old_id,'new_release':new_id,'unit_mode':'managed',
        'actions':['verify_existing_inactive_disabled_installation','retain_private_public_unit_backup',
            'install_new_immutable_public_release','exchange_exact_known_units','daemon_reload_only'],
        'activated':False,'execution_qualified':False,'private_owner_state_accessed':False}


def plan_update(directory,*,expected_old_release,expected_new_release):
    _ids(expected_old_release,expected_new_release)
    with Bundle(directory,expected_new_release) as bundle:
        if sha(bundle.data['unit.service.in'])!=MANAGED_TEMPLATE:fail('managed_template_required')
        return _plan(bundle,expected_old_release,expected_new_release)


def _new_release(releases,new_id,bundle):
    with releases.mkdir(new_id) as created:
        dirs={''}
        for name in bundle.data:
            dirs.update(str(p) for p in Path(name).parents if str(p)!='.')
        for name in sorted(dirs-{''},key=lambda s:(s.count('/'),s)):
            with Directory(created.path/Path(name).parent) as parent:
                with parent.mkdir(Path(name).name):pass
        for name,data in bundle.data.items():
            with Directory(created.path/Path(name).parent) as parent:
                parent.new(Path(name).name,data,0o755 if name.startswith('tools/') else 0o444)
        # Each fd is still owned and pinned during its explicit chmod. Reopen
        # after the deliberate mode transition rather than adopting a new path.
        for name in sorted(dirs,key=lambda s:(s.count('/'),s),reverse=True):
            with Directory(created.path/name) as parent:
                parent.verify();os.fchmod(parent.fd,0o555);os.fsync(parent.fd)


def update_existing(directory,*,expected_old_release,expected_new_release):
    _ids(expected_old_release,expected_new_release)
    if os.geteuid()!=0:fail('administrator_required')
    if platform.system()!='Linux':fail('linux_host_required')
    with ExitStack() as stack:
        base=stack.enter_context(Directory(BASE));releases=stack.enter_context(Directory(RELEASES))
        installations=stack.enter_context(Directory(BASE/'installations'));units=stack.enter_context(Directory(UNIT_DIR))
        lockfd=os.open('.install.lock',os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=base.fd)
        lockpin=file_pin(os.fstat(lockfd))
        try:
            import fcntl
            info=os.fstat(lockfd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=ROOT_UID or stat.S_IMODE(info.st_mode)!=0o600:
                fail('insecure_install_lock')
            try:fcntl.flock(lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:fail('installation_in_progress')
            if file_pin(base.stat('.install.lock'))!=lockpin:fail('install_lock_changed')
            old=stack.enter_context(Bundle(RELEASES/expected_old_release,expected_old_release,installed=True))
            new=stack.enter_context(Bundle(directory,expected_new_release));_compatible(old,new)
            if platform.machine()!=old.manifest['architecture']:fail('linux_host_mismatch')
            receipt,accounts,receipt_pin=_receipt(installations,expected_old_release)
            if releases.exists(expected_new_release) or installations.exists(expected_new_release+'.json'):
                fail('new_release_or_receipt_already_exists')
            runtime=stack.enter_context(Runtime(old.manifest))
            account_baseline=_accounts(accounts)
            def check_accounts():
                if _accounts(accounts)!=account_baseline:fail('existing_owner_identity_changed')
            expected_units={};new_units={};pins={};installed_new=None
            for username in OWNER_NAMES:
                name=unit_name(username)
                expected_units[name]=render_unit(old.data['unit.service.in'].decode(),old.manifest,old.path,username).encode()
                new_units[name]=render_unit(new.data['unit.service.in'].decode(),new.manifest,RELEASES/expected_new_release,username).encode()
                data,pins[name]=units.read(name,maximum=16384,owner=ROOT_UID,mode=0o644)
                if data!=expected_units[name]:fail('old_unit_bytes_changed')
            def recheck(expected):
                base.verify();releases.verify();installations.verify();units.verify();old.verify();new.verify();runtime.verify()
                if installed_new is not None:installed_new.verify()
                if file_pin(base.stat('.install.lock'))!=lockpin:fail('install_lock_changed')
                if file_pin(installations.stat(expected_old_release+'.json'))!=receipt_pin:fail('old_installation_receipt_changed')
                check_accounts();_services();_links()
                for name,pin in expected.items():
                    if file_pin(units.stat(name))!=pin:fail('unit_changed_during_update')
            recheck(pins)
            if not base.exists('updates'):
                with base.mkdir('updates'):pass
            updates=stack.enter_context(Directory(BASE/'updates'))
            if stat.S_IMODE(os.fstat(updates.fd).st_mode)!=0o700:fail('backup_root_not_private')
            operation=str(uuid.uuid4());backup=stack.enter_context(updates.mkdir(operation))
            backup.new('plan.json',encoded(_plan(new,expected_old_release,expected_new_release)),0o400)
            for index,name in enumerate(expected_units):backup.new('old-'+str(index),expected_units[name],0o400)
            written=[];event=0;receipt_attempted=False
            def record(phase,**values):
                nonlocal event
                event+=1
                if event>64:fail('update_event_bound')
                backup.new('event-'+str(event).zfill(3)+'.json',encoded({'phase':phase,**values}),0o400)
            try:
                record('release_intent');recheck(pins);_new_release(releases,expected_new_release,new)
                installed_new=stack.enter_context(Bundle(RELEASES/expected_new_release,expected_new_release,installed=True))
                record('release_verified')
                for index,name in enumerate(expected_units):
                    recheck(pins);record('unit_exchange_intent',unit=name)
                    pin=_swap_unit(units,name,pins[name],expected_units[name],new_units[name],operation,index,backup)
                    written.append((name,pin));pins[name]=pin;record('unit_exchanged',unit=name)
                recheck(pins);manage.command(['/usr/bin/systemctl','daemon-reload']);recheck(pins)
                record('units_reloaded_inactive_disabled')
                result={**_plan(new,expected_old_release,expected_new_release),'status':'updated_disabled',
                    'backup_id':operation,'services_confirmed_inactive_disabled':True}
                # This last durable receipt is the commit boundary. An I/O
                # exception during it is ambiguous: preserve both known states
                # for review instead of rolling units back under a possibly
                # committed new installation receipt.
                receipt_attempted=True
                installations.new(expected_new_release+'.json',encoded({**receipt,'bundle_id':expected_new_release}),0o600)
                recheck(pins)
                backup.new('result.json',encoded(result),0o400)
                recheck(pins)
                return result
            except BaseException:
                if receipt_attempted:
                    try:record('commit_receipt_uncertain_requires_review')
                    except BaseException:pass
                    raise PackageError('update_commit_requires_review') from None
                rollback=True
                try:
                    base.verify();units.verify();old.verify();runtime.verify();_services();check_accounts();_links()
                    for index,(name,pin) in enumerate(reversed(written)):
                        restored=_swap_unit(units,name,pin,new_units[name],expected_units[name],operation,20+index,backup)
                        pins[name]=restored
                    if written:manage.command(['/usr/bin/systemctl','daemon-reload'])
                    for name in expected_units:
                        data,_=units.read(name,maximum=16384,owner=ROOT_UID,mode=0o644)
                        if data!=expected_units[name]:fail('rollback_unit_unconfirmed')
                    runtime.verify();_services();check_accounts()
                except BaseException:rollback=False
                try:record('failed_review_required',recognized_units_restored=rollback)
                except BaseException:pass
                raise PackageError('update_failed_restored_units_only' if rollback else 'update_failed_requires_review') from None
        finally:
            if identity(os.fstat(lockfd))==lockpin[:3]:os.close(lockfd)
