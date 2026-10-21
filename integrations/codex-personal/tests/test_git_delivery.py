"""Real, small local repos and synthetic controller test receipts; no network/model."""
from copy import deepcopy
from functools import partial
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
from blackoutburst_agents.dev_journal import PrivateDirectory
from blackoutburst_agents.git_delivery import LocalGitDeliveryController
from blackoutburst_agents.git_workspace import LocalGitWorkspaceController
from test_git_workspace import setup, planned, git


@pytest.fixture
def delivery(setup):
    f = setup
    f['delivery_root'] = f['state'].parent / 'deliveries'; f['delivery_root'].mkdir(mode=0o700)
    f['review_root'] = f['state'].parent / 'reviews'; f['review_root'].mkdir(mode=0o700)
    f['verified'] = {}
    def guard(**kw):
        sha = digest(canonical(kw['snapshot']))
        return f['verified'].get(sha) == kw['evidence']
    f['test_guard'] = guard
    f['delivery'] = LocalGitDeliveryController(f['controller'], f['delivery_root'], f['review_root'], test_guard=guard)
    f['op'], _ = planned(f)
    f['checkout'] = Path(f['controller'].materialize(f['op'])['checkout'])
    (f['checkout']/'example.py').write_text('value = 2\n')
    f['changes'] = [dict(path='example.py', sha256=digest(b'value = 2\n'), mode='100644')]
    return f


def capture(f):
    snapshot = f['delivery'].capture(operation_id=f['op'], scope=f['scope'], changes=f['changes'])
    f['snapshot'] = snapshot; f['snapshot_sha256'] = digest(canonical(snapshot))
    return snapshot


