"""Explicit finite family commands. No listener or implicit registration."""
from contextlib import ExitStack,closing
import json
from pathlib import Path

from .board_reader import BoardReadError
from .development_worker import DevelopmentBoardWorker
from .dev_journal import PrivateDirectory,decode
from .dev_manifest import DevelopmentError
from .dev_test_supervisor import _ancestor_identities
from .family_client import FamilyBoardClient
from .family_journal import FamilyJournal,SLOTS
from . import family_journal
from .family_worker import DevelopmentFamilyWorker
from .run_store import RunStore,StoreError
from .runtime_client import RuntimeBoardClient
from .runtime_journal import RuntimeJournal
from .work_journal import WorkJournal,JournalError,_encode


def configure_parser(parser):
    parser.add_argument('--journal',required=True,type=Path,help='Journal familial privé.')
    commands=parser.add_subparsers(dest='family_command',required=True)
    prepare=commands.add_parser('prepare',help='Sceller trois checkouts et un plan explicite ; ne lance rien.')
    prepare.add_argument('--config-file',required=True,type=Path)
    prepare.add_argument('--connection-file',required=True,type=Path)
    for command in ('open','bind','run','reconcile','cleanup','publish-parent','stop'):
        child=commands.add_parser(command)
        child.add_argument('--connection-file',required=True,type=Path)
    commands.add_parser('status',help='État local, sans connexion.')
    commands.add_parser('stop-local',help='Arrêt local durable immédiat, sans connexion.')


def _read_plan(path):
    path=Path(path).absolute()
    with PrivateDirectory(path.parent) as directory:spec=decode(directory.read(path.name,512*1024))
    if (type(spec) is not dict or set(spec)!={'schema','request','workflows'} or type(spec['schema']) is not int
            or spec['schema']!=1 or type(spec['workflows']) is not dict or set(spec['workflows'])!=set(SLOTS)):
        raise DevelopmentError('invalid_family_configuration')
    for item in spec['workflows'].values():
        if type(item) is not dict or set(item)!={'journal','runs','runtime','registration'}:
            raise DevelopmentError('invalid_family_configuration')
    _encode(spec,512*1024,'invalid_family_configuration')  # Credentials are never configuration data.
    return spec


def _outside(path,roots):
    ancestors=_ancestor_identities(Path(path).absolute().parent)
    if any(tuple(pin['identity']) in ancestors for pin in roots.values()):
        raise DevelopmentError('family_private_overlap')


def _directory_outside(path,roots):
    """Read boundaries before a constructor may create a private directory."""
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise DevelopmentError('invalid_family_directory')
    ancestors=_ancestor_identities(path,allow_missing=True)
    if any(tuple(pin['identity']) in ancestors for pin in roots.values()):
        raise DevelopmentError('family_private_overlap')
    try:path.lstat()
    except FileNotFoundError:return
    # A missing sibling may share an ancestor with a checkout. Only the actual
    # existing target, never its nearest existing parent, can contain a root.
    if any(ancestors[0] in _ancestor_identities(pin['path']) for pin in roots.values()):
        raise DevelopmentError('family_private_overlap')


def _preflight(args,spec):
    """Pure preparation checks: no journals, stores, credentials or RPC opened."""
    from .family_audit import manifest_document
    from .dev_planner import _directory_pin
    request=spec['request']
    if (type(request) is not dict or type(request.get('children')) is not list
            or len(request['children'])!=2):raise DevelopmentError('invalid_family_configuration')
    parent,_=manifest_document(request['parent']['broker_directory'])
    roots={'parent':_directory_pin(parent['root']),**{
        'child_'+str(i):_directory_pin(child['root']) for i,child in enumerate(request['children'])}}
    if roots['parent']['identity']!=parent['root_identity']:
        raise DevelopmentError('family_initial_files_changed')
    if len({tuple(pin['identity']) for pin in roots.values()})!=3:
        raise DevelopmentError('family_private_overlap')
    for slot,pin in roots.items():
        ancestors=_ancestor_identities(pin['path'])
        if any(tuple(other['identity']) in ancestors for name,other in roots.items() if name!=slot):
            raise DevelopmentError('family_private_overlap')
    _outside(args.config_file,roots);_outside(args.connection_file,roots)
    directories=[args.journal,family_journal.canonical_anchor(),Path(__file__).resolve().parent]
    for item in spec['workflows'].values():
        directories.extend(item[key] for key in ('journal','runs','runtime'))
    for item in [request['parent'],*request['children']]:
        directories.extend(item[key] for key in ('broker_directory','backend_directory','planner_policy_directory'))
    for directory in directories:_directory_outside(directory,roots)
    return roots


def _local_state(directory,*,stop=False):
    """Existing metadata and the stop marker need no fence/store constructor."""
    with PrivateDirectory(directory) as private:
        journal=object.__new__(FamilyJournal);journal.private=private
        state=journal.read()
        if stop:journal.request_stop()
        return state,journal.stopped()


