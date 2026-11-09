"""Human-only quota reconciliation, exact provenance, no live storage."""
import json
import pytest
from board import BoardError
from test_hierarchy_store import setup, enabled, new_project, bind_created, leaf_policy


def archive(s, result, key='archive'):
    return s['store'].archive_project_lead(s['human'], result['project_id'], 1, 1, key)


def test_archive_releases_only_live_once_and_prevents_unaccounted_reentry(setup):
    s=setup; root=enabled(s); result=new_project(s,root)
    with s['b']._connection() as db:
        before=json.loads(db.execute('SELECT counters FROM hierarchy_limits').fetchone()[0])
    s['store'].change(s['human'], root['id'], 1, 'revoke', revoke=True)
    saved=archive(s,result)
    assert archive(s,result)==saved
    with pytest.raises(BoardError,match='already_released'): archive(s,result,'other-key')
    with s['b']._connection() as db:
        after=json.loads(db.execute('SELECT counters FROM hierarchy_limits').fetchone()[0])
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    for kind in ('projects','profiles','delegations'):
        assert after[kind]['live']==before[kind]['live']-1
        assert after[kind]['created']==before[kind]['created']
    with pytest.raises(BoardError,match='reentry'):
        s['b'].update_agent_profile(s['human'],result['profile_id'],2,status='active')
    with pytest.raises(BoardError,match='reentry'):
        s['b'].update_project(s['human'],result['project_id'],2,archived=False)


def test_archive_refuses_agent_foreign_owner_and_unreleased_team(setup):
    s=setup; root=enabled(s); result=new_project(s,root)
    with pytest.raises(BoardError):
        s['store'].archive_project_lead(s['parent_actor'],result['project_id'],1,1,'agent')
    with pytest.raises(BoardError):
        s['store'].archive_project_lead(s['other'],result['project_id'],1,1,'foreign')
    lead=bind_created(s,result)
    s['store'].create_team_profile(lead,result['id'],'Coder','developer',leaf_policy(result['project_id']),1,'team')
    with pytest.raises(BoardError,match='descendants'):
        s['store'].archive_project_lead(s['human'],result['project_id'],1,2,'archive')


def test_archive_rolls_back_receipt_counters_profile_project_together(setup,monkeypatch):
    s=setup; root=enabled(s); result=new_project(s,root)
    remember=s['store']._remember
    def fail(*args):
        remember(*args); raise RuntimeError('after receipt')
    monkeypatch.setattr(s['store'],'_remember',fail)
    with pytest.raises(RuntimeError): archive(s,result)
    assert s['b'].get_project(s['human'],result['project_id'])['archived'] is False
    assert s['b'].get_agent_profile(s['human'],result['profile_id'])['status']=='active'
    monkeypatch.setattr(s['store'],'_remember',remember)
    assert archive(s,result)['version']==2


def test_archive_requires_normal_claim_release_and_current_versions(setup):
    s=setup; root=enabled(s); result=new_project(s,root); b=s['b']
    lead=bind_created(s,result)
    ticket=b.create_ticket(s['human'],result['project_id'],'Work',assignee_owner_id='alice',
        assignee_agent_id=result['profile_id'],resource='repo:work')
    claim=b.claim_ticket(lead,ticket['id'],ticket['version'])
    with pytest.raises(BoardError,match='claim_present'):
        s['store'].archive_project_lead(s['human'],result['project_id'],1,2,'archive')
    assert b.get_ticket(s['human'],ticket['id'])['ticket']['claimant']==claim['claimant']
    b.release_ticket(lead,ticket['id'],claim['version'],'release','No model executed in this fixture')
    with pytest.raises(BoardError,match='version_conflict'): archive(s,result)
    assert s['store'].archive_project_lead(s['human'],result['project_id'],1,2,'archive')['profile_version']==3


def test_project_archive_waits_for_real_runtime_cleanup_record(setup):
    from test_hierarchy_access import reserved_hierarchy
    s=setup; _,result,actor,reserved=reserved_hierarchy(s);b=s['b']
    # A normally released ticket does not mean the reserved Runtime is clean.
    ticket=b.get_ticket(s['human'],reserved['ticket_id'])['ticket']
    b.release_ticket(actor,ticket['id'],ticket['version'],'release','No model launched')
    with pytest.raises(BoardError,match='cleanup_unconfirmed'):
        s['store'].archive_project_lead(s['human'],result['project_id'],1,2,'archive')
    pending=b.finish_execution(actor,reserved['id'],reserved['revision'],'cancelled',False,'stop_requested')
    with pytest.raises(BoardError,match='cleanup_unconfirmed'):
        s['store'].archive_project_lead(s['human'],result['project_id'],1,2,'archive')
    b.finish_execution(actor,pending['id'],pending['revision'],'cancelled',True,'stop_requested')
    assert s['store'].archive_project_lead(s['human'],result['project_id'],1,2,'archive')['version']==2


def test_concurrent_archive_retries_release_one_owner_and_ancestor_debit(setup):
    from concurrent.futures import ThreadPoolExecutor
    s=setup;root=enabled(s);result=new_project(s,root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _: archive(s,result),range(2)))
    assert results[0]==results[1]
    with s['b']._connection() as db:
        owner=json.loads(db.execute('SELECT counters FROM hierarchy_limits').fetchone()[0])
        ancestor=json.loads(db.execute('SELECT counters FROM hierarchy_delegations WHERE id=?',(root['id'],)).fetchone()[0])
        assert owner['projects']==dict(live=0,created=1)
        assert ancestor['profiles']==dict(live=0,created=1)
        assert db.execute("SELECT COUNT(*) FROM hierarchy_receipts WHERE operation='archive_project_lead'").fetchone()[0]==1