def test_receipt(f):
    # This is an actual controller-selected synthetic test, not a declared pass.
    code = "from pathlib import Path; assert Path('example.py').read_text() == 'value = 2\\n'"
    result = subprocess.run([sys.executable, '-I', '-c', code], cwd=f['checkout'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    receipt = dict(snapshot_sha256=f['snapshot_sha256'], exit_code=result.returncode, command_sha256=digest(code.encode()))
    evidence = dict(schema='controller-test-evidence-v1', snapshot_sha256=f['snapshot_sha256'],
                    receipts=[dict(name='synthetic-value', sha256=digest(canonical(receipt)))])
    f['verified'][f['snapshot_sha256']] = evidence
    f['evidence'] = evidence
    return evidence
test_receipt.__test__ = False


def seal(f):
    capture(f); test_receipt(f)
    f['seal'] = f['delivery'].finalize(operation_id=f['op'], snapshot_sha256=f['snapshot_sha256'],
                                    test_evidence=f['evidence'], message='Explicit synthetic change')
    return f['seal']


def review(f):
    f['review_op'] = str(uuid.uuid4())
    f['review_scope'] = dict(owner_id=f['scope']['owner_id'], agent_id='independent-reviewer',
        project_id=f['scope']['project_id'], ticket_id=f['scope']['ticket_id'],
        board_session_id='review-session', board_endpoint='https://board.example.invalid/mcp')
    f['context'] = dict(fingerprint='a'*64)
    f['material'] = 'Controller-selected summary: example.py changes value from 1 to 2.'
    f['review'] = f['delivery'].prepare_review(operation_id=f['op'], review_operation_id=f['review_op'],
        reviewer_scope=f['review_scope'], context_sha256=f['context']['fingerprint'], material=f['material'])
    return f['review']


def guard(f, **kw):
    args = dict(scope=f['review_scope'], selection=f['review']['request']['selection'],
        context=f['context'], material=f['material'], workspace=f['review']['workspace'])
    args.update(kw)
    return partial(f['delivery'].review_guard, f['op'], f['review_op'])(**args)


def test_exact_commit_and_distinct_readonly_export_leave_base_head_index_and_source_unchanged(delivery):
    f=delivery; before=git(f['checkout'], 'rev-parse', 'HEAD'); index=(f['checkout']/'.git/index').read_bytes()
    result=seal(f)
    assert result['delivery_sha'] != before == f['grant'].base_commit
    assert git(f['checkout'], 'rev-parse', 'HEAD') == before
    assert (f['checkout']/'.git/index').read_bytes() == index
    assert git(f['checkout'], 'show', result['delivery_sha']+':example.py') == 'value = 2'
    assert git(f['checkout'], 'rev-parse', result['delivery_sha']+'^') == before
    assert git(f['source'], 'rev-parse', 'HEAD') == before
    assert (f['source']/'example.py').read_text() == 'value = 1\n'
    f['controller'].manifest_inputs(f['op'])  # Foundation remains compatible.
    exported=Path(review(f)['workspace'])
    assert (exported/'example.py').read_text() == 'value = 2\n'
    assert not (exported/'.git').exists()
    assert exported.stat().st_mode & 0o777 == 0o500
    assert (exported/'example.py').stat().st_mode & 0o777 == 0o400
    assert (exported/'example.py').stat().st_ino != (f['checkout']/'example.py').stat().st_ino
    assert (exported/'example.py').stat().st_nlink == 1
    assert guard(f) is True


def test_exact_cold_resume_never_recommits_or_recopies(delivery, monkeypatch):
    f=delivery; expected=seal(f); r=review(f)
    reopened=LocalGitDeliveryController(LocalGitWorkspaceController(f['state'], grant=f['grant']),
        f['delivery_root'], f['review_root'], test_guard=f['test_guard'])
    monkeypatch.setattr(reopened, '_tree', lambda *_: pytest.fail('repeated commit effect'))
    assert reopened.finalize(operation_id=f['op'], snapshot_sha256=f['snapshot_sha256'],
        test_evidence=f['evidence'], message='Explicit synthetic change') == expected
    assert reopened.prepare_review(operation_id=f['op'], review_operation_id=f['review_op'],
        reviewer_scope=f['review_scope'], context_sha256='a'*64, material=f['material']) == r


@pytest.mark.parametrize('kind', ['undeclared', 'untracked', 'deleted', 'wrong_hash', 'private', 'symlink', 'hardlink', 'case_alias'])
def test_capture_refuses_unselected_or_unsafe_changes_without_commit(delivery, kind):
    f=delivery
    if kind=='undeclared':f['changes']=[]
    elif kind=='untracked':(f['checkout']/'unselected.txt').write_text('unselected\n')
    elif kind=='deleted':(f['checkout']/'example.py').unlink()
    elif kind=='wrong_hash':f['changes'][0]['sha256']='0'*64
    elif kind=='private':f['changes'][0]['path']='.git/hooks/post-checkout'
    elif kind=='symlink':
        (f['checkout']/'example.py').unlink(); (f['checkout']/'example.py').symlink_to(f['source']/'example.py')
    elif kind=='hardlink':
        (f['checkout']/'example.py').unlink(); os.link(f['source']/'example.py', f['checkout']/'example.py')
    else:f['changes'] += [dict(path='EXAMPLE.py', sha256='a'*64, mode='100644')]
    with pytest.raises(DevelopmentError):capture(f)
    assert git(f['checkout'], 'rev-parse', 'HEAD') == f['grant'].base_commit


def test_add_delete_mode_and_nested_paths_are_explicit_and_exact(delivery):
    f=delivery
    (f['checkout']/'example.py').unlink()
    directory=f['checkout']/'nested'; directory.mkdir(mode=0o700)
    (directory/'run.py').write_text('print(42)\n'); (directory/'run.py').chmod(0o700)
    f['changes']=[dict(path='example.py',sha256=None,mode=None),
                  dict(path='nested/run.py',sha256=digest(b'print(42)\n'),mode='100755')]
    capture(f)
    evidence=dict(schema='controller-test-evidence-v1', snapshot_sha256=f['snapshot_sha256'],
                  receipts=[dict(name='explicit-controller-test',sha256='c'*64)])
    f['verified'][f['snapshot_sha256']]=evidence
    result=f['delivery'].finalize(operation_id=f['op'],snapshot_sha256=f['snapshot_sha256'],test_evidence=evidence,message='Selected move')
    assert git(f['checkout'],'ls-tree','-r',result['delivery_sha']).startswith('100755 blob')
    assert 'nested/run.py' in git(f['checkout'],'ls-tree','-r',result['delivery_sha'])


@pytest.mark.parametrize('kind', ['missing_guard','false_guard','ambiguous_guard','no_receipt','wrong_snapshot','unrecognized_receipt'])
def test_finalize_requires_controller_verified_tests_for_exact_snapshot(delivery, kind):
    f=delivery;capture(f);test_receipt(f);evidence=deepcopy(f['evidence'])
    if kind=='missing_guard':f['delivery'].test_guard=None
    elif kind=='false_guard':f['delivery'].test_guard=lambda **_:False
    elif kind=='ambiguous_guard':f['delivery'].test_guard=lambda **_:1
    elif kind=='no_receipt':evidence['receipts']=[]
    elif kind=='wrong_snapshot':evidence['snapshot_sha256']='f'*64
    else:evidence['receipts'][0]['sha256']='f'*64
    with pytest.raises(DevelopmentError):
        f['delivery'].finalize(operation_id=f['op'],snapshot_sha256=f['snapshot_sha256'],test_evidence=evidence,message='Selected')
    assert git(f['checkout'],'rev-parse','HEAD')==f['grant'].base_commit
    assert git(f['checkout'],'for-each-ref','refs/heads/codex/delivery-')==''


@pytest.mark.parametrize('stage',['captured','sealed','reviewed'])
def test_author_mutation_after_proof_refuses_without_overwriting_edits(delivery,stage):
    f=delivery
    if stage=='captured':capture(f);test_receipt(f)
    else:seal(f)
    if stage=='reviewed':review(f)
    (f['checkout']/'example.py').write_text('new unpublished edit\n')
    with pytest.raises(DevelopmentError):
        if stage=='reviewed':guard(f)
        else:f['delivery'].finalize(operation_id=f['op'],snapshot_sha256=f['snapshot_sha256'],test_evidence=f['evidence'],message='Explicit synthetic change')
    assert (f['checkout']/'example.py').read_text()=='new unpublished edit\n'


@pytest.mark.parametrize('kind',['contents','writable','additional','replacement','link'])
def test_review_guard_rechecks_every_export_file_and_readonly_mode(delivery, kind):
    f=delivery;seal(f);review(f);export=Path(f['review']['workspace']);path=export/'example.py'
    if kind=='contents':path.chmod(0o600);path.write_text('different\n');path.chmod(0o400)
    elif kind=='writable':path.chmod(0o600)
    elif kind=='additional':
        export.chmod(0o700);(export/'extra.txt').write_text('extra\n');(export/'extra.txt').chmod(0o400);export.chmod(0o500)
    elif kind=='replacement':
        export.chmod(0o700);path.unlink();path.write_text('value = 2\n');path.chmod(0o400);export.chmod(0o500)
    else:
        export.chmod(0o700);path.unlink();path.symlink_to(f['checkout']/'example.py');export.chmod(0o500)
    with pytest.raises(DevelopmentError):guard(f)


@pytest.mark.parametrize('kind',['author','ticket','context','material','delivery','workspace','session'])
def test_distinct_review_selection_is_exact(delivery,kind):
    f=delivery;seal(f);review(f);args={}
    if kind in ('author','ticket','session'):
        scope=deepcopy(f['review_scope']);scope[{'author':'agent_id','ticket':'ticket_id','session':'board_session_id'}[kind]] = f['scope']['agent_id'] if kind=='author' else 'different'
        args['scope']=scope
    elif kind=='context':args['context']=dict(fingerprint='b'*64)
    elif kind=='material':args['material']='different'
    elif kind=='delivery':
        selection=deepcopy(f['review']['request']['selection']);selection['delivery_sha']=f['grant'].base_commit;args['selection']=selection
    else:args['workspace']=str(f['checkout'])
    with pytest.raises(DevelopmentError):guard(f,**args)


class Crash(BaseException):pass


@pytest.mark.parametrize('boundary',['object','ref'])
def test_interrupted_commit_is_retained_never_adopted_or_retried(delivery,monkeypatch,boundary):
    f=delivery;capture(f);test_receipt(f);original=f['delivery']._write_object
    def effect_then_crash(*args):original(*args);raise Crash()
    if boundary=='object':monkeypatch.setattr(f['delivery'],'_write_object',effect_then_crash)
    else:
        command=f['controller']._git
        def command_then_crash(args,**kw):
            result=command(args,**kw)
            if args[0]=='update-ref':raise Crash()
            return result
        monkeypatch.setattr(f['controller'],'_git',command_then_crash)
    with pytest.raises(Crash):
        f['delivery'].finalize(operation_id=f['op'],snapshot_sha256=f['snapshot_sha256'],test_evidence=f['evidence'],message='Selected')
    reopened=LocalGitDeliveryController(f['controller'],f['delivery_root'],f['review_root'],test_guard=f['test_guard'])
    monkeypatch.setattr(reopened,'_write_object',lambda *_:pytest.fail('replayed ambiguous effect'))
    with pytest.raises(DevelopmentError):
        reopened.finalize(operation_id=f['op'],snapshot_sha256=f['snapshot_sha256'],test_evidence=f['evidence'],message='Selected')
    assert (f['checkout']/'example.py').read_text()=='value = 2\n'


def test_interrupted_export_is_retained_never_recreated(delivery,monkeypatch):
    f=delivery;seal(f);original=PrivateDirectory._write
    def write_then_crash(fd,data):
        original(fd,data)
        if data==b'value = 2\n':raise Crash()
    monkeypatch.setattr(PrivateDirectory,'_write',staticmethod(write_then_crash))
    with pytest.raises(Crash):review(f)
    op=f['review_op']
    monkeypatch.setattr(PrivateDirectory,'_write',staticmethod(original))
    with pytest.raises(DevelopmentError):
        f['delivery'].prepare_review(operation_id=f['op'],review_operation_id=op,reviewer_scope=f['review_scope'],context_sha256='a'*64,material=f['material'])
    assert (f['review_root']/op/'export/example.py').read_text()=='value = 2\n'


def test_metadata_hook_added_after_capture_is_not_a_commit_input(delivery):
    f=delivery;capture(f);test_receipt(f)
    hooks=f['checkout']/'.git/hooks';hooks.mkdir(mode=0o700,exist_ok=True)
    (hooks/'post-commit').write_text('must never execute\n')
    with pytest.raises(DevelopmentError):
        f['delivery'].finalize(operation_id=f['op'],snapshot_sha256=f['snapshot_sha256'],test_evidence=f['evidence'],message='Selected')


def test_material_size_and_author_self_review_are_refused(delivery):
    f=delivery;seal(f);review(f)
    with pytest.raises(DevelopmentError):
        f['delivery'].prepare_review(operation_id=f['op'],review_operation_id=str(uuid.uuid4()),
            reviewer_scope={**f['review_scope'],'agent_id':f['scope']['agent_id']},context_sha256='a'*64,material=f['material'])
    with pytest.raises(DevelopmentError):
        f['delivery'].prepare_review(operation_id=f['op'],review_operation_id=str(uuid.uuid4()),
            reviewer_scope=f['review_scope'],context_sha256='a'*64,material='x'*24001)


def test_test_guard_cannot_change_the_snapshot_while_confirming_its_receipts(delivery):
    f=delivery;capture(f);test_receipt(f)
    def modifying_guard(**_):
        (f['checkout']/'example.py').write_text('value = 3\n')
        return True
    f['delivery'].test_guard=modifying_guard
    with pytest.raises(DevelopmentError):
        f['delivery'].finalize(operation_id=f['op'],snapshot_sha256=f['snapshot_sha256'],test_evidence=f['evidence'],message='Selected')
    assert git(f['checkout'],'for-each-ref','refs/heads/codex/delivery-')==''
    assert (f['checkout']/'example.py').read_text()=='value = 3\n'


@pytest.mark.parametrize('target',['seal','review'])
def test_lost_durable_response_reopens_exactly_without_repeating_effects(delivery,monkeypatch,target):
    f=delivery
    if target=='seal':capture(f);test_receipt(f)
    else:seal(f)
    original=PrivateDirectory.replace
    def saved_then_crash(private,name,data):
        original(private,name,data)
        value=json.loads(data)
        if (target=='seal' and name=='delivery.json' and value['phase']=='sealed'
                or target=='review' and name=='review.json' and value['phase']=='ready'):
            raise Crash()
    monkeypatch.setattr(PrivateDirectory,'replace',saved_then_crash)
    with pytest.raises(Crash):
        if target=='seal':
            f['delivery'].finalize(operation_id=f['op'],snapshot_sha256=f['snapshot_sha256'],test_evidence=f['evidence'],message='Explicit synthetic change')
        else:review(f)
    monkeypatch.setattr(PrivateDirectory,'replace',original)
    monkeypatch.setattr(f['delivery'],'_write_object',lambda *_:pytest.fail('Repeated commit effect'))
    if target=='seal':
        value=f['delivery'].finalize(operation_id=f['op'],snapshot_sha256=f['snapshot_sha256'],test_evidence=f['evidence'],message='Explicit synthetic change')
        assert git(f['checkout'],'rev-parse',value['ref'])==value['delivery_sha']
    else:
        value=f['delivery'].prepare_review(operation_id=f['op'],review_operation_id=f['review_op'],
            reviewer_scope=f['review_scope'],context_sha256='a'*64,material=f['material'])
        f['review']=value
        assert guard(f) is True


@pytest.mark.parametrize('path',['../outside','/outside','.git/config','.git/hooks/pre-commit','private/.ssh/key','dir\\escape'])
def test_escaping_and_private_selected_paths_are_refused(delivery,path):
    f=delivery;f['changes'][0]['path']=path
    with pytest.raises(DevelopmentError):capture(f)


def test_gitlink_mode_is_not_a_selected_regular_file(delivery):
    f=delivery;f['changes'][0]['mode']='160000'
    with pytest.raises(DevelopmentError):capture(f)


def test_two_deliveries_have_independent_commits_records_and_exports(delivery):
    f=delivery;seal(f);review(f)
    other=dict(f)
    other['scope']={**f['scope'],'ticket_id':'ticket-two','session_id':'session-two'}
    other['op'],_=planned(f,scope=other['scope'])
    other['checkout']=Path(f['controller'].materialize(other['op'])['checkout'])
    (other['checkout']/'example.py').write_text('value = 2\n')
    seal(other);review(other)
    assert f['seal']['ref']!=other['seal']['ref']
    assert f['review']['workspace']!=other['review']['workspace']
    assert f['snapshot_sha256']!=other['snapshot_sha256']
    assert guard(f) is guard(other) is True


def test_same_operation_requires_the_actual_workspace_lock(delivery):
    f=delivery
    with f['controller']._locked(f['op']):
        with pytest.raises(DevelopmentError,match='git_workspace_busy'):capture(f)
    assert list(f['delivery_root'].iterdir())==[]


@pytest.mark.parametrize('field',['owner_id','project_id','ticket_id','session_id','agent_id','profile_version'])
def test_capture_never_changes_the_original_operation_scope(delivery,field):
    f=delivery;f['scope']={**f['scope'],field:2 if field=='profile_version' else 'different'}
    with pytest.raises(DevelopmentError):capture(f)
    assert list(f['delivery_root'].iterdir())==[]


def test_overlapping_roots_are_refused(delivery):
    f=delivery
    with pytest.raises(DevelopmentError):
        LocalGitDeliveryController(f['controller'],f['state'],f['review_root'],test_guard=f['test_guard'])


@pytest.mark.parametrize('boundary',['before_capture','after_capture'])
def test_staged_index_mutation_is_never_a_delivery_input(delivery,boundary):
    f=delivery
    if boundary=='after_capture':capture(f);test_receipt(f)
    git(f['checkout'],'add','example.py')
    staged=(f['checkout']/'.git/index').read_bytes()
    with pytest.raises(DevelopmentError):
        if boundary=='before_capture':capture(f)
        else:f['delivery'].finalize(operation_id=f['op'],snapshot_sha256=f['snapshot_sha256'],test_evidence=f['evidence'],message='Selected')
    assert (f['checkout']/'.git/index').read_bytes()==staged


def test_replaced_record_root_cannot_be_adopted_after_controller_callback(delivery):
    f=delivery;capture(f);test_receipt(f)
    original=f['test_guard']
    def replaced_root(**kw):
        moved=f['delivery_root'].with_name('deliveries-moved')
        f['delivery_root'].rename(moved)
        f['delivery_root'].mkdir(mode=0o700)
        # Preserve the operation directory identity while changing its root.
        (moved/f['op']).rename(f['delivery_root']/f['op'])
        return original(**kw)
    f['delivery'].test_guard=replaced_root
    with pytest.raises(DevelopmentError,match='git_delivery_root_changed'):
        f['delivery'].finalize(operation_id=f['op'],snapshot_sha256=f['snapshot_sha256'],test_evidence=f['evidence'],message='Selected')
    assert git(f['checkout'],'for-each-ref','refs/heads/codex/delivery-')==''


@pytest.mark.parametrize('mutation',['contents','export_identity'])
def test_final_test_verification_cannot_make_an_old_export_check_current(delivery,mutation):
    f=delivery;seal(f);review(f);original=f['test_guard'];calls=[]
    export=Path(f['review']['workspace'])
    def interleaved(**kw):
        verified=original(**kw);calls.append(True)
        if len(calls)==2:
            if mutation=='contents':
                target=export/'example.py';target.chmod(0o600)
                target.write_text('value = 9000\n');target.chmod(0o400)
            else:
                export.rename(export.with_name('retained-original-export'))
                export.mkdir(mode=0o700)
                target=export/'example.py';target.write_text('value = 2\n');target.chmod(0o400)
                export.chmod(0o500)
        return verified
    f['delivery'].test_guard=interleaved
    with pytest.raises(DevelopmentError):guard(f)
    assert len(calls)==2