def _resume_preflight(args,state):
    from .dev_planner import _directory_pin
    config=state['configuration'];roots=config['roots']
    descriptor=state['payload']['fence_descriptor']
    if _directory_pin(args.journal)!=descriptor['journal']:
        raise DevelopmentError('family_directory_changed')
    for pin in roots.values():
        if _directory_pin(pin['path'])!=pin:raise DevelopmentError('family_directory_changed')
    pins=[descriptor['journal']]
    for slot in SLOTS:
        pins.extend(config['pins'][slot].values())
        pins.extend(config['child_stores'][slot].values())
    for pin in pins:
        _directory_outside(pin['path'],roots)
        if _directory_pin(pin['path'])!=pin:raise DevelopmentError('family_directory_changed')
    _outside(args.connection_file,roots)
    _directory_outside(family_journal.canonical_anchor(),roots)
    _directory_outside(Path(__file__).resolve().parent,roots)


def run(args):
    local_stop=False
    try:
        command=args.family_command
        spec=None
        if command=='prepare':
            spec=_read_plan(args.config_file)
            _preflight(args,spec)  # Must precede FamilyJournal/RunStore constructors.
        else:
            state,local_stop=_local_state(args.journal,stop=command in ('stop','stop-local'))
            if command in ('status','stop-local'):
                result={**DevelopmentFamilyWorker._public(state),'stop_requested':local_stop}
                print(json.dumps(result,ensure_ascii=False,sort_keys=True));return 0
            _resume_preflight(args,state)
        with ExitStack() as stack:
            journal=stack.enter_context(FamilyJournal(args.journal))
            if command in ('status','stop-local'):
                state=journal.read()
                if command=='stop-local':journal.request_stop()
                result={**DevelopmentFamilyWorker._public(state),'stop_requested':journal.stopped()}
            else:
                if command=='prepare':
                    request=spec['request'];registrations={}
                    stores=spec['workflows'];scope=request['scope']
                    sessions={'parent':scope['board_session_id'],**{
                        'child_'+str(i):child['session_id'] for i,child in enumerate(request['children'])}}
                else:
                    state=journal.read();config=state['configuration'];scope=config['scope']
                    registrations=config['registrations'];sessions={slot:config['provisional_scopes'][slot]['board_session_id'] for slot in SLOTS}
                    stores={slot:{'journal':config['pins'][slot]['journal']['path'],
                        **{kind:config['child_stores'][slot][kind]['path'] for kind in ('runs','runtime')}} for slot in SLOTS}
                workflows={}
                for slot in SLOTS:
                    item=stores[slot]
                    work=stack.enter_context(WorkJournal(item['journal']))
                    store=stack.enter_context(closing(RunStore(item['runs'])))
                    runtime=stack.enter_context(RuntimeJournal(item['runtime']))
                    client=FamilyBoardClient.from_connection_file(args.connection_file,session_id=sessions[slot],timeout=3)
                    if client.endpoint!=scope['board_endpoint']:raise DevelopmentError('family_connection_scope_changed')
                    client.adopt_registration()
                    if command=='prepare':registrations[slot]=item['registration']
                    workflows[slot]=DevelopmentBoardWorker(work,store,runtime,client,RuntimeBoardClient.from_worker(client))
                worker=DevelopmentFamilyWorker(journal,workflows,registrations=registrations)
                if command=='prepare':
                    # Determine roots from supplied manifests before any connection
                    # effect or global reservation, including config/credentials.
                    from .family_audit import manifest_document
                    from .dev_planner import _directory_pin
                    parent,_=manifest_document(request['parent']['broker_directory'])
                    roots={'parent':_directory_pin(parent['root']),**{
                        'child_'+str(i):_directory_pin(child['root']) for i,child in enumerate(request['children'])}}
                    _outside(args.config_file,roots);_outside(args.connection_file,roots)
                    result=worker.prepare(request)
                else:
                    _outside(args.connection_file,state['configuration']['roots'])
                    method={'publish-parent':'publish_parent'}.get(command,command)
                    result=getattr(worker,method)()
            print(json.dumps(result,ensure_ascii=False,sort_keys=True))
            return 0 if result.get('status') in ('prepared','opened','bound','review_ready') or result.get('stop_requested') is True else 2
    except (DevelopmentError,JournalError,StoreError,BoardReadError) as error:
        result={'status':'blocked','reason':error.code}
        if local_stop:result.update(stop_requested=True,server_stop='pending',cleanup_confirmed=False)
        print(json.dumps(result,sort_keys=True));return 2
    except (ValueError,TypeError,KeyError,OSError,AttributeError):
        result={'status':'blocked','reason':'family_request_invalid'}
        if local_stop:result.update(stop_requested=True,server_stop='pending',cleanup_confirmed=False)
        print(json.dumps(result,sort_keys=True));return 2
