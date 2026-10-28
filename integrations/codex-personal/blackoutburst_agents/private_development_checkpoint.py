"""Private Git source checkpoints, never tested delivery or future authority.

Only a live DevelopmentCheckpointProof can first capture a source. Historical
metadata can be re-read but cannot claim, clone, execute, publish or resume work.
Interrupted intents are retained and never retried or adopted automatically.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import time
import unicodedata
import uuid

from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, canonical, digest, relative_path, sha256, signature, check_content, opaque
from .git_delivery import _inventory, _commit_bytes, object_id
from .git_workspace import (LocalGitWorkspaceController, MAX_CHECKOUT_BYTES, MAX_CHECKOUT_FILE_BYTES, MAX_CHECKOUT_FILES,
    _ancestry, _commit, _directory, _operation, _path, _scope, _file)

SCHEMA='private-development-checkpoint-record-v1'
CONFIG_SCHEMA='private-development-checkpoint-config-v1'
SEAL_SCHEMA='private-development-checkpoint-v1'
MAX_RECORD=8*1024*1024


def fail(code='private_checkpoint_invalid'):
    raise DevelopmentError(code)


def copy(value):return decode(canonical(value))


def checkpoint_id_for(operation_id):
    """One slot per actual generation; a new key cannot escape an old intent."""
    return str(uuid.uuid5(uuid.UUID(_operation(operation_id)),'blackoutburst-private-development-checkpoint-v1'))


def _message(checkpoint_id,configuration_sha256,proof_sha256,parent_sha256):
    return ('Private development checkpoint '+checkpoint_id+'\n\nNot tested delivery or acceptance.\n'
        'Configuration SHA256: '+configuration_sha256+'\nProof SHA256: '+proof_sha256+'\n'
        'Parent checkpoint SHA256: '+('none' if parent_sha256 is None else parent_sha256))


def validate_configuration(value):
    fields={'schema','task_id','owner_id','project_id','ticket_id','repository_id','resource',
        'original_public_base','global_paths','global_test_caps_sha256','static_caps_sha256','source_release_sha256'}
    if type(value) is not dict or set(value)!=fields or value['schema']!=CONFIG_SCHEMA:fail('private_checkpoint_configuration_refused')
    for key in ('task_id','owner_id','project_id','ticket_id','repository_id','resource'):opaque(value[key])
    _commit(value['original_public_base'])
    for key in ('global_test_caps_sha256','static_caps_sha256','source_release_sha256'):sha256(value[key])
    paths=value['global_paths']
    if type(paths) is not list or not 1<=len(paths)<=128:fail('private_checkpoint_configuration_refused')
    aliases=set()
    for path in paths:
        relative_path(path);alias=unicodedata.normalize('NFC',path).casefold()
        if alias in aliases:fail('private_checkpoint_configuration_refused')
        aliases.add(alias)
    if paths!=sorted(paths) or len(canonical(value))>32768:fail('private_checkpoint_configuration_refused')
    return copy(value)


class PrivateDevelopmentCheckpointController:
    def __init__(self,workspaces,checkpoint_root,*,configuration):
        if type(workspaces) is not LocalGitWorkspaceController:fail('private_checkpoint_controller_required')
        self.workspaces=workspaces;self.configuration=validate_configuration(configuration)
        self.configuration_sha256=digest(canonical(self.configuration))
        self.root=_path(checkpoint_root);self.pin=_directory(self.root,private=True)
        grant=workspaces.grant
        if (grant['owner_id']!=self.configuration['owner_id'] or grant['repository_id']!=self.configuration['repository_id']):
            fail('private_checkpoint_configuration_refused')
        self._roots()

    def _roots(self):
        self.workspaces._roots()
        if (validate_configuration(self.configuration)!=self.configuration
                or digest(canonical(self.configuration))!=self.configuration_sha256):
            fail('private_checkpoint_configuration_changed')
        if _directory(self.root,private=True)!=self.pin:fail('private_checkpoint_root_changed')
        roots=(self.root,self.workspaces.repository,self.workspaces.state_root,self.workspaces.workspace_root)
        chains=[_ancestry(path) for path in roots]
        if any(a[-1] in b or b[-1] in a for n,a in enumerate(chains) for b in chains[n+1:]):
            fail('private_checkpoint_roots_overlap')

    @contextmanager
    def _record(self,checkpoint_id,*,create=False):
        self._roots();checkpoint_id=_operation(checkpoint_id);path=self.root/checkpoint_id;fresh=False
        if create:
            try:
                os.mkdir(path,0o700)
                with PrivateDirectory(self.root) as parent:os.fsync(parent.fd)
                fresh=True
            except FileExistsError:pass
        with PrivateDirectory(path) as private:
            flags=os.O_RDWR|os.O_NOFOLLOW
            if fresh:flags|=os.O_CREAT|os.O_EXCL
            try:fd=os.open('checkpoint.lock',flags,0o600,dir_fd=private.fd)
            except OSError:fail('private_checkpoint_lock_missing_or_changed')
            try:
                private.validate_file(fd)
                pin=signature(os.fstat(fd))
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:fail('private_checkpoint_busy')
                if signature(os.stat('checkpoint.lock',dir_fd=private.fd,follow_symlinks=False))!=pin:
                    fail('private_checkpoint_lock_missing_or_changed')
                yield private,fresh
                private.verify()
                if (signature(os.fstat(fd))!=pin
                        or signature(os.stat('checkpoint.lock',dir_fd=private.fd,follow_symlinks=False))!=pin):
                    fail('private_checkpoint_lock_missing_or_changed')
            finally:os.close(fd)

    def _workspace(self,private,operation_id):
        source=self.workspaces._read(private)
        if source['phase']!='ready' or source['request']['operation_id']!=operation_id:fail('private_checkpoint_workspace_changed')
        self.workspaces._ready(source)
        root=Path(source['request']['checkout'])
        if self.workspaces._git(['diff-index','--cached','--raw','--no-ext-diff',self.workspaces.grant['base_commit'],'--'],cwd=root):
            fail('private_checkpoint_index_changed')
        return source,root

    @staticmethod
    def _anchors(root):
        result={}
        for name in ('HEAD','index','config'):
            path=root/'.git'/name;before=signature(path.lstat());raw=_file(path,16*1024*1024)
            if signature(path.lstat())!=before:fail('private_checkpoint_head_or_index_changed')
            result[name]={'sha256':digest(raw),'signature':list(before)}
        return result

    def _base_entries(self,root,base):
        raw=self.workspaces._git(['ls-tree','-r','-z','--full-tree',base],cwd=root);result={}
        if not raw or not raw.endswith(b'\0'):fail('private_checkpoint_base_changed')
        for entry in raw[:-1].split(b'\0'):
            head,path=entry.split(b'\t',1);mode,kind,oid=head.split();path=path.decode('utf-8','strict');relative_path(path)
            if kind!=b'blob' or mode not in (b'100644',b'100755'):fail('private_checkpoint_base_changed')
            result[path]=(mode.decode(),oid.decode())
        return result

    def _snapshot(self,root,observation):
        files,_pins=_inventory(root);base=self.workspaces.grant['base_commit'];before=self._base_entries(root,base)
        selected=observation['selected_postimages']
        if (set(files)!=set(before) or set(selected)!=set(self.configuration['global_paths'])
                or observation['selected_paths']!=self.configuration['global_paths']):fail('private_checkpoint_undeclared_change')
        changes=[]
        for path,(mode,data) in files.items():
            if mode!=before[path][0]:fail('private_checkpoint_mode_changed')
            if path in selected:
                sha256(selected[path])
                if digest(data)!=selected[path]:fail('private_checkpoint_postimage_changed')
            if object_id(data,'blob',base)!=before[path][1]:
                if path not in selected:fail('private_checkpoint_undeclared_change')
                check_content(data);changes.append(path)
        entries=[dict(path=path,mode=mode,bytes=len(data),sha256=digest(data),blob_oid=object_id(data,'blob',base),payload='file-'+str(n))
            for n,(path,(mode,data)) in enumerate(sorted(files.items()))]
        return files,entries,sorted(changes)

    def _proof_caps(self,observation):
        c=self.configuration
        if (type(observation) is not dict or observation.get('task_id')!=c['task_id']
                or observation.get('schema')!='development-checkpoint-observation-v1'
                or observation.get('passed_tests_promoted') is not False
                or observation.get('resource')!=c['resource']
                or observation.get('original_public_base')!=c['original_public_base']
                or observation.get('source_release_sha256')!=c['source_release_sha256']
                or observation.get('test_caps_sha256')!=c['global_test_caps_sha256']
                or observation.get('static_caps_sha256')!=c['static_caps_sha256']
                or observation.get('selected_paths')!=c['global_paths']
                or type(observation.get('selected_postimages')) is not dict
                or set(observation['selected_postimages'])!=set(c['global_paths'])):fail('private_checkpoint_proof_changed')
        scope=_scope(observation.get('scope'))
        if any(scope[key]!=c[key] for key in ('owner_id','project_id','ticket_id')):fail('private_checkpoint_scope_changed')
        for key in ('operation_id','work_id','generation_id','cycle_id','attempt_id'):_operation(observation[key])
        for key in ('manifest_sha256','configuration_sha256','planner_document_sha256','broker_document_sha256',
                    'worker_operation_sha256','runtime_cleanup_sha256','source_snapshot_sha256','work_state_sha256','model_results_sha256'):
            sha256(observation[key])
        for value in observation['selected_postimages'].values():sha256(value)
        if observation['source_snapshot_sha256']!=digest(canonical(observation['selected_postimages'])):
            fail('private_checkpoint_proof_changed')
        _commit(observation['current_base_commit'])

    def _binding(self,observation,source,root,parent):
        self._proof_caps(observation);c=self.configuration
        if (observation.get('current_base_commit')!=self.workspaces.grant['base_commit']
                or observation.get('root')!=str(root) or observation.get('root_identity')!=_directory(root,private=True)
                or observation.get('operation_id')!=source['request']['operation_id']
                or _scope(observation.get('scope'))!=source['request']['scope']):fail('private_checkpoint_proof_changed')
        expected=None if parent is None else parent['checkpoint_sha256']
        if observation.get('parent_checkpoint_sha256')!=expected:fail('private_checkpoint_parent_changed')
        base=self.workspaces.grant['base_commit']
        if parent is None:
            if base!=c['original_public_base']:fail('private_checkpoint_parent_required')
        elif (base!=parent['commit_sha'] or str(self.workspaces.repository)!=parent['checkout']
                or parent['configuration_sha256']!=self.configuration_sha256
                or self.verify(parent['checkpoint_id'])!=parent):fail('private_checkpoint_parent_changed')
        if self.workspaces._git(['cat-file','-t',c['original_public_base']],cwd=root)!=b'commit\n':fail('private_checkpoint_base_changed')
        self.workspaces._git(['merge-base','--is-ancestor',c['original_public_base'],base],cwd=root)

    def _write_object(self,private,root,data,kind):
        name='object-'+digest(kind.encode()+b'\0'+data)
        try:private.create(name,data)
        except DevelopmentError as error:
            if error.code!='private_file_exists' or private.read(name,MAX_CHECKOUT_BYTES)!=data:raise
        expected=object_id(data,kind,self.workspaces.grant['base_commit'])
        actual=self.workspaces._git(['hash-object','-w','--no-filters','-t',kind,'--',str(private.path/name)],cwd=root).strip().decode()
        if actual!=expected:fail('private_checkpoint_object_changed')
        return actual

    def _tree(self,private,root,entries):
        tree={}
        for row in entries:
            node=tree
            for part in row['path'].split('/')[:-1]:node=node.setdefault(part,{})
            raw=private.read(row['payload'],MAX_CHECKOUT_FILE_BYTES)
            if digest(raw)!=row['sha256'] or len(raw)!=row['bytes']:fail('private_checkpoint_payload_changed')
            oid=self._write_object(private,root,raw,'blob')
            if oid!=row['blob_oid']:fail('private_checkpoint_object_changed')
            node[row['path'].split('/')[-1]]=(row['mode'],oid)
        def write(node):
            raw=b''
            for name in sorted(node,key=lambda n:n.encode()+(b'/' if type(node[n]) is dict else b'')):
                item=node[name];mode,oid=('40000',write(item)) if type(item) is dict else item
                raw+=mode.encode()+b' '+name.encode()+b'\0'+bytes.fromhex(oid)
            return self._write_object(private,root,raw,'tree')
        return write(tree)

    def _read(self,private):
        value=decode(private.read('checkpoint.json',MAX_RECORD))
        if (type(value) is not dict or set(value)!={'schema','phase','intent','snapshot','seal','checkpoint_sha256'}
                or value['schema']!=SCHEMA or value['phase'] not in ('capturing','committing','sealed')
                or type(value['intent']) is not dict or value['intent'].get('checkpoint_id')!=private.path.name
                or value['intent'].get('configuration')!=self.configuration
                or value['intent'].get('configuration_sha256')!=self.configuration_sha256):fail('private_checkpoint_record_changed')
        fields={'checkpoint_id','operation_id','configuration','configuration_sha256','proof','checkout','anchors',
            'workspace_request_sha256','parent_checkpoint_id','parent_checkpoint_sha256','changes','epoch','message'}
        if set(value['intent'])!=fields:fail('private_checkpoint_record_changed')
        if checkpoint_id_for(value['intent']['operation_id'])!=private.path.name:fail('private_checkpoint_record_changed')
        _path(value['intent']['checkout'])
        return value

    def _sealed(self,private,value):
        if value['phase']!='sealed':fail('private_checkpoint_interrupted_requires_review')
        seal=value['seal'];intent=value['intent'];snapshot=value['snapshot'];root=Path(intent['checkout'])
        self._proof_caps(intent['proof'])
        if _directory(root,private=True)!=intent['proof']['root_identity']:fail('private_checkpoint_source_changed')
        fields={'schema','checkpoint_id','configuration_sha256','operation_id','checkout','base_commit',
            'original_public_base','parent_checkpoint_id','parent_checkpoint_sha256','commit_sha','tree_sha','ref',
            'snapshot_sha256','source_snapshot_sha256','proof_sha256','publishable'}
        if (type(seal) is not dict or set(seal)!=fields or seal.get('schema')!=SEAL_SCHEMA or seal.get('publishable') is not False
                or seal.get('checkpoint_id')!=private.path.name or seal.get('configuration_sha256')!=self.configuration_sha256
                or seal['original_public_base']!=self.configuration['original_public_base']
                or seal['operation_id']!=intent['operation_id'] or seal['checkout']!=intent['checkout']
                or seal['base_commit']!=intent['proof']['current_base_commit']
                or seal['parent_checkpoint_id']!=intent['parent_checkpoint_id']
                or seal['parent_checkpoint_sha256']!=intent['parent_checkpoint_sha256']
                or seal['source_snapshot_sha256']!=intent['proof']['source_snapshot_sha256']
                or seal['proof_sha256']!=digest(canonical(intent['proof']))
                or value['checkpoint_sha256']!=digest(canonical(seal))
                or seal.get('snapshot_sha256')!=digest(canonical(snapshot))
                or seal.get('ref')!='refs/blackoutburst/private-checkpoints/'+digest(self.configuration['task_id'].encode())[:32]+'/'+private.path.name):
            fail('private_checkpoint_record_changed')
        for key in ('commit_sha','tree_sha','base_commit'):_commit(seal[key])
        if (seal['parent_checkpoint_id'] is None)!=(seal['parent_checkpoint_sha256'] is None):
            fail('private_checkpoint_parent_changed')
        if seal['parent_checkpoint_id'] is not None:
            _operation(seal['parent_checkpoint_id']);sha256(seal['parent_checkpoint_sha256'])
        if intent['message']!=_message(private.path.name,self.configuration_sha256,seal['proof_sha256'],seal['parent_checkpoint_sha256']):
            fail('private_checkpoint_record_changed')
        if self._anchors(root)!=intent['anchors']:fail('private_checkpoint_head_or_index_changed')
        if type(snapshot) is not list or not 1<=len(snapshot)<=MAX_CHECKOUT_FILES:fail('private_checkpoint_record_changed')
        paths=[];total=0
        for n,row in enumerate(snapshot):
            if (type(row) is not dict or set(row)!={'path','mode','bytes','sha256','blob_oid','payload'}
                    or row['mode'] not in ('100644','100755') or type(row['bytes']) is not int
                    or not 0<=row['bytes']<=MAX_CHECKOUT_FILE_BYTES or row['payload']!='file-'+str(n)):
                fail('private_checkpoint_record_changed')
            relative_path(row['path']);sha256(row['sha256']);_commit(row['blob_oid']);paths.append(row['path'])
            total+=row['bytes']
            if total>MAX_CHECKOUT_BYTES:fail('private_checkpoint_record_changed')
            raw=private.read(row['payload'],MAX_CHECKOUT_FILE_BYTES)
            if (digest(raw)!=row['sha256'] or len(raw)!=row['bytes']
                    or object_id(raw,'blob',seal['base_commit'])!=row['blob_oid']):fail('private_checkpoint_payload_changed')
        if paths!=sorted(set(paths)):fail('private_checkpoint_record_changed')
        raw_commit=_commit_bytes(seal['tree_sha'],seal['base_commit'],intent['message'],intent['epoch'])
        if (self.workspaces._git(['rev-parse','--verify',seal['ref']],cwd=root).strip().decode()!=seal['commit_sha']
                or self.workspaces._git(['cat-file','commit',seal['commit_sha']],cwd=root)!=raw_commit
                or object_id(raw_commit,'commit',seal['base_commit'])!=seal['commit_sha']):fail('private_checkpoint_ref_changed')
        expected=b''.join((row['mode']+' blob '+row['blob_oid']+'\t'+row['path']).encode()+b'\0'
            for row in sorted(snapshot,key=lambda row:row['path'].encode()))
        if self.workspaces._git(['ls-tree','-r','-z','--full-tree',seal['commit_sha']],cwd=root)!=expected:
            fail('private_checkpoint_tree_changed')
        files,_=_inventory(root)
        if [(p,m,len(data),digest(data)) for p,(m,data) in sorted(files.items())]!=[(r['path'],r['mode'],r['bytes'],r['sha256']) for r in snapshot]:
            fail('private_checkpoint_source_changed')
        if self.workspaces._git(['rev-parse','--verify','HEAD'],cwd=root).strip().decode()!=seal['base_commit']:
            fail('private_checkpoint_head_changed')
        if self._anchors(root)!=intent['anchors']:fail('private_checkpoint_head_or_index_changed')
        return {**copy(seal),'checkpoint_sha256':value['checkpoint_sha256'],'historical':True,'launchable':False}

    def verify(self,checkpoint_id):
        """Historical source-chain readback; never emits a permission/claim."""
        result=None;child=None;seen=set()
        while checkpoint_id is not None:
            if checkpoint_id in seen:fail('private_checkpoint_parent_changed')
            seen.add(checkpoint_id)
            with self._record(checkpoint_id) as (private,_):current=self._sealed(private,self._read(private))
            if result is None:result=current
            if child is not None and (child['parent_checkpoint_sha256']!=current['checkpoint_sha256']
                    or child['base_commit']!=current['commit_sha']):fail('private_checkpoint_parent_changed')
            child=current;checkpoint_id=current['parent_checkpoint_id']
        if child is None or child['base_commit']!=self.configuration['original_public_base']:
            fail('private_checkpoint_parent_changed')
        return result

    def capture(self,*,checkpoint_id,proof,parent_checkpoint_id=None):
        try:from .development_checkpoint_proof import DevelopmentCheckpointProof
        except ImportError:fail('private_checkpoint_producer_unavailable')
        if type(proof) is not DevelopmentCheckpointProof:fail('private_checkpoint_proof_required')
        checkpoint_id=_operation(checkpoint_id)
        if parent_checkpoint_id==checkpoint_id:fail('private_checkpoint_parent_changed')
        parent=None if parent_checkpoint_id is None else self.verify(parent_checkpoint_id)
        with proof.hold() as observation:
            operation_id=_operation(observation['operation_id'])
            if checkpoint_id!=checkpoint_id_for(operation_id):fail('private_checkpoint_alternative_key_refused')
            with self.workspaces._locked(operation_id) as (workspace,_),self._record(checkpoint_id,create=True) as (private,fresh):
                source,root=self._workspace(workspace,operation_id)
                self._binding(observation,source,root,parent)
                if not fresh:
                    value=self._read(private)
                    if (value['intent']['operation_id']!=operation_id or value['intent']['proof']!=observation
                            or value['intent']['parent_checkpoint_id']!=parent_checkpoint_id):fail('private_checkpoint_request_conflict')
                    proof.recheck(observation)
                    return self._sealed(private,value)
                files,entries,changes=self._snapshot(root,observation)
                metadata=self.workspaces._copy_metadata(root)
                epoch=int(time.time())
                message=_message(checkpoint_id,self.configuration_sha256,digest(canonical(observation)),
                    None if parent is None else parent['checkpoint_sha256'])
                intent={'checkpoint_id':checkpoint_id,'operation_id':operation_id,'configuration':copy(self.configuration),
                    'configuration_sha256':self.configuration_sha256,'proof':copy(observation),'checkout':str(root),
                    'anchors':self._anchors(root),
                    'workspace_request_sha256':source['request_sha256'],'parent_checkpoint_id':parent_checkpoint_id,
                    'parent_checkpoint_sha256':None if parent is None else parent['checkpoint_sha256'],
                    'changes':changes,'epoch':epoch,'message':message}
                value={'schema':SCHEMA,'phase':'capturing','intent':intent,'snapshot':entries,'seal':None,'checkpoint_sha256':None}
                # Exclusive durable intent precedes all Git object/ref effects.
                private.create('checkpoint.json',canonical(value))
                for row in entries:private.create(row['payload'],files[row['path']][1])
                if self._snapshot(root,observation)[1]!=entries or self.workspaces._copy_metadata(root)!=metadata:
                    fail('private_checkpoint_source_changed')
                proof.recheck(observation);self._binding(observation,source,root,parent)
                value['phase']='committing';private.replace('checkpoint.json',canonical(value))
                tree=self._tree(private,root,entries)
                raw=_commit_bytes(tree,self.workspaces.grant['base_commit'],message,epoch)
                commit=self._write_object(private,root,raw,'commit')
                proof.recheck(observation)
                self._binding(observation,source,root,parent)
                if self._snapshot(root,observation)[1]!=entries:fail('private_checkpoint_source_changed')
                if self._anchors(root)!=intent['anchors']:fail('private_checkpoint_head_or_index_changed')
                self.workspaces._ready(source)
                ref='refs/blackoutburst/private-checkpoints/'+digest(self.configuration['task_id'].encode())[:32]+'/'+checkpoint_id
                self.workspaces._git(['update-ref',ref,commit,'0'*len(commit)],cwd=root)
                self.workspaces._fsync_tree(root/'.git')
                self.workspaces._ready(source)
                if self._snapshot(root,observation)[1]!=entries:fail('private_checkpoint_source_changed')
                value['seal']={'schema':SEAL_SCHEMA,'checkpoint_id':checkpoint_id,'configuration_sha256':self.configuration_sha256,
                    'operation_id':operation_id,'checkout':str(root),'base_commit':self.workspaces.grant['base_commit'],
                    'original_public_base':self.configuration['original_public_base'],'parent_checkpoint_id':parent_checkpoint_id,
                    'parent_checkpoint_sha256':intent['parent_checkpoint_sha256'],'commit_sha':commit,'tree_sha':tree,'ref':ref,
                    'snapshot_sha256':digest(canonical(entries)),'source_snapshot_sha256':observation['source_snapshot_sha256'],
                    'proof_sha256':digest(canonical(observation)),'publishable':False}
                value['checkpoint_sha256']=digest(canonical(value['seal']));value['phase']='sealed'
                private.replace('checkpoint.json',canonical(value))
                return {**self._sealed(private,value),'historical':False}
