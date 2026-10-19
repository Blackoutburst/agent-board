"""Explicit development commands; offline status/stop/inspection stay offline."""
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path

from .board_reader import BoardReadError
from .client_lifecycle import own_client
from .development_client import DevelopmentBoardClient
from .development_worker import DevelopmentBoardWorker
from .dev_journal import PrivateDirectory, decode
from .dev_manifest import (DevelopmentError, RepositoryFiles, absolute_directory,
                           canonical, check_content, digest, relative_path)
from .dev_planner_journal import PlannerBudgets
from .dev_planner import _directory_pin
from .dev_test_supervisor import _ancestor_identities
from .run_store import RunStore, StoreError
from .runtime_client import RuntimeBoardClient
from .runtime_journal import RuntimeJournal
from .work_journal import WorkJournal, JournalError


def configure_parser(parser):
    parser.add_argument('--journal',required=True,type=Path,help='Journal privé MCP du travail choisi.')
    commands=parser.add_subparsers(dest='development_command',required=True)
    prepare=commands.add_parser('prepare',help='Lire les droits et sceller le plan ; ne lance rien.')
    prepare.add_argument('--state',required=True,type=Path)
    prepare.add_argument('--runtime-journal',required=True,type=Path)
    prepare.add_argument('--connection-file',required=True,type=Path)
    prepare.add_argument('--session',required=True)
    for value in ('owner','agent','project','ticket','request','model'):
        prepare.add_argument('--'+value,required=True)
    prepare.add_argument('--instruction-file',required=True,type=Path)
    prepare.add_argument('--broker',required=True,type=Path,help='Manifeste existant du checkout isolé.')
    prepare.add_argument('--test-backend',required=True,type=Path,help='Backend nommé déjà qualifié et supervisé.')
    prepare.add_argument('--planner-policy',required=True,type=Path,help='Politique déjà qualifiée du planner vide.')
    prepare.add_argument('--max-turns',type=int,default=16)
    prepare.add_argument('--max-actions',type=int,default=16)
    prepare.add_argument('--max-wall-seconds',type=int,default=1800)
    prepare.add_argument('--turn-timeout-seconds',type=int,default=120)
    for command in ('run','reconcile','publish'):
        child=commands.add_parser(command,help={'run':'Exécuter seulement le plan choisi.',
            'reconcile':'Relire les reçus exacts ; ne relance aucun modèle/test/patch.',
            'publish':'Publier le reçu daté en revue et libérer ce claim.'}[command])
        child.add_argument('--operation',required=True)
        child.add_argument('--connection-file',required=True,type=Path)
    for command in ('status','stop','inspect'):
        child=commands.add_parser(command,help={'status':'Métadonnées locales, sans connexion.',
            'stop':'Arrêt local durable, sans connexion.',
            'inspect':'Exporter les données privées pour examen, sans connexion.'}[command])
        child.add_argument('--operation',required=True)
        if command=='inspect':child.add_argument('--output',required=True,type=Path)


def _instruction(path):
    path=Path(path).absolute()
    relative_path(path.name)
    descriptor=absolute_directory(path.parent)
    try:
        data,_=RepositoryFiles.read_at(descriptor,path.name,16384)
    finally:os.close(descriptor)
    return check_content(data)


def _inspect(journal,operation_id,path):
    op=journal.get(operation_id)
    pins=op['configuration']['pins']
    target=Path(path).absolute()
    if _directory_pin(pins['planner_workspace']['path'])!=pins['planner_workspace']:
        raise DevelopmentError('development_directory_changed')
    ancestors=_ancestor_identities(target.parent)
    if tuple(pins['planner_workspace']['identity']) in ancestors:
        raise DevelopmentError('development_output_inside_planner')
    with PrivateDirectory(pins['broker']['path']) as private:
        manifest=decode(private.read('manifest.json',128*1024))
        files=RepositoryFiles(manifest['root'],manifest['root_identity'])
        files.close()
        root_identity=tuple(manifest['root_identity'])
        if root_identity in ancestors:
            raise DevelopmentError('development_output_inside_checkout')
        try:planner=decode(private.read('planner.json',2*1024*1024))
        except DevelopmentError as error:
            if error.code!='private_read_failed':raise
            planner=None
    if planner is not None:
        if planner.get('operation_id')!=operation_id:
            raise DevelopmentError('development_planner_scope_mismatch')
        empty=tuple(planner['configuration']['pins']['planner']['identity'])
        if empty in ancestors:raise DevelopmentError('development_output_inside_planner')
    export=canonical({'operation':op,'planner':planner})
    with PrivateDirectory(target.parent) as destination:
        destination.create(target.name,export)
    return {'operation_id':operation_id,'status':'exported','bytes':len(export),'sha256':digest(export)}


def run(args):
    try:
        with ExitStack() as stack:
            journal=stack.enter_context(WorkJournal(args.journal))
            command=args.development_command
            if command in ('status','stop','inspect'):
                worker=DevelopmentBoardWorker(journal,None,None,None,None)
                result=_inspect(journal,args.operation,args.output) if command=='inspect' else (
                    worker.stop(args.operation) if command=='stop' else worker.status(args.operation))
            else:
                if command=='prepare':
                    instruction=_instruction(args.instruction_file)
                    budgets=PlannerBudgets(max_turns=args.max_turns,max_actions=args.max_actions,
                        max_wall_seconds=args.max_wall_seconds,turn_timeout_seconds=args.turn_timeout_seconds)
                    budgets.document()
                    store=stack.enter_context(closing(RunStore(args.state)))
                    runtime=stack.enter_context(RuntimeJournal(args.runtime_journal))
                    client=own_client(stack,DevelopmentBoardClient.from_connection_file(args.connection_file,session_id=args.session,timeout=3))
                    scope={key+'_id':getattr(args,key) for key in ('owner','agent','project','ticket')}
                    scope.update(board_endpoint=client.endpoint,board_session_id=args.session)
                else:
                    op=journal.get(args.operation)
                    pins=op['configuration']['pins']
                    store=stack.enter_context(closing(RunStore(pins['runs']['path'])))
                    runtime=stack.enter_context(RuntimeJournal(pins['runtime']['path']))
                    client=own_client(stack,DevelopmentBoardClient.from_connection_file(args.connection_file,
                        session_id=op['scope']['board_session_id'],timeout=3))
                    if client.endpoint!=op['scope']['board_endpoint']:
                        raise DevelopmentError('development_connection_scope_mismatch')
                    client.adopt_registration()
                control=own_client(stack,RuntimeBoardClient.from_worker(client))
                worker=DevelopmentBoardWorker(journal,store,runtime,client,control)
                if command=='prepare':
                    result=worker.prepare(scope=scope,request_id=args.request,instruction=instruction,
                        broker_directory=args.broker,backend_directory=args.test_backend,
                        planner_policy_directory=args.planner_policy,model=args.model,budgets=budgets)
                elif command=='publish':result=worker.publish(args.operation)
                else:result=worker.run(args.operation,reconcile=command=='reconcile')
            print(json.dumps(result,ensure_ascii=False,sort_keys=True))
            return 0 if result.get('status') in ('prepared','review_ready','complete','exported') else 2
    except (DevelopmentError,JournalError,StoreError,BoardReadError) as error:
        print(json.dumps({'status':'blocked','reason':error.code},sort_keys=True))
        return 2
    except (ValueError,TypeError,KeyError,OSError):
        print(json.dumps({'status':'blocked','reason':'development_request_invalid'},sort_keys=True))
        return 2
