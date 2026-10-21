"""Real private I/O and actual source-wait code, without any child or namespace."""
import ast
from copy import deepcopy
import inspect
import os
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from blackoutburst_agents.dev_journal import PrivateDirectory
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
import blackoutburst_agents.linux_test_supervisor as supervisor


def source_wait_function():
    tree=ast.parse(inspect.getsource(supervisor.supervise))
    loops=[node for node in ast.walk(tree) if isinstance(node,ast.While)
           and 'source-window.json' in ast.unparse(node)]
    assert len(loops)==1
    arguments=[ast.arg(arg=name) for name in
               ('private','control','job','challenge','boundary_sequence','startup_deadline')]
    function=ast.FunctionDef(name='source_wait',args=ast.arguments(posonlyargs=[],args=arguments,
        kwonlyargs=[],kw_defaults=[],defaults=[]),decorator_list=[],body=[
        ast.Assign(targets=[ast.Name(id='approved',ctx=ast.Store())],value=ast.Constant(False)),
        deepcopy(loops[0]),ast.Return(value=ast.Name(id='approved',ctx=ast.Load()))])
    module=ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[]))
    namespace=dict(vars(supervisor))
    exec(compile(module,inspect.getfile(supervisor)+'<source-wait>','exec'),namespace)
    namespace['_control_clock']=lambda:100.2
    return namespace['source_wait']


@pytest.fixture
def case(tmp_path):
    directory=tmp_path/'private';directory.mkdir(mode=0o700)
    private=PrivateDirectory(directory)
    window={'schema':1,'nonce':'a'*32,'sequence':2,'started_at':100.0,
            'validated_at':100.1,'deadline':101.5}
    job={'nonce':'a'*32,'request':{'operation_id':'synthetic-only'}}
    raw=canonical({'nonce':job['nonce'],'challenge':'b'*32,
                   'request_sha256':digest(canonical(job['request'])),
                   'window_sha256':digest(canonical(window))})
    control=SimpleNamespace(last_window=window,authorized=lambda:True)
    function=source_wait_function()
    def publish():
        private.create('source-window.json',raw)
        private.create('source-window.ready',b'')
    yield private,raw,publish,lambda:function(private,control,job,'b'*32,1,101.0)
    private.close()


def test_partial_window_is_not_read_before_empty_commit_marker(case):
    private,raw,publish,wait=case
    opened,release=threading.Event(),threading.Event()
    original=PrivateDirectory._write
    once=[True];errors=[]
    def write(descriptor,data):
        if once:
            once.pop();opened.set()
            assert release.wait(2)
        return original(descriptor,data)
    def writer():
        try:publish()
        except BaseException as error:errors.append(error)
    with patch.object(PrivateDirectory,'_write',staticmethod(write)):
        thread=threading.Thread(target=writer);thread.start();assert opened.wait(2)
        assert private.read('source-window.json',2048)==b''
        assert supervisor.source_window_ready(private) is False
        def complete(_seconds):release.set();thread.join(2)
        try:
            with patch.object(supervisor.time,'sleep',side_effect=complete):assert wait() is True
        finally:release.set();thread.join(2)
    assert not errors and not thread.is_alive()
    assert private.read('source-window.json',2048)==raw


def test_absent_marker_open_then_completed_publication_stays_pending(case):
    private,raw,publish,wait=case
    original=os.open;once=[True]
    def interleaved_open(name,flags,*args,**kwargs):
        if name=='source-window.ready' and not flags & os.O_CREAT and once:
            once.pop()
            try:return original(name,flags,*args,**kwargs)
            except FileNotFoundError:
                publish()
                raise
        return original(name,flags,*args,**kwargs)
    with patch.object(supervisor.os,'open',side_effect=interleaved_open), \
         patch.object(supervisor.time,'sleep',return_value=None):
        assert wait() is True
    assert private.read('source-window.json',2048)==raw


@pytest.mark.parametrize('kind',['body','symlink','hardlink','fifo'])
def test_present_invalid_marker_is_never_pending(case,kind):
    private,raw,publish,wait=case
    private.create('source-window.json',raw)
    if kind=='body':private.create('source-window.ready',b'x')
    elif kind=='symlink':
        os.symlink('source-window.json',private.path/'source-window.ready')
    elif kind=='hardlink':
        private.create('marker-owned',b'')
        os.link(private.path/'marker-owned',private.path/'source-window.ready')
    else:os.mkfifo(private.path/'source-window.ready',0o600)
    with patch.object(supervisor.time,'sleep',side_effect=AssertionError('must refuse immediately')):
        with pytest.raises(DevelopmentError):wait()


def test_marker_disappearing_after_open_is_not_pending(case):
    private,raw,publish,wait=case
    publish();original=os.stat
    def unlinked(name,*args,**kwargs):
        if name=='source-window.ready':
            os.unlink(name,dir_fd=private.fd)
        return original(name,*args,**kwargs)
    with patch.object(supervisor.os,'stat',side_effect=unlinked), \
         patch.object(supervisor.time,'sleep',side_effect=AssertionError('must refuse immediately')):
        with pytest.raises(DevelopmentError):wait()


@pytest.mark.parametrize('kind',['missing','partial','symlink'])
def test_published_marker_requires_strict_complete_window(case,kind):
    private,raw,publish,wait=case
    if kind=='partial':private.create('source-window.json',b'{')
    elif kind=='symlink':
        private.create('foreign.json',raw)
        os.symlink('foreign.json',private.path/'source-window.json')
    private.create('source-window.ready',b'')
    with patch.object(supervisor.time,'sleep',side_effect=AssertionError('must refuse immediately')):
        with pytest.raises(DevelopmentError):wait()


def test_double_publication_refuses_and_preserves_original_bytes(case):
    private,raw,publish,wait=case
    publish()
    with pytest.raises(DevelopmentError,match='private_file_exists'):publish()
    assert private.read('source-window.json',2048)==raw
    assert private.read('source-window.ready',1)==b'' and wait() is True
