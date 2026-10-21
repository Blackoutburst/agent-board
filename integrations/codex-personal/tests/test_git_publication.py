"""Real tiny local Git transports plus a closed fake gh command layer: no network."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import subprocess
import uuid

import pytest

from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.dev_journal import PrivateDirectory
from blackoutburst_agents.git_publication import (GitHubDraftPublisher, GitHubPublicationGrant,
                                                  REPOSITORY, REMOTE, API)
from test_git_workspace import setup, git
from test_git_delivery import delivery, seal


class Crash(BaseException): pass


@pytest.fixture
def publication(delivery):
    f=delivery;seal(f)
    root=f['state'].parent
    f['publication_root']=root/'publications';f['publication_root'].mkdir(mode=0o700)
    f['gh']=root/'approved-gh';f['gh'].write_text('#!/bin/sh\nexit 99\n');f['gh'].chmod(0o700)
    f['remote']=root/'synthetic-remote.git'
    git(root,'clone','--bare','--no-hardlinks',str(f['source']),str(f['remote']))
    f['publication_grant']=GitHubPublicationGrant(f['scope']['owner_id'],REPOSITORY,'main',f['grant'].base_commit)
    f.update(prs=[],calls=[],pushes=0,posts=0,actor='synthetic-user',auth=True,
             push_fault=None,post_fault=None,base_alias=None,pr_mutation=None)
    def make():
        value=GitHubDraftPublisher(f['delivery'],f['publication_root'],grant=f['publication_grant'],gh_executable=f['gh'])
        value._command=command
        return value
    def command(argv,*,cwd,git=False):
        # No unknown command can fall back to a live executable/network.
        f['calls'].append(list(argv))
        if git:
            assert argv[0]=='/usr/bin/git' and REMOTE in argv
            assert argv[1] in ('ls-remote','push')
            actual=[str(f['remote']) if part==REMOTE else part for part in argv]
            if argv[1]=='push':
                f['pushes']+=1
                durable=json.loads((f['publication_root']/f['op']/'publication.json').read_text())
                assert durable['phase']=='pushing'
                assert argv==['/usr/bin/git','push','--porcelain','--no-verify',
                    '--force-with-lease=refs/heads/codex/delivery-'+f['op']+':',REMOTE,
                    f['seal']['delivery_sha']+':refs/heads/codex/delivery-'+f['op']]
                if f['push_fault']=='before':raise Crash()
            result=subprocess.run(actual,cwd=cwd,env={'PATH':'/usr/bin:/bin','GIT_CONFIG_NOSYSTEM':'1',
                'GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_SYSTEM':'/dev/null','GIT_ALLOW_PROTOCOL':'file',
                'GIT_TERMINAL_PROMPT':'0'},stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            if argv[1]=='push' and f['push_fault']=='after':raise Crash()
            return result.returncode,result.stdout,result.stderr
        assert argv[0]==str(f['gh'])
        if argv[1:]==['auth','status','--hostname','github.com']:
            return (0,b'normal official authentication available\n',b'') if f['auth'] else (1,b'',b'not authenticated')
        assert argv[1:5]==['api','--hostname','github.com','--method']
        method,path=argv[5:7]
        if path=='user':data={'login':f['actor']}
        elif path==API:data={'full_name':REPOSITORY,'html_url':'https://github.com/'+REPOSITORY,
            'permissions':{'push':True},'archived':False,'disabled':False}
        elif path.startswith(API+'/pulls?'):
            assert method=='GET' and 'state=all' in path and 'Blackoutburst%3Acodex%2Fdelivery-' in path
            data=f['prs']
        elif path==API+'/pulls' and method=='POST':
            f['posts']+=1
            durable=json.loads((f['publication_root']/f['op']/'publication.json').read_text())
            assert durable['phase']=='creating'
            payload=json.loads(Path(argv[8]).read_text())
            assert argv[7]=='--input' and payload['draft'] is True
            if f['post_fault']=='before':raise Crash()
            data=dict(number=17,html_url='https://github.com/'+REPOSITORY+'/pull/17',state='open',
                draft=True,title=payload['title'],body=payload['body'],user={'login':f['actor']},
                head={'ref':payload['head'],'sha':f['seal']['delivery_sha'],'repo':{'full_name':REPOSITORY}},
                base={'ref':'main','sha':f['grant'].base_commit,'repo':{'full_name':REPOSITORY}})
            if f['pr_mutation']:f['pr_mutation'](data)
            f['prs'].append(data)
            if f['post_fault']=='after':raise Crash()
        else:pytest.fail('Unexpected command path: '+path)
        return 0,json.dumps(data).encode(),b''
    f['make_publisher']=make;f['publisher']=make()
    return f


def publish(f,**kwargs):
    args=dict(operation_id=f['op'],delivery_sha=f['seal']['delivery_sha'],
              title='Explicit synthetic draft',body='Controller-selected synthetic delivery evidence.')
    args.update(kwargs)
    return f['publisher'].publish(**args)


def test_real_local_push_and_verified_draft_keep_main_source_and_seal_unchanged(publication):
    f=publication;source_before=f['delivery'].capture(operation_id=f['op'],scope=f['scope'],changes=f['changes'])
    result=publish(f)
    assert result['status']=='complete' and result['pull_request']['url'].endswith('/pull/17')
    assert f['pushes']==f['posts']==1
    assert git(f['remote'],'rev-parse','refs/heads/main')==f['grant'].base_commit
    assert git(f['remote'],'rev-parse','refs/heads/'+result['branch'])==f['seal']['delivery_sha']
    assert git(f['checkout'],'rev-parse','HEAD')==f['grant'].base_commit
    assert f['delivery'].capture(operation_id=f['op'],scope=f['scope'],changes=f['changes'])==source_before
    assert (f['publication_root']/f['op']/'transport/.git/config').read_bytes().startswith(b'[core]')
    assert b'credential' not in (f['publication_root']/f['op']/'transport/.git/config').read_bytes()


def test_exact_cold_retry_reads_remote_without_another_effect(publication):
    f=publication;expected=publish(f);f['publisher']=f['make_publisher']()
    assert publish(f)==expected
    assert f['pushes']==f['posts']==1


@pytest.mark.parametrize('effect',['push','post'])
def test_lost_effect_response_is_reconciled_exactly_without_repeating(publication,effect):
    f=publication;f[effect+'_fault']='after'
    with pytest.raises(Crash):publish(f)
    f['publisher']=f['make_publisher']()
    assert publish(f)['status']=='complete'
    assert f['pushes']==f['posts']==1


@pytest.mark.parametrize('effect',['push','post'])
def test_intent_without_remote_proof_is_retained_without_second_effect(publication,effect):
    f=publication;f[effect+'_fault']='before'
    with pytest.raises(Crash):publish(f)
    before=(f['pushes'],f['posts'])
    f[effect+'_fault']=None;f['publisher']=f['make_publisher']()
    with pytest.raises(DevelopmentError,match='retained'):publish(f)
    assert (f['pushes'],f['posts'])==before


def test_crash_after_clone_never_adopts_existing_transport(publication,monkeypatch):
    f=publication;original=f['publisher']._clone
    def copied_then_crash(*args):original(*args);raise Crash()
    monkeypatch.setattr(f['publisher'],'_clone',copied_then_crash)
    with pytest.raises(Crash):publish(f)
    assert (f['publication_root']/f['op']/'transport').exists()
    f['publisher']=f['make_publisher']()
    with pytest.raises(DevelopmentError,match='copy_retained'):publish(f)
    assert f['pushes']==f['posts']==0


def test_occupied_branch_is_not_force_pushed_or_adopted(publication):
    f=publication;git(f['remote'],'update-ref','refs/heads/codex/delivery-'+f['op'],f['grant'].base_commit)
    with pytest.raises(DevelopmentError,match='branch_occupied'):publish(f)
    assert f['pushes']==f['posts']==0
    assert git(f['remote'],'rev-parse','refs/heads/codex/delivery-'+f['op'])==f['grant'].base_commit


def test_authentication_unavailable_precedes_private_copy_or_remote_mutation(publication):
    f=publication;f['auth']=False
    with pytest.raises(DevelopmentError,match='authentication_unavailable'):publish(f)
    assert not list(f['publication_root'].iterdir())
    assert f['pushes']==f['posts']==0


def test_missing_gh_is_a_clear_refusal_without_reading_an_account(publication):
    f=publication
    with pytest.raises(DevelopmentError,match='tool_unavailable'):
        GitHubDraftPublisher(f['delivery'],f['publication_root'],grant=f['publication_grant'],gh_executable=f['gh'].with_name('missing-gh'))
    assert not list(f['publication_root'].iterdir())


@pytest.mark.parametrize('field,new',[('owner_id','other-owner'),('repository','Other/project'),
    ('host','example.invalid'),('base_commit','a'*40)])
def test_grant_cannot_change_scope_or_destination(publication,field,new):
    f=publication
    with pytest.raises(DevelopmentError):
        f['publication_grant']=replace(f['publication_grant'],**{field:new})
        f['publisher']=f['make_publisher']();publish(f)
    assert f['pushes']==f['posts']==0


@pytest.mark.parametrize('field',['title','body','delivery_sha'])
def test_existing_request_cannot_be_repurposed(publication,field):
    f=publication;publish(f)
    with pytest.raises(DevelopmentError):publish(f,**{field:'a'*40 if field=='delivery_sha' else 'Different selection'})
    assert f['pushes']==f['posts']==1


@pytest.mark.parametrize('kind',['head','base','repo','draft','closed','url','body','author'])
def test_remote_pr_must_match_all_exact_pins(publication,kind):
    f=publication
    def mutate(row):
        if kind=='head':row['head']['sha']='a'*40
        elif kind=='base':row['base']['sha']='a'*40
        elif kind=='repo':row['head']['repo']['full_name']='Other/project'
        elif kind=='draft':row['draft']=False
        elif kind=='closed':row['state']='closed'
        elif kind=='url':row['html_url']='https://example.invalid/pull/17'
        elif kind=='body':row['body']='unrelated PR'
        else:row['user']['login']='other-user'
    f['pr_mutation']=mutate
    with pytest.raises(DevelopmentError,match='pr_mismatch'):publish(f)
    assert f['posts']==1
    with pytest.raises(DevelopmentError):publish(f)
    assert f['posts']==1


def test_base_move_refuses_publication_without_changing_remote(publication):
    f=publication
    git(f['remote'],'fetch',str(f['checkout']),f['seal']['ref'])
    git(f['remote'],'update-ref','refs/heads/main',f['seal']['delivery_sha'])
    with pytest.raises(DevelopmentError,match='base_changed'):publish(f)
    assert f['pushes']==f['posts']==0


def test_source_edit_after_sealing_refuses_without_clobbering_or_network(publication):
    f=publication;(f['checkout']/'example.py').write_text('unpublished later edit\n')
    with pytest.raises(DevelopmentError):publish(f)
    assert f['calls']==[] and f['pushes']==f['posts']==0
    assert (f['checkout']/'example.py').read_text()=='unpublished later edit\n'


def test_transport_replacement_cannot_be_adopted_on_retry(publication):
    f=publication;publish(f)
    path=f['publication_root']/f['op']/'transport/.git/config'
    path.write_text('[core]\n\thooksPath = /tmp/unapproved\n')
    with pytest.raises(DevelopmentError,match='transport_changed'):publish(f)
    assert f['pushes']==f['posts']==1


def test_environment_preserves_gh_account_location_but_rejects_checkout_transport_overrides(publication,monkeypatch):
    f=publication
    monkeypatch.setenv('GH_CONFIG_DIR','/private/normal-user-gh-location')
    monkeypatch.setenv('GIT_CONFIG_COUNT','1');monkeypatch.setenv('GIT_CONFIG_KEY_0','http.proxy')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0','https://unapproved.invalid')
    monkeypatch.setenv('GIT_EXEC_PATH',str(f['checkout']));monkeypatch.setenv('GH_TOKEN','never-export-this')
    monkeypatch.setenv('HTTPS_PROXY','https://unapproved.invalid')
    env=f['publisher']._environment(git=True)
    assert env['HOME']==os.environ['HOME'] and env['GH_CONFIG_DIR']==os.environ['GH_CONFIG_DIR']
    assert not ({'GH_TOKEN','HTTPS_PROXY','GIT_EXEC_PATH'} & set(env))
    options=[(env['GIT_CONFIG_KEY_'+str(n)],env['GIT_CONFIG_VALUE_'+str(n)]) for n in range(int(env['GIT_CONFIG_COUNT']))]
    assert ('http.proxy','') in options and ('http.followRedirects','false') in options
    assert options.count(('credential.helper',''))==1
    assert options[-1]==('credential.helper','!'+str(f['gh'])+' auth git-credential')


def test_same_delivery_is_actually_locked_during_publication(publication):
    f=publication
    with f['controller']._locked(f['op']):
        with pytest.raises(DevelopmentError,match='git_workspace_busy'):publish(f)
    assert not f['calls']


def test_lost_complete_response_reads_exact_pr_without_second_create(publication,monkeypatch):
    f=publication;original=PrivateDirectory.replace
    def saved_then_crash(private,name,data):
        original(private,name,data)
        if name=='publication.json' and json.loads(data)['phase']=='complete':raise Crash()
    monkeypatch.setattr(PrivateDirectory,'replace',saved_then_crash)
    with pytest.raises(Crash):publish(f)
    monkeypatch.setattr(PrivateDirectory,'replace',original)
    f['publisher']=f['make_publisher']()
    assert publish(f)['status']=='complete'
    assert f['pushes']==f['posts']==1


def test_final_test_callback_cannot_change_the_pr_request_file(publication):
    f=publication;original=f['test_guard'];mutations=[]
    def interleaved(**kw):
        valid=original(**kw)
        path=f['publication_root']/f['op']/'create-pr.json'
        if path.exists() and not mutations:
            request=json.loads(path.read_bytes());request['draft']=False
            path.write_text(json.dumps(request));mutations.append(True)
        return valid
    f['delivery'].test_guard=interleaved
    with pytest.raises(DevelopmentError,match='input_changed'):publish(f)
    assert mutations==[True] and f['pushes']==1 and f['posts']==0


def test_final_test_callback_cannot_rewrite_a_pending_publication_record(publication):
    f=publication;original=f['test_guard'];mutations=[]
    def interleaved(**kw):
        valid=original(**kw)
        path=f['publication_root']/f['op']/'publication.json'
        if path.exists() and not mutations:
            request=json.loads(path.read_bytes())
            if request['phase']=='prepared':
                request['reason']='unrecognized replacement'
                path.write_text(json.dumps(request,separators=(',',':'),sort_keys=True));mutations.append(True)
        return valid
    f['delivery'].test_guard=interleaved
    with pytest.raises(DevelopmentError,match='record_changed'):publish(f)
    assert mutations==[True] and f['pushes']==f['posts']==0


def test_preexisting_exact_head_is_verified_before_one_draft_create(publication):
    f=publication
    git(f['remote'],'fetch',str(f['checkout']),f['seal']['ref'])
    git(f['remote'],'update-ref','refs/heads/codex/delivery-'+f['op'],f['seal']['delivery_sha'])
    assert publish(f)['status']=='complete'
    assert f['pushes']==0 and f['posts']==1


def test_ambiguous_pr_list_never_creates_another_pr(publication):
    f=publication;publish(f);f['prs'].append(deepcopy(f['prs'][0]))
    with pytest.raises(DevelopmentError,match='pr_ambiguous'):publish(f)
    assert f['posts']==1


def test_actor_cannot_change_between_authentication_and_push(publication):
    f=publication;original=f['publisher']._api;seen=[]
    def changed_actor(path,**kw):
        result=original(path,**kw)
        if path=='user':
            seen.append(True)
            if len(seen)>1:result['login']='different-user'
        return result
    f['publisher']._api=changed_actor
    with pytest.raises(DevelopmentError,match='authentication_changed'):publish(f)
    assert f['pushes']==f['posts']==0


def test_untracked_transport_file_is_refused(publication):
    f=publication;publish(f)
    (f['publication_root']/f['op']/'transport/.gitattributes').write_text('* filter=unapproved\n')
    with pytest.raises(DevelopmentError,match='transport_changed'):publish(f)
    assert f['pushes']==f['posts']==1


def test_changed_pinned_tool_refuses_before_any_authentication(publication):
    f=publication;f['gh'].write_text('#!/bin/sh\nexit 0\n')
    with pytest.raises(DevelopmentError,match='pin_changed'):publish(f)
    assert f['calls']==[] and f['pushes']==f['posts']==0


@pytest.mark.parametrize('branch',['main/.hidden','main.lock','a..b','main//other','-option','main/..'])
def test_evasive_base_ref_is_refused_before_preparation(publication,branch):
    f=publication
    with pytest.raises(DevelopmentError,match='base_refused'):
        replace(f['publication_grant'],base_branch=branch).document()


def test_actual_bounded_command_uses_only_a_synthetic_executable(publication):
    f=publication
    f['gh'].write_text('#!/bin/sh\nprintf synthetic-output\n');f['gh'].chmod(0o700)
    value=GitHubDraftPublisher(f['delivery'],f['publication_root'],grant=f['publication_grant'],gh_executable=f['gh'])
    code,out,err=value._command([str(f['gh']),'synthetic-command'],cwd=f['publication_root'])
    assert (code,out,err)==(0,b'synthetic-output',b'')


def test_empty_authentication_output_is_not_a_success(publication):
    f=publication;original=f['publisher']._command
    def empty(argv,**kw):
        if argv[1:3]==['auth','status']:return 0,b'',b''
        return original(argv,**kw)
    f['publisher']._command=empty
    with pytest.raises(DevelopmentError,match='authentication_unavailable'):publish(f)
    assert not list(f['publication_root'].iterdir())


@pytest.mark.parametrize('field',['title','body'])
def test_known_secret_material_in_selected_pr_text_refuses_before_auth(publication,field):
    f=publication
    with pytest.raises(DevelopmentError,match='secret_content_refused'):
        publish(f,**{field:'ghp_'+'A'*24})
    assert f['calls']==[] and f['pushes']==f['posts']==0


def test_account_configuration_cannot_be_redirected_into_checkout(publication,monkeypatch):
    f=publication;monkeypatch.setenv('GH_CONFIG_DIR',str(f['checkout']))
    with pytest.raises(DevelopmentError,match='authentication_location_refused'):
        f['publisher']._environment(git=True)


def test_exact_unsent_request_file_can_resume_after_read_only_failure(publication,monkeypatch):
    f=publication;original=f['publisher']._before_effect;failed=[]
    def read_failure(*args,**kw):
        path=f['publication_root']/f['op']/'create-pr.json'
        if path.exists() and not failed:
            failed.append(True);raise DevelopmentError('synthetic_read_failure_before_create_intent')
        return original(*args,**kw)
    monkeypatch.setattr(f['publisher'],'_before_effect',read_failure)
    with pytest.raises(DevelopmentError):publish(f)
    assert f['posts']==0 and f['publisher'].status(f['op'])['phase']=='branch_confirmed'
    assert publish(f)['status']=='complete'
    assert f['pushes']==f['posts']==1


def test_actual_bounded_command_reaps_a_synthetic_process_on_timeout(publication,monkeypatch):
    f=publication;f['gh'].write_text('#!/bin/sh\nsleep 10\n');f['gh'].chmod(0o700)
    value=GitHubDraftPublisher(f['delivery'],f['publication_root'],grant=f['publication_grant'],gh_executable=f['gh'],timeout_seconds=1)
    original=subprocess.Popen;processes=[]
    def remember(*args,**kw):
        result=original(*args,**kw);processes.append(result);return result
    monkeypatch.setattr(subprocess,'Popen',remember)
    with pytest.raises(DevelopmentError,match='command_unconfirmed'):
        value._command([str(f['gh']),'synthetic-command'],cwd=f['publication_root'])
    assert len(processes)==1 and processes[0].poll() is not None


def test_branch_appearing_before_push_is_not_fast_forwarded_even_from_base(publication):
    f=publication;original=f['publisher']._command;inserted=[]
    def occupied_before_advertisement(argv,**kw):
        if argv[1]=='push' and not inserted:
            git(f['remote'],'update-ref','refs/heads/codex/delivery-'+f['op'],f['grant'].base_commit)
            inserted.append(True)
        return original(argv,**kw)
    f['publisher']._command=occupied_before_advertisement
    with pytest.raises(DevelopmentError):publish(f)
    assert inserted==[True] and f['posts']==0
    assert git(f['remote'],'rev-parse','refs/heads/codex/delivery-'+f['op'])==f['grant'].base_commit


def test_timeout_kills_owned_group_when_main_process_already_exited(publication,monkeypatch):
    f=publication;f['gh'].write_text('#!/bin/sh\nsleep 10 &\nexit 0\n');f['gh'].chmod(0o700)
    value=GitHubDraftPublisher(f['delivery'],f['publication_root'],grant=f['publication_grant'],gh_executable=f['gh'],timeout_seconds=1)
    popen=subprocess.Popen;killpg=os.killpg;processes=[];killed=[]
    def remember(*args,**kw):
        result=popen(*args,**kw);processes.append(result);return result
    def kill(group,number):
        result=killpg(group,number);killed.append((group,number));return result
    monkeypatch.setattr(subprocess,'Popen',remember);monkeypatch.setattr(os,'killpg',kill)
    try:
        with pytest.raises(DevelopmentError,match='command_unconfirmed'):
            value._command([str(f['gh']),'synthetic-command'],cwd=f['publication_root'])
        assert len(processes)==1 and processes[0].poll()==0
        assert (processes[0].pid,signal.SIGKILL) in killed
    finally:
        for process in processes:
            if (process.pid,signal.SIGKILL) not in killed:
                try:killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
