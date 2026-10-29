"""One explicit sealed delivery -> verified GitHub draft PR, never a merge.

Only normal official gh authentication is used. No credential file/token is
read by this module. All network destinations and argv are closed local code.
The publication grant and protected GitDelivery checker are not Board authority.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import sys
import time
from urllib.parse import quote

from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, canonical, check_content, digest, opaque
from .git_delivery import LocalGitDeliveryController
from .git_workspace import _ancestry, _directory, _file, _operation, _path

REPOSITORY = 'Blackoutburst/agent-board'
HOST = 'github.com'
REMOTE = 'https://github.com/Blackoutburst/agent-board.git'
API = 'repos/Blackoutburst/agent-board'
SCHEMA = 'github-draft-publication-v1'
MAX_RECORD = 65536
MAX_OUTPUT = 2 * 1024 * 1024
PHASES = {'planned','cloning','prepared','pushing','branch_confirmed','creating','complete'}
CONFIG = b'[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tfilemode = true\n\tlogallrefupdates = false\n'


def fail(code='git_publication_invalid'):
    raise DevelopmentError(code)


class _AuthorityRefusal(DevelopmentError):
    """A pre-launch local refusal, never an uncertain remote response."""


def sha(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{40}', value): fail('git_publication_sha_refused')
    return value


def copied(value):
    return decode(canonical(value))


def _development_authority_pair(authority, proof):
    """Closed concrete pairs; an aggregate never uses the generic no-authority path."""
    from .development_git_authority import DevelopmentGitPublicationAuthority
    from .development_delivery import DevelopmentDeliveryProof
    from .development_aggregate_delivery import (AggregateDevelopmentDeliveryProof,
        AggregateDevelopmentGitPublicationAuthority)
    if type(authority) is DevelopmentGitPublicationAuthority and type(proof) is DevelopmentDeliveryProof:
        return proof.worker is authority.worker and proof.operation_id == authority.operation_id
    return (type(authority) is AggregateDevelopmentGitPublicationAuthority
        and type(proof) is AggregateDevelopmentDeliveryProof and authority.controller is proof.controller
        and proof.controller.proof is proof and proof.worker is authority.worker
        and proof.operation_id == authority.operation_id)


def json_output(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: fail('git_publication_remote_invalid')
            result[key] = value
        return result
    try:
        return json.loads(raw.decode('utf-8','strict'), object_pairs_hook=pairs,
                          parse_constant=lambda _: fail('git_publication_remote_invalid'))
    except (ValueError,UnicodeError): fail('git_publication_remote_invalid')


@dataclass(frozen=True)
class GitHubPublicationGrant:
    owner_id: str
    repository: str
    base_branch: str
    base_commit: str
    host: str = HOST

    def document(self):
        opaque(self.owner_id)
        sha(self.base_commit)
        if self.repository != REPOSITORY or self.host != HOST: fail('git_publication_destination_refused')
        branch = self.base_branch
        if (type(branch) is not str or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._/-]{0,199}',branch)
                or '..' in branch or '//' in branch or any(p.startswith('.') or p.endswith(('.lock','.')) or p=='' for p in branch.split('/'))
                or branch.endswith(('.', '/'))): fail('git_publication_base_refused')
        return vars(self).copy()


class GitHubDraftPublisher:
    """Per-delivery lock only. Interrupted effects are read back, never repeated."""
    def __init__(self, deliveries, publication_root, *, grant, git_executable='/usr/bin/git',
                 gh_executable='/usr/bin/gh', timeout_seconds=60, python_executable=None, authority=None):
        if type(deliveries) is not LocalGitDeliveryController or type(grant) is not GitHubPublicationGrant:
            fail('git_publication_controller_required')
        self.deliveries, self.workspaces = deliveries, deliveries.workspaces
        self.root, self.git, self.gh = _path(publication_root), _path(git_executable), _path(gh_executable)
        self.python = _path(python_executable or Path(sys.executable).resolve())
        self.authority, self.authority_pin, self._mutation_authority = authority, None, None
        from .development_aggregate_delivery import AggregateDevelopmentDeliveryProof
        if type(deliveries.test_guard) is AggregateDevelopmentDeliveryProof and authority is None:
            fail('git_publication_authority_required')
        if authority is not None:
            # Lazy: the generic standalone publisher need not import the Board
            # worker. No callable/subclass/model-provided grant is accepted.
            proof = deliveries.test_guard
            if not _development_authority_pair(authority,proof):
                fail('git_publication_authority_required')
            self.authority_pin = authority.descriptor()
        self.guardian = _path(Path(__file__).absolute().with_name('git_publication_guardian.py'))
        self._grant, self.grant = grant, grant.document()
        if self.git != self.workspaces.git: fail('git_publication_tool_mismatch')
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 120: fail('git_publication_timeout_refused')
        self.timeout = timeout_seconds
        self.pins = self._pins()
        self._roots()

    def _pins(self):
        return {'root':_directory(self.root,private=True), 'git':self._tool(self.git), 'gh':self._tool(self.gh),
                'python':self._tool(self.python), 'guardian':digest(_file(self.guardian,32768))}

    @staticmethod
    def _tool(path):
        try:
            info = path.lstat()
            if not os.access(path,os.X_OK) or info.st_mode & 0o6000: fail('git_publication_tool_unavailable')
            return digest(_file(path,64*1024*1024,executable=True))
        except (OSError,DevelopmentError): fail('git_publication_tool_unavailable')

    def _roots(self):
        self.deliveries._roots()
        from .development_aggregate_delivery import AggregateDevelopmentDeliveryProof
        if type(self.deliveries.test_guard) is AggregateDevelopmentDeliveryProof and self.authority is None:
            fail('git_publication_authority_changed')
        if self.authority is not None:
            if not _development_authority_pair(self.authority,self.deliveries.test_guard):
                fail('git_publication_authority_changed')
        if ((self.authority is None) != (self.authority_pin is None)
                or self.authority is not None and self.authority.descriptor() != self.authority_pin):
            fail('git_publication_authority_changed')
        if (self.grant != self._grant.document()
                or self.pins != self._pins()
                or self.pins['git'] != self.workspaces.pins['git_sha256']): fail('git_publication_pin_changed')
        paths = (self.root,self.deliveries.delivery_root,self.deliveries.review_root,
                 self.workspaces.state_root,self.workspaces.workspace_root,self.workspaces.repository)
        chains = [_ancestry(path) for path in paths]
        if any(a[-1] in b or b[-1] in a for n,a in enumerate(chains) for b in chains[n+1:]):
            fail('git_publication_roots_overlap')
        for tool in (self.python,self.guardian):
            ancestry = _ancestry(tool.parent)
            if any(root[-1] in ancestry for root in chains): fail('git_publication_tool_location_refused')

    def _environment(self, *, git=False):
        # Preserve the existing user's normal gh configuration/keyring location.
        # Never replace HOME, inspect those files, or copy authentication bytes.
        env = {name:os.environ[name] for name in ('HOME','XDG_CONFIG_HOME','XDG_DATA_HOME',
            'XDG_STATE_HOME','XDG_RUNTIME_DIR','GH_CONFIG_DIR','DBUS_SESSION_BUS_ADDRESS') if name in os.environ}
        # Reject an obvious configuration redirection into model resources,
        # without opening normal configuration/credential files. Host account
        # configuration itself remains a trusted deployment prerequisite.
        location = env.get('GH_CONFIG_DIR')
        if location is None:
            parent = env.get('XDG_CONFIG_HOME') or (str(Path(env['HOME'])/'.config') if env.get('HOME') else None)
            location = str(Path(parent)/'gh') if parent else None
        if location is None: fail('git_publication_authentication_unavailable')
        config = _path(location)
        for root in (self.root,self.deliveries.delivery_root,self.deliveries.review_root,
                     self.workspaces.state_root,self.workspaces.workspace_root,self.workspaces.repository):
            if config.is_relative_to(root) or root.is_relative_to(config):
                fail('git_publication_authentication_location_refused')
        env.update(PATH='/usr/bin:/bin', LANG='C.UTF-8', LC_ALL='C.UTF-8', GH_HOST=HOST,
            GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1', GH_PAGER='cat', PAGER='cat')
        if git:
            options = {'core.hooksPath':'/dev/null','core.fsmonitor':'false','core.attributesFile':'/dev/null',
                'core.askPass':'','credential.helper':'','credential.interactive':'never',
                'credential.useHttpPath':'true','protocol.allow':'never','protocol.https.allow':'always',
                'submodule.recurse':'false','http.proxy':'','http.followRedirects':'false','http.sslVerify':'true'}
            # This sole helper is official protected code, not checkout/global
            # configuration. Git consumes its secret output internally.
            items = list(options.items()) + [('credential.helper','!'+shlex.quote(str(self.gh))+' auth git-credential')]
            env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL='/dev/null',GIT_CONFIG_SYSTEM='/dev/null',
                GIT_TERMINAL_PROMPT='0',GIT_OPTIONAL_LOCKS='0',GIT_NO_REPLACE_OBJECTS='1',GIT_NO_LAZY_FETCH='1',
                GIT_ALLOW_PROTOCOL='https',GIT_PROTOCOL_FROM_USER='0',GCM_INTERACTIVE='never',GIT_CONFIG_COUNT=str(len(items)))
            for n,(key,value) in enumerate(items):
                env['GIT_CONFIG_KEY_'+str(n)] = key; env['GIT_CONFIG_VALUE_'+str(n)] = value
        return env

    def _command(self, argv, *, cwd, git=False):
        """Bounded argv-only command. Raw stdout/stderr never become a receipt."""
        self._roots()
        process = None; descriptors = []; terminated = False
        try:
            descriptors.extend(os.pipe()); descriptors.extend(os.pipe())
            status_read,status_write,life_read,life_write = descriptors
            launcher = [str(self.python),'-I','-S','-B',str(self.guardian),str(status_write),str(life_read),'--',*argv]
            environment = self._environment(git=git)
            self._confirm_launch(argv,cwd=cwd,git=git)
            process = subprocess.Popen(launcher,cwd=cwd,env=environment,stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,umask=0o077,
                pass_fds=(status_write,life_read))
            os.close(status_write); descriptors.remove(status_write)
            os.close(life_read); descriptors.remove(life_read)
            outputs = [bytearray(),bytearray()]; status = bytearray(); code = None
            total = 0; deadline = time.monotonic()+self.timeout
            with selectors.DefaultSelector() as poll:
                poll.register(process.stdout,selectors.EVENT_READ,0); poll.register(process.stderr,selectors.EVENT_READ,1)
                poll.register(status_read,selectors.EVENT_READ,2)
                while poll.get_map():
                    if time.monotonic() >= deadline: fail('git_publication_command_unconfirmed')
                    for key,_ in poll.select(min(.1,max(.001,deadline-time.monotonic()))):
                        raw = os.read(key.fd,65-len(status) if key.data==2 else min(65536,MAX_OUTPUT+1-total))
                        if not raw:
                            poll.unregister(key.fileobj)
                            if key.data==2 and code is None: fail('git_publication_command_unconfirmed')
                        elif key.data==2:
                            status.extend(raw)
                            if len(status)>64: fail('git_publication_command_unconfirmed')
                            if b'\n' in status:
                                if code is not None or re.fullmatch(rb'(?:0|[1-9][0-9]{0,2}|-[1-9][0-9]{0,2})\n',status) is None:
                                    fail('git_publication_command_unconfirmed')
                                code = int(status)
                                if not -255 <= code <= 255: fail('git_publication_command_unconfirmed')
                                if os.getpgid(process.pid)!=process.pid or os.getsid(process.pid)!=process.pid:
                                    fail('git_publication_command_unconfirmed')
                                os.killpg(process.pid,signal.SIGKILL); terminated = True
                        else:
                            total += len(raw)
                            if total > MAX_OUTPUT: fail('git_publication_command_unconfirmed')
                            outputs[key.data].extend(raw)
            if code is None or not terminated or process.wait(timeout=5)!=-signal.SIGKILL:
                fail('git_publication_command_unconfirmed')
            return code,bytes(outputs[0]),bytes(outputs[1])
        except (OSError,subprocess.TimeoutExpired): fail('git_publication_command_unconfirmed')
        finally:
            cleanup_error = False
            if process is not None:
                # No poll/reap before the owned group signal, including failures.
                if not terminated and process.returncode is None:
                    try: os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError: pass
                    except OSError: cleanup_error = True
                if process.returncode is None:
                    try: process.wait(timeout=5)
                    except (OSError,subprocess.TimeoutExpired): cleanup_error = True
                process.stdout.close(); process.stderr.close()
            for descriptor in descriptors: os.close(descriptor)
            if cleanup_error: fail('git_publication_command_unconfirmed')

    def _confirm_launch(self, argv, *, cwd, git):
        push = git and len(argv)>1 and argv[1]=='push'
        post = not git and len(argv)>6 and argv[1:7]==['api','--hostname',HOST,'--method','POST',API+'/pulls']
        if self.authority is None or not (push or post): return
        pending,self._mutation_authority = self._mutation_authority,None
        try:
            if pending is None: fail('git_publication_authority_unconfirmed')
            operation_id,request,phase = pending
            root = self.root/operation_id
            expected = ([str(self.git),'push','--porcelain','--no-verify',
                '--force-with-lease=refs/heads/'+request['branch']+':',REMOTE,
                request['delivery_sha']+':refs/heads/'+request['branch']] if push else
                [str(self.gh),'api','--hostname',HOST,'--method','POST',API+'/pulls','--input',str(root/'create-pr.json')])
            if argv!=expected or Path(cwd)!=(root/'transport' if push else root) or phase!=('pushing' if push else 'creating'):
                fail('git_publication_authority_unconfirmed')
            with PrivateDirectory(root) as private:
                value = self._read(private)
                if value['request']!=request or value['phase']!=phase: fail('git_publication_record_changed')
            if self.authority.confirm_local(operation_id=operation_id,grant=self.grant,request=request) is not True:
                fail('git_publication_authority_refused')
        except DevelopmentError as error: raise _AuthorityRefusal(error.code) from None
        except Exception: raise _AuthorityRefusal('git_publication_authority_unconfirmed') from None

    def _api(self, path, *, cwd, method='GET', input_file=None):
        argv = [str(self.gh),'api','--hostname',HOST,'--method',method,path]
        if input_file is not None: argv += ['--input',str(input_file)]
        code,out,_ = self._command(argv,cwd=cwd)
        if code != 0 or not out: fail('git_publication_remote_unavailable')
        return json_output(out)

    def _auth(self, cwd, expected_login=None):
        code,out,err = self._command([str(self.gh),'auth','status','--hostname',HOST],cwd=cwd)
        if code != 0 or not (out or err): fail('git_publication_authentication_unavailable')
        actor = self._api('user',cwd=cwd)
        login = actor.get('login') if type(actor) is dict else None
        if type(login) is not str or not re.fullmatch('[A-Za-z0-9-]{1,100}',login): fail('git_publication_authentication_unavailable')
        if expected_login is not None and login != expected_login: fail('git_publication_authentication_changed')
        repo = self._api(API,cwd=cwd)
        if (type(repo) is not dict or repo.get('full_name') != REPOSITORY
                or repo.get('html_url') != 'https://github.com/'+REPOSITORY
                or type(repo.get('permissions')) is not dict or repo['permissions'].get('push') is not True
                or repo.get('archived') is not False or repo.get('disabled') is not False):
            fail('git_publication_repository_unavailable')
        return login

    def _source(self, operation_id, workspace_private, delivery_private, delivery_sha):
        value = self.deliveries._sealed(operation_id,workspace_private,delivery_private)
        if (value['request']['scope']['owner_id'] != self.grant['owner_id']
                or self.workspaces.grant['owner_id'] != self.grant['owner_id']
                or value['request']['base_commit'] != self.grant['base_commit']
                or value['seal']['delivery_sha'] != delivery_sha): fail('git_publication_scope_changed')
        self._roots()
        return value

    def _read(self, private):
        value = decode(private.read('publication.json',MAX_RECORD))
        if (type(value) is not dict or set(value) != {'schema','phase','request','request_sha256','pins',
                'login','transport_identity','transport_metadata','pr','reason'}
                or value['schema'] != SCHEMA or value['phase'] not in PHASES or value['pins'] != self.pins
                or value['request_sha256'] != digest(canonical(value['request']))
                or value['request'].get('operation_id') != private.path.name
                or value['request'].get('grant') != self.grant
                or value['request'].get('authority') != self.authority_pin
                or value['request'].get('local_grant') != self.workspaces.grant): fail('git_publication_record_refused')
        return value

    @staticmethod
    def _public(value):
        return dict(operation_id=value['request']['operation_id'], phase=value['phase'],
            status='complete' if value['phase']=='complete' else 'retained',reason=value['reason'],
            delivery_sha=value['request']['delivery_sha'],branch=value['request']['branch'],
            pull_request=copied(value['pr']))

    def _save(self, private, value, phase, **kwargs):
        value.update(phase=phase,reason='',**kwargs)
        self._roots(); private.replace('publication.json',canonical(value))

    def _clone(self, private, value, source):
        path = private.path/'transport'; os.mkdir(path,0o700)
        templates = private.path/'empty-templates'; os.mkdir(templates,0o700)
        self.workspaces._git(['clone','--local','--no-hardlinks','--no-checkout','--no-tags',
            '--template='+str(templates),'--',source['request']['checkout'],str(path)])
        with PrivateDirectory(path/'.git') as git: git.replace('config',CONFIG)
        self.workspaces._git(['fsck','--strict','--no-reflogs','--no-dangling'],cwd=path)
        self.workspaces._fsync_tree(path)
        value.update(transport_identity=_directory(path,private=True),
            transport_metadata=digest(canonical(self.workspaces._copy_metadata(path))))
        self._transport(private,value)

    def _transport(self, private, value):
        path = private.path/'transport'
        if (sorted(os.listdir(path)) != ['.git'] or value['transport_identity'] != _directory(path,private=True)
                or _file(path/'.git/config',32768) != CONFIG
                or value['transport_metadata'] != digest(canonical(self.workspaces._copy_metadata(path)))):
            fail('git_publication_transport_changed')
        if (self.workspaces._git(['rev-parse','--verify',value['request']['delivery_sha']+'^{tree}'],cwd=path).strip().decode()
                != value['request']['tree_sha']): fail('git_publication_transport_changed')
        if value['transport_metadata'] != digest(canonical(self.workspaces._copy_metadata(path))):
            fail('git_publication_transport_changed')
        return path

    def _remote(self, private, value):
        path = self._transport(private,value)
        base = 'refs/heads/'+self.grant['base_branch']; head = 'refs/heads/'+value['request']['branch']
        code,out,_ = self._command([str(self.git),'ls-remote','--refs',REMOTE,base,head],cwd=path,git=True)
        if code != 0 or not out: fail('git_publication_remote_unavailable')
        seen = {}
        try:
            for line in out.decode('ascii','strict').splitlines():
                oid,ref = line.split('\t')
                if ref not in (base,head) or ref in seen: fail('git_publication_remote_invalid')
                seen[ref] = sha(oid)
        except (ValueError,UnicodeError): fail('git_publication_remote_invalid')
        if seen.get(base) != self.grant['base_commit']: fail('git_publication_base_changed')
        current = seen.get(head)
        if current is not None and current != value['request']['delivery_sha']: fail('git_publication_branch_occupied')
        return current

    @staticmethod
    def _body(value):
        return value['request']['body'].rstrip()+'\n\n<!-- blackoutburst-delivery:'+value['request_sha256']+' -->\n'

    def _pr(self, value, row):
        request = value['request']; number = row.get('number') if type(row) is dict else None
        url = 'https://github.com/'+REPOSITORY+'/pull/'+str(number)
        if (type(number) is not int or number < 1 or row.get('html_url') != url or row.get('state') != 'open'
                or row.get('draft') is not True or row.get('title') != request['title'] or row.get('body') != self._body(value)
                or type(row.get('user')) is not dict or row['user'].get('login') != value['login']): fail('git_publication_pr_mismatch')
        for kind,ref,oid in (('head',request['branch'],request['delivery_sha']),
                              ('base',self.grant['base_branch'],self.grant['base_commit'])):
            part = row.get(kind)
            if (type(part) is not dict or part.get('ref') != ref or part.get('sha') != oid
                    or type(part.get('repo')) is not dict or part['repo'].get('full_name') != REPOSITORY): fail('git_publication_pr_mismatch')
        return dict(number=number,url=url,head_sha=request['delivery_sha'],base_sha=self.grant['base_commit'],draft=True)

    def _lookup_pr(self, private, value):
        query = '?state=all&per_page=100&head='+quote('Blackoutburst:'+value['request']['branch'],safe='')+'&base='+quote(self.grant['base_branch'],safe='')
        rows = self._api(API+'/pulls'+query,cwd=private.path)
        if type(rows) is not list or len(rows)>1: fail('git_publication_pr_ambiguous')
        return None if not rows else self._pr(value,rows[0])

    def _before_effect(self, operation_id, workspace_private, delivery_private, private, value, *, head_required):
        self._auth(private.path,expected_login=value['login'])
        head = self._remote(private,value)
        if head_required and head != value['request']['delivery_sha']: fail('git_publication_branch_unconfirmed')
        self._source(operation_id,workspace_private,delivery_private,value['request']['delivery_sha'])
        self._transport(private,value)  # After the last protected test callback.
        if self._read(private) != value: fail('git_publication_record_changed')
        return head

    def _authorize_effect(self, operation_id, workspace_private, delivery_private, private, value):
        # All GitHub/remote GETs precede this fresh Board observation. Nothing
        # remotely read follows it before push/POST. Repeat local proof/pins
        # after those Board reads without spending execution/test budgets.
        if self.authority is None: return
        if self.authority.before_effect(operation_id=operation_id,grant=self.grant,request=value['request']) is not True:
            fail('git_publication_authority_refused')
        self._source(operation_id,workspace_private,delivery_private,value['request']['delivery_sha'])
        self._transport(private,value)
        if self._read(private) != value: fail('git_publication_record_changed')
        if self.authority.confirm_local(operation_id=operation_id,grant=self.grant,request=value['request']) is not True:
            fail('git_publication_authority_refused')
        self._mutation_authority = operation_id,copied(value['request']),value['phase']

    def publish(self, *, operation_id, delivery_sha, title, body):
        _operation(operation_id); sha(delivery_sha)
        if (type(title) is not str or not 1 <= len(title.encode()) <= 200 or not title.strip()
                or any(ord(c)<32 or ord(c)==127 for c in title)
                or type(body) is not str or not body.strip() or len(body.encode()) > 16000
                or '<!-- blackoutburst-delivery:' in body): fail('git_publication_text_refused')
        check_content(title.encode()); check_content(body.encode())
        with self.workspaces._locked(operation_id) as (workspace_private,_), \
             self.deliveries._record(self.deliveries.delivery_root,operation_id) as (delivery_private,_):
            source = self._source(operation_id,workspace_private,delivery_private,delivery_sha)
            # Authentication and tool checks precede even the private copy.
            login = self._auth(self.root)
            request = dict(operation_id=operation_id,grant=self.grant,local_grant=self.workspaces.grant,
                delivery_sha=delivery_sha,tree_sha=source['seal']['tree_sha'],snapshot_sha256=source['snapshot_sha256'],
                scope=source['request']['scope'],branch='codex/delivery-'+operation_id,title=title,body=body)
            if self.authority_pin is not None: request['authority'] = copied(self.authority_pin)
            with self.deliveries._record(self.root,operation_id,create=True) as (private,fresh):
                if fresh:
                    value = dict(schema=SCHEMA,phase='planned',request=request,request_sha256=digest(canonical(request)),
                        pins=self.pins,login=login,transport_identity=None,transport_metadata=None,pr=None,reason='')
                    private.create('publication.json',canonical(value))
                else:
                    value = self._read(private)
                    if value['request'] != request or value['login'] != login: fail('git_publication_request_conflict')
                if value['phase']=='planned':
                    self._source(operation_id,workspace_private,delivery_private,delivery_sha)
                    self._save(private,value,'cloning')
                    self._clone(private,value,source)
                    self._source(operation_id,workspace_private,delivery_private,delivery_sha)
                    self._transport(private,value); self._save(private,value,'prepared')
                if value['phase']=='cloning': fail('git_publication_copy_retained')
                if value['phase'] in ('prepared','pushing'):
                    head = self._before_effect(operation_id,workspace_private,delivery_private,private,value,head_required=False)
                    if head is None:
                        if value['phase']=='pushing': fail('git_publication_push_retained')
                        self._save(private,value,'pushing')
                        self._authorize_effect(operation_id,workspace_private,delivery_private,private,value)
                        # Absence-only CAS: the server must still see this exact
                        # deterministic ref as absent. No existing ref is replaced.
                        try:
                            self._command([str(self.git),'push','--porcelain','--no-verify',
                                '--force-with-lease=refs/heads/'+request['branch']+':',REMOTE,
                                delivery_sha+':refs/heads/'+request['branch']],cwd=private.path/'transport',git=True)
                        except _AuthorityRefusal: raise
                        except DevelopmentError: pass
                        if self._remote(private,value) != delivery_sha: fail('git_publication_push_retained')
                    self._source(operation_id,workspace_private,delivery_private,delivery_sha)
                    self._transport(private,value); self._save(private,value,'branch_confirmed')
                if value['phase'] in ('branch_confirmed','creating','complete'):
                    self._before_effect(operation_id,workspace_private,delivery_private,private,value,head_required=True)
                    found = self._lookup_pr(private,value)
                    if found is None:
                        if value['phase'] != 'branch_confirmed': fail('git_publication_create_retained')
                        payload = dict(title=title,body=self._body(value),head=request['branch'],base=self.grant['base_branch'],draft=True)
                        try: private.create('create-pr.json',canonical(payload))
                        except DevelopmentError as error:
                            if error.code != 'private_file_exists' or private.read('create-pr.json',MAX_RECORD) != canonical(payload):
                                fail('git_publication_input_changed')
                        self._before_effect(operation_id,workspace_private,delivery_private,private,value,head_required=True)
                        if private.read('create-pr.json',MAX_RECORD) != canonical(payload): fail('git_publication_input_changed')
                        self._save(private,value,'creating')
                        self._authorize_effect(operation_id,workspace_private,delivery_private,private,value)
                        try: self._api(API+'/pulls',cwd=private.path,method='POST',input_file=private.path/'create-pr.json')
                        except _AuthorityRefusal: raise
                        except DevelopmentError: pass
                        found = self._lookup_pr(private,value)
                        if found is None: fail('git_publication_create_retained')
                    if value['phase']=='complete' and value['pr'] != found: fail('git_publication_pr_mismatch')
                    self._before_effect(operation_id,workspace_private,delivery_private,private,value,head_required=True)
                    # Re-read PR after the final source/test callback as well.
                    confirmed = self._lookup_pr(private,value)
                    if confirmed != found: fail('git_publication_pr_mismatch')
                    self._transport(private,value)
                    self._save(private,value,'complete',pr=found)
                return self._public(value)

    def status(self, operation_id):
        """Historical local metadata only; publish revalidates remote evidence."""
        self._roots()
        with self.deliveries._record(self.root,operation_id) as (private,_):
            return self._public(self._read(private))
