"""Owner-invoked decision pins and logical reset; no automatic memory discovery."""
import json
import os
from pathlib import Path
import stat

from .board_reader import BoardReader, BoardReadError
from .client_lifecycle import managed_client
from .context import ContextError, ContextScope
from .decision_memory import DecisionMemory, DecisionMemoryError, MAX_SUMMARY_BYTES, memory_scope
from .runner_cli import _write_private


def configure_parser(parser):
    parser.add_argument('--state',type=Path,required=True,help='Magasin privé explicite, distinct des contextes et des sessions du modèle.')
    commands=parser.add_subparsers(dest='memory_command',required=True)
    for name in ('pin','inspect','reset','forget'):
        child=commands.add_parser(name)
        for field in ('owner','agent','project','ticket','endpoint'):
            child.add_argument('--'+field,required=True)
        if name=='pin':
            child.add_argument('--connection-file',type=Path,required=True)
            child.add_argument('--session',required=True)
            child.add_argument('--thread',required=True)
            child.add_argument('--message',required=True)
            child.add_argument('--summary-file',type=Path,required=True,help='Résumé explicitement choisi pour le contexte partagé de ce ticket ; données, jamais permissions.')
            child.add_argument('--decision',help='UUID de la décision existante pour une correction explicite.')
        if name in ('pin','reset','forget'):
            child.add_argument('--revision',type=int,required=True,help='Révision inspectée, 0 uniquement pour le premier pin.')
        if name=='forget':child.add_argument('--decision',required=True)
        if name=='inspect':child.add_argument('--output',type=Path,required=True)


def read_summary(path):
    descriptor=None
    try:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):raise ValueError
        data=os.read(descriptor,MAX_SUMMARY_BYTES+1)
        if len(data)>MAX_SUMMARY_BYTES:raise ValueError
        return data.decode('utf-8','strict')
    except (OSError,ValueError,UnicodeError):raise DecisionMemoryError('decision_summary_invalid') from None
    finally:
        if descriptor is not None:os.close(descriptor)


def run(args):
    try:
        scope=memory_scope(args.endpoint,ContextScope(args.owner,args.agent,args.project,args.ticket))
        with DecisionMemory(args.state,create=args.memory_command=='pin') as memory:
            if args.memory_command=='pin':
                with managed_client(BoardReader.from_connection_file(args.connection_file,session_id=args.session)) as reader:
                    if reader.endpoint!=args.endpoint:raise DecisionMemoryError('decision_scope_invalid')
                    result=memory.pin(scope,expected_revision=args.revision,summary=read_summary(args.summary_file),
                        thread_id=args.thread,message_id=args.message,call=reader.call,registration=reader.register(),decision_id=args.decision)
                    output={'status':'pinned',**result}
            elif args.memory_command=='inspect':
                snapshot=memory.inspect(scope)
                _write_private(args.output,json.dumps(snapshot,ensure_ascii=False,indent=2)+'\n')
                output={'status':'exported','decision_count':len(snapshot['decisions']),**memory.descriptor(scope)}
            else:
                result=memory.forget(scope,expected_revision=args.revision,
                                     decision_id=args.decision if args.memory_command=='forget' else None)
                output={'status':'reset' if args.memory_command=='reset' else 'forgotten',**result}
        print(json.dumps(output,ensure_ascii=False));return 0
    except (ContextError,BoardReadError) as error:
        print(json.dumps({'status':'blocked','reason':error.code}));return 2
    except Exception:
        print(json.dumps({'status':'blocked','reason':'decision_configuration_or_io_invalid'}));return 2
