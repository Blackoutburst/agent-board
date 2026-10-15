"""Explicit single-ticket selection; never a scheduler or permanent worker."""
from contextlib import ExitStack
import json
import math
from pathlib import Path
import re
import signal
import threading

from .board_worker import BoardWorker
from .context import ContextScope, _profile
from .queue_client import QueueBoardClient, HumanQueueBoardClient
from .queue_journal import QueueJournal, STAGES
from .queue_worker import QueueWorker
from .run_store import RunStore
from .runner_cli import _write_private
from .subscription_probe import ProbeSettings, validate_executable
from .work_journal import WorkJournal, JournalError
from .worker_cli import (_instruction, configure_routing, routing_options,
    configure_runtime, runtime_worker_options, runtime_run_options)
from .model_policy import PolicyError, routing_configuration, check_routing_connection


def configure_parser(parser):
    parser.add_argument('--journal', required=True, type=Path)
    parser.add_argument('--work-journal', type=Path)
    parser.add_argument('--state', type=Path)
    commands=parser.add_subparsers(dest='queue_command', required=True)
    run=commands.add_parser('run', help='Prévisualiser ou traiter un seul ticket éligible du projet autorisé.')
    run.add_argument('--connection-file', required=True, type=Path)
    run.add_argument('--session', required=True)
    for name in ('owner','agent','project','request'):
        run.add_argument('--'+name, required=True)
    run.add_argument('--instruction-file', required=True, type=Path)
    run.add_argument('--workspace', required=True, type=Path)
    run.add_argument('--codex', required=True)
    run.add_argument('--model')
    configure_routing(run)
    configure_runtime(run)
    run.add_argument('--timeout', type=float, default=120)
    run.add_argument('--human-owner')
    run.add_argument('--execute', action='store_true')
    status=commands.add_parser('status', help='Lire uniquement les métadonnées locales.')
    status.add_argument('--operation', required=True)
    inspect=commands.add_parser('inspect', help='Exporter la configuration privée pour examen.')
    inspect.add_argument('--operation', required=True)
    inspect.add_argument('--output', required=True, type=Path)
    selection=commands.add_parser('reconcile-selection', help='Retrouver la sélection exacte en lecture seule.')
    selection.add_argument('--operation', required=True)
    selection.add_argument('--connection-file', required=True, type=Path)
    resolve=commands.add_parser('reconcile-worker', help='Clore après preuve exacte du worker local résolu.')
    resolve.add_argument('--operation', required=True)
    abandon=commands.add_parser('abandon', help='Abandonner après examen explicite des effets et du worker.')
    abandon.add_argument('--operation', required=True)
    abandon.add_argument('--revision', type=int, required=True)
    abandon.add_argument('--reason', required=True)
    abandon.add_argument('--acknowledge-board-reconciled', required=True, action='store_true')


def _public(value):
    if (not isinstance(value,dict) or value.get('status') not in ('idle','complete','pending','needs_review','abandoned')
            or value.get('stage') not in STAGES or type(value.get('cached')) is not bool
            or not isinstance(value.get('reason'),str) or not re.fullmatch('[a-z][a-z0-9_]{0,100}',value['reason'])):
        raise ValueError
    result={key:value[key] for key in ('status','stage','reason','cached')}
    for key in ('operation_id','ticket_id','worker_operation_id'):
        if key in value:
            if not isinstance(value[key],str) or not re.fullmatch('[A-Za-z0-9_-]{1,256}',value[key]):
                raise ValueError
            result[key]=value[key]
    return result


def _child_scope(operation):
    return {**operation['scope'],'project_id':operation['configuration']['project_id'],
            'ticket_id':operation['payload']['ticket_id']}


def _local_resolution(args,journal):
    operation=journal.get(args.operation)
    with journal.lock(operation['scope']):
        operation=journal.get(args.operation)
        expected=None
        if args.queue_command=='abandon':
            if (type(args.revision) is not int or args.revision<1 or args.acknowledge_board_reconciled is not True
                    or not isinstance(args.reason,str) or not args.reason.strip() or len(args.reason)>256
                    or any(ord(c)<32 or ord(c)==127 for c in args.reason)):
                raise ValueError
            expected={'revision':args.revision,'reason':args.reason,'board_reconciled':True}
            intent=operation['payload'].get('abandonment_intent')
            if intent is not None:
                if intent!=expected or operation['revision']!=args.revision+(2 if operation['stage']=='abandoned' else 1):
                    raise JournalError('abandonment_conflict')
                if operation['stage']=='abandoned':return journal.status(operation['id'])
            elif operation['revision']!=args.revision:
                raise JournalError('revision_conflict')
            if operation['stage']!='needs_review':raise JournalError('invalid_stage_transition')
            if (operation['payload'].get('selection_attempted') is True
                    and operation['payload'].get('selection_confirmed') is not True):
                raise JournalError('selection_unconfirmed')

        def commit_abandon(*,proof=None,worker_absent=False):
            # Persist intent only after all selection and child checks pass.
            # A rejected attempt must leave its original revision usable after
            # reconciliation. Both locks remain held through the transition.
            nonlocal operation
            if operation['payload'].get('abandonment_intent') is None:
                operation=journal.update(operation['id'],operation['revision'],stage='needs_review',
                    payload={**operation['payload'],'abandonment_intent':expected})
            journal.abandon(operation['id'],operation['revision'],args.reason,
                proof=proof,worker_absent=worker_absent)

        linked=operation['payload'].get('worker_preparing') or operation['payload'].get('worker_operation_id')
        if not linked:
            if args.queue_command!='abandon':raise JournalError('worker_not_found')
            commit_abandon()
            return journal.status(operation['id'])
        if args.work_journal is None:raise JournalError('worker_proof_required')
        child_scope=_child_scope(operation)
        with WorkJournal(args.work_journal) as work:
            with work.lock(child_scope):
                child=work.find(child_scope,operation['payload']['worker_request_id'])
                if child is None:
                    if args.queue_command!='abandon':raise JournalError('worker_not_found')
                    if (operation['payload'].get('worker_preparing') is not True
                            or operation['payload'].get('worker_operation_id')):
                        raise JournalError('invalid_worker_absence')
                    commit_abandon(worker_absent=True)
                else:
                    journal._worker_proof(operation,child)
                    if child['stage'] not in ('complete','abandoned'):
                        return _public({'status':'needs_review','stage':operation['stage'],
                            'reason':'worker_not_resolved','cached':False,'operation_id':operation['id'],
                            'worker_operation_id':child['id'],'ticket_id':operation['payload']['ticket_id']})
                    if args.queue_command=='abandon':commit_abandon(proof=child)
                    else:journal.resolve(operation['id'],child)
        return journal.status(operation['id'])


def run(args):
    try:
        with ExitStack() as stack:
            if args.queue_command in ('status','inspect','abandon','reconcile-worker','reconcile-selection'):
                journal=stack.enter_context(QueueJournal(args.journal))
                if args.queue_command=='status':output=journal.status(args.operation)
                elif args.queue_command=='inspect':
                    operation=journal.get(args.operation)
                    _write_private(args.output,json.dumps(operation,ensure_ascii=False,indent=2)+'\n')
                    output={'status':'exported','operation_id':operation['id'],'revision':operation['revision']}
                elif args.queue_command=='reconcile-selection':
                    operation=journal.get(args.operation)
                    client=QueueBoardClient.from_connection_file(args.connection_file,
                        session_id=operation['scope']['board_session_id'])
                    output=_public(QueueWorker(journal,None,client).reconcile_selection(operation['id']))
                else:output=_local_resolution(args,journal)
            else:
                if (not math.isfinite(args.timeout) or not 1<=args.timeout<=300
                        or (args.execute and (args.state is None or args.work_journal is None))):raise ValueError
                instruction=_instruction(args.instruction_file)
                routing=routing_options(args)
                executable=validate_executable(args.codex)
                ProbeSettings(executable=executable,model=args.model,timeout_seconds=args.timeout)
                if not routing and (not isinstance(args.model,str) or not args.model.strip()):raise ValueError
                if args.human_owner is not None and (not isinstance(args.human_owner,str) or not args.human_owner.strip()
                        or len(args.human_owner)>256 or any(ord(c)<32 or ord(c)==127 for c in args.human_owner)):raise ValueError
                if args.human_owner is not None:args.human_owner.encode('utf-8','strict')
                workspace=args.workspace.expanduser().resolve(strict=True)
                if not workspace.is_dir():raise ValueError
                client_class=HumanQueueBoardClient if args.human_owner is not None else QueueBoardClient
                client=client_class.from_connection_file(args.connection_file,session_id=args.session)
                scope={'owner_id':args.owner,'agent_id':args.agent,'board_session_id':args.session,'board_endpoint':client.endpoint}
                if not args.execute:
                    registration=client.register()
                    _profile(registration,ContextScope(args.owner,args.agent,args.project,'queue-preview'))
                    if routing:
                        configured=routing_configuration(**routing,owner_id=args.owner)
                        check_routing_connection(configured,scope,registration)
                    project=client.call('get_project',{'project_id':args.project})
                    if (project.get('id')!=args.project or args.owner not in project.get('participants',[])
                            or project.get('archived') is not False):raise ValueError
                    candidates=client.call('get_work',{'project_id':args.project,'limit':20})
                    output={'status':'preview','ready_count':len(candidates['ready']),
                            'recovery_count':len(candidates['recovery']),'waiting_count':len(candidates['waiting'])}
                else:
                    journal=stack.enter_context(QueueJournal(args.journal))
                    work=stack.enter_context(WorkJournal(args.work_journal))
                    store=RunStore(args.state);stack.callback(store.close)
                    worker=BoardWorker(work,store,client,**runtime_worker_options(args,client,stack))
                    cancelled,previous=threading.Event(),{}
                    try:
                        for signum in (signal.SIGINT,signal.SIGTERM):
                            previous[signum]=signal.signal(signum,lambda *_:cancelled.set())
                        output=_public(QueueWorker(journal,worker,client).run(scope=scope,request_id=args.request,
                            project_id=args.project,instruction=instruction,workspace=workspace,executable=executable,
                            model=args.model,timeout=args.timeout,human_owner_id=args.human_owner,
                            should_cancel=cancelled.is_set,**routing,**runtime_run_options(args)))
                    finally:
                        for signum,handler in previous.items():signal.signal(signum,handler)
        print(json.dumps(output,ensure_ascii=False,indent=2))
        return 2 if output.get('status') in ('needs_review','pending') else 0
    except KeyboardInterrupt:
        print(json.dumps({'status':'needs_review','reason':'queue_interrupted'}));return 130
    except (JournalError, PolicyError) as error:
        code=error.code if re.fullmatch('[a-z][a-z0-9_]{0,100}',error.code) else 'queue_state_unavailable'
        print(json.dumps({'status':'needs_review','reason':code}));return 2
    except Exception:
        print(json.dumps({'status':'needs_review','reason':'invalid_queue_configuration_or_io'}));return 2
