"""Real SQLite transactions and authenticated actors; no live grants or Runtime."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from board import Board, BoardError
from hierarchy_store import HierarchyStore, migrate_hierarchy
from test_hierarchy_policy import policy, child


@pytest.fixture
def setup(tmp_path):
    b = Board(tmp_path / 'board')
    b.ensure_owner('alice', 'Alice'); b.ensure_owner('bob', 'Bob')
    human = dict(kind='human', owner_id='alice')
    other = dict(kind='human', owner_id='bob')
    b.set_policy(human, True, True, True, True,
                 ['repo.read_file', 'repo.patch_file', 'repo.run_test'], ['repo:work'])
    project = b.create_project(human, 'Project', ['alice'], idempotency_key='project')
    with b._tx() as db:
        migrate_hierarchy(db)
    store = HierarchyStore(b)
    def agent(name):
        p = b.create_agent_profile(human, name, project_ids=[project['id']], policy=dict(
            allow_read=True, allow_messages=True, allow_requests=True,
            actions=['repo.read_file', 'repo.patch_file', 'repo.run_test'],
            resources=['repo:work'], autonomy='queue'))
        device = b.create_device(human, name, [])
        p = b.bind_agent_profile(human, p['id'], p['version'], device['id'], name, 'codex')
        raw = b.authenticate(device['token'])
        b.register_agent(raw, name, name, [], 'codex', True, name)
        return p, b.actor_session(raw, name)
    parent, parent_actor = agent('parent')
    target, target_actor = agent('target')
    def ceiling(**changes): return policy(project_ids=[project['id']], **changes)
    def subordinate(**changes): return child(project_ids=[project['id']], **changes)
    return dict(b=b, store=store, human=human, other=other, parent=parent, parent_actor=parent_actor,
        target=target, target_actor=target_actor, agent=agent, ceiling=ceiling, subordinate=subordinate)


def enabled(s, **changes):
    root_policy = s['ceiling'](**changes)
    s['store'].configure(s['human'], 0, root_policy, 'enable')
    return s['store'].create_root(s['human'], s['parent']['id'], root_policy, 'root')


def make_child(s, root, key='child', beneficiary=None):
    return s['store'].create_child(s['parent_actor'], root['id'], beneficiary or s['target']['id'],
                                  s['subordinate'](), root['version'], key)


def snapshot(s):
    with s['b']._connection() as db:
        return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
                for table in ('hierarchy_limits', 'hierarchy_delegations', 'hierarchy_receipts', 'audit')}


def test_no_implicit_root_and_only_the_human_configures_permissions(setup):
    s = setup
    with pytest.raises(BoardError, match='hierarchy_not_enabled'):
        s['store'].create_root(s['human'], s['parent']['id'], s['ceiling'](), 'root')
    for actor in (s['parent_actor'], dict(s['parent_actor'], companion_for='alice', kind='agent')):
        with pytest.raises(BoardError): s['store'].configure(actor, 0, s['ceiling'](), 'forged')
    assert not snapshot(s)['hierarchy_limits']


def test_root_child_counters_and_retry_are_durable_across_store_instances(setup):
    s = setup; root = enabled(s)
    result = make_child(s, root)
    before = snapshot(s)
    second_store = HierarchyStore(Board(s['b'].data_dir))
    assert second_store.create_child(s['parent_actor'], root['id'], s['target']['id'],
        s['subordinate'](), root['version'], 'child') == result
    assert snapshot(s) == before
    assert before['hierarchy_limits'][0][3] == json.dumps(
        {'projects': {'live': 0, 'created': 0}, 'profiles': {'live': 0, 'created': 0},
         'delegations': {'live': 2, 'created': 2}}, sort_keys=True, separators=(',', ':'))
    assert s['store'].inspect(s['target_actor'], result['id'])['ancestry'] == [
        dict(id=root['id'], version=1), dict(id=result['id'], version=1)]


def test_revoked_ancestor_refuses_reads_mutations_and_even_successful_retry(setup):
    s = setup; root = enabled(s); result = make_child(s, root)
    s['store'].change(s['human'], root['id'], 1, 'revoke', revoke=True)
    before = snapshot(s)
    with pytest.raises(BoardError, match='hierarchy_revoked'): make_child(s, root)
    with pytest.raises(BoardError, match='hierarchy_revoked'):
        s['store'].inspect(s['target_actor'], result['id'])
    assert snapshot(s) == before
    assert s['store'].inspect(s['human'], result['id'])['status'] == 'active'
    assert s['store'].inspect(s['human'], root['id'])['status'] == 'revoked'


def test_reductions_apply_without_rewriting_descendant_snapshots(setup):
    s = setup; root = enabled(s); result = make_child(s, root)
    saved = s['store'].inspect(s['human'], result['id'])
    reduced = s['ceiling'](actions=['repo.read_file'])
    s['store'].change(s['human'], root['id'], 1, 'reduce', policy=reduced)
    assert s['store'].inspect(s['target_actor'], result['id'])['effective']['actions'] == ['repo.read_file']
    assert s['store'].inspect(s['human'], result['id']) == saved
    with pytest.raises(BoardError): make_child(s, root, key='new')


def test_cross_owner_wrong_actor_and_cycles_refuse_without_audit_or_debit(setup):
    s = setup; root = enabled(s); before = snapshot(s)
    with pytest.raises(BoardError): s['store'].inspect(s['other'], root['id'])
    with pytest.raises(BoardError):
        s['store'].create_child(s['target_actor'], root['id'], s['parent']['id'], s['subordinate'](), 1, 'wrong')
    with pytest.raises(BoardError, match='hierarchy_provenance_cycle'):
        make_child(s, root, beneficiary=s['parent']['id'])
    assert snapshot(s) == before


def test_cas_and_key_conflicts_do_not_modify_state(setup):
    s = setup; root = enabled(s); make_child(s, root)
    before = snapshot(s)
    with pytest.raises(BoardError, match='hierarchy_idempotency_conflict'):
        s['store'].create_child(s['parent_actor'], root['id'], s['target']['id'],
            s['subordinate'](actions=['repo.read_file']), 1, 'child')
    with pytest.raises(BoardError, match='hierarchy_version_conflict'):
        s['store'].change(s['human'], root['id'], 2, 'stale', revoke=True)
    assert snapshot(s) == before


def test_interruption_after_quota_debit_rolls_back_objects_receipt_and_audit(setup, monkeypatch):
    s = setup; root = enabled(s); before = snapshot(s)
    original = s['store']._remember
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('fixture_lost_before_commit')
    monkeypatch.setattr(s['store'], '_remember', fail)
    with pytest.raises(RuntimeError): make_child(s, root)
    assert snapshot(s) == before


def test_concurrent_children_share_owner_and_ancestor_quota(setup):
    s = setup; root = enabled(s)
    limited = s['ceiling']()
    limited['quotas']['delegations'] = dict(live=2, created=2)
    s['store'].configure(s['human'], 1, limited, 'limit')
    barrier = threading.Barrier(2)
    def attempt(key):
        barrier.wait()
        try:
            child_policy = s['subordinate']()
            child_policy['quotas']['delegations'] = dict(live=2, created=2)
            return s['store'].create_child(s['parent_actor'], root['id'], s['target']['id'],
                child_policy, root['version'], key)['id']
        except BoardError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ['one', 'two']))
    assert sum(result == 'hierarchy_quota_exhausted' for result in results) == 1
    after = snapshot(s)
    assert len(after['hierarchy_delegations']) == 2
    assert json.loads(after['hierarchy_limits'][0][3])['delegations'] == dict(live=2, created=2)


def test_revocation_does_not_refund_cumulative_or_live_occupancy(setup):
    s = setup; root = enabled(s)
    limited = s['ceiling']()
    limited['quotas']['delegations'] = dict(live=1, created=1)
    s['store'].configure(s['human'], 1, limited, 'limit')
    s['store'].change(s['human'], root['id'], 1, 'revoke', revoke=True)
    with pytest.raises(BoardError):
        s['store'].create_root(s['human'], s['target']['id'], limited, 'replacement')


def test_parent_profile_pause_suspends_descendants_without_erasing_them(setup):
    s = setup; root = enabled(s); result = make_child(s, root)
    s['b'].update_agent_profile(s['human'], s['parent']['id'], s['parent']['version'], status='paused')
    before = snapshot(s)
    with pytest.raises(BoardError, match='hierarchy_ancestor_profile_unavailable'):
        s['store'].inspect(s['target_actor'], result['id'])
    assert snapshot(s) == before


def test_ancestor_quota_binds_even_while_owner_capacity_remains(setup):
    s = setup
    s['store'].configure(s['human'], 0, s['ceiling'](), 'enable')
    root_policy = s['ceiling']()
    root_policy['quotas']['delegations'] = dict(live=1, created=1)
    root = s['store'].create_root(s['human'], s['parent']['id'], root_policy, 'root')
    descendant_policy = s['subordinate']()
    descendant_policy['quotas']['delegations'] = dict(live=1, created=1)
    s['store'].create_child(s['parent_actor'], root['id'], s['target']['id'], descendant_policy, 1, 'first')
    before = snapshot(s)
    with pytest.raises(BoardError, match='hierarchy_quota_exhausted'):
        s['store'].create_child(s['parent_actor'], root['id'], s['target']['id'], descendant_policy, 1, 'second')
    assert snapshot(s) == before
    assert json.loads(before['hierarchy_limits'][0][3])['delegations']['created'] == 2


def test_owner_reduction_intersects_current_rights_without_deleting_history(setup):
    s = setup; root = enabled(s); result = make_child(s, root)
    old = s['store'].inspect(s['human'], result['id'])
    reduced = s['ceiling'](actions=['repo.read_file'], resources=[])
    s['store'].configure(s['human'], 1, reduced, 'reduce-owner')
    current = s['store'].inspect(s['target_actor'], result['id'])
    assert current['effective']['actions'] == ['repo.read_file']
    assert current['effective']['resources'] == []
    assert s['store'].inspect(s['human'], result['id']) == old


def test_store_schema_does_not_expose_an_incomplete_board_api(setup):
    s = setup
    assert not hasattr(s['b'], 'create_hierarchy_root')
    with s['b']._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 19
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def new_project(s, root, key='new-project', **changes):
    return s['store'].create_project_lead(s['parent_actor'], root['id'], 'New project', 'Project Lead',
        child(project_ids=[], **changes), root['version'], key)


def bind_created(s, result):
    b = s['b']; human = s['human']
    device = b.create_device(human, 'new-lead', [])
    b.bind_agent_profile(human, result['profile_id'], 1, device['id'], 'new-lead', 'codex')
    raw = b.authenticate(device['token'])
    b.register_agent(raw, 'new-lead', 'new-lead', [], 'codex', True, 'new-lead')
    return b.actor_session(raw, 'new-lead')


def test_new_project_lead_is_atomic_scoped_unbound_and_idempotent(setup):
    s = setup; root = enabled(s)
    result = new_project(s, root)
    assert new_project(s, root) == result
    with s['b']._connection() as db:
        profile = db.execute('SELECT * FROM agent_profiles WHERE id=?', (result['profile_id'],)).fetchone()
        assert json.loads(profile['project_ids']) == [result['project_id']]
        assert not db.execute('SELECT * FROM agent_profile_bindings WHERE profile_id=?', (result['profile_id'],)).fetchall()
        assert [r[0] for r in db.execute('SELECT owner_id FROM project_members WHERE project_id=?',
                                       (result['project_id'],))] == ['alice']
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        counters = json.loads(db.execute('SELECT counters FROM hierarchy_limits').fetchone()[0])
        assert counters['projects'] == dict(live=1, created=1)
        assert counters['profiles'] == dict(live=1, created=1)
    actor = bind_created(s, result)
    assert s['store'].inspect(actor, result['id'])['effective']['project_ids'] == [result['project_id']]
    # Explicit binding never broadens a static ancestor profile.
    with s['b']._connection() as db:
        projects = json.loads(db.execute('SELECT project_ids FROM agent_profiles WHERE id=?',
                                        (s['parent']['id'],)).fetchone()[0])
        assert result['project_id'] not in projects


def test_new_project_creation_rolls_back_every_object_and_ledger(setup, monkeypatch):
    s = setup; root = enabled(s)
    tables = ['projects', 'project_members', 'agent_profiles', 'agent_profile_versions',
              'hierarchy_limits', 'hierarchy_delegations', 'hierarchy_project_bindings', 'hierarchy_receipts', 'audit']
    def counts():
        with s['b']._connection() as db:
            return {t: [tuple(r) for r in db.execute('SELECT * FROM ' + t)] for t in tables}
    before = counts(); original = s['store']._remember
    def fail(*args):
        original(*args)
        raise RuntimeError('injected after all writes')
    monkeypatch.setattr(s['store'], '_remember', fail)
    with pytest.raises(RuntimeError): new_project(s, root)
    assert counts() == before


def test_created_scope_requires_current_creation_ceiling_and_revocation_blocks_retry(setup):
    s = setup; root = enabled(s); result = new_project(s, root)
    actor = bind_created(s, result)
    reduced = s['ceiling'](allow_new_projects=False)
    s['store'].change(s['human'], root['id'], 1, 'reduce-creation', policy=reduced)
    assert s['store'].inspect(actor, result['id'])['effective']['project_ids'] == []
    s['store'].change(s['human'], root['id'], 2, 'revoke', revoke=True)
    with pytest.raises(BoardError, match='hierarchy_revoked'): new_project(s, root)
    with pytest.raises(BoardError, match='hierarchy_revoked'): s['store'].inspect(actor, result['id'])


def test_project_creation_refuses_client_chosen_scope_and_overquota_without_orphans(setup):
    s = setup; root = enabled(s, quotas={k: dict(live=1 if k == 'projects' else 4, created=8)
                                       for k in ('projects', 'profiles', 'delegations')})
    small = {k: dict(live=1 if k == 'projects' else 4, created=8)
             for k in ('projects', 'profiles', 'delegations')}
    with pytest.raises(BoardError, match='scope_must_be_empty'):
        s['store'].create_project_lead(s['parent_actor'], root['id'], 'Project', 'Lead',
            s['subordinate'](), 1, 'chosen')
    new_project(s, root, quotas=small)
    before = snapshot(s)
    with pytest.raises(BoardError, match='hierarchy_quota_exhausted'):
        new_project(s, root, key='second', quotas=small)
    assert snapshot(s) == before


def test_parallel_project_constructors_share_one_owner_quota(setup):
    s = setup
    quotas = {k: dict(live=1 if k == 'projects' else 4, created=8)
              for k in ('projects', 'profiles', 'delegations')}
    root = enabled(s, quotas=quotas)
    gate = threading.Barrier(2)
    def run(key):
        gate.wait()
        try:
            return new_project(s, root, key=key, quotas=quotas)
        except BoardError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ['parallel-a', 'parallel-b']))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum('hierarchy_quota_exhausted' in str(r) for r in results) == 1
    with s['b']._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hierarchy_project_bindings').fetchone()[0] == 1
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_new_project_scope_tracks_owner_reduction_and_receipt_payload(setup):
    s = setup; root = enabled(s); result = new_project(s, root)
    actor = bind_created(s, result)
    with pytest.raises(BoardError, match='hierarchy_idempotency_conflict'):
        s['store'].create_project_lead(s['parent_actor'], root['id'], 'Changed', 'Project Lead',
                                      child(project_ids=[]), 1, 'new-project')
    s['store'].configure(s['human'], 1, s['ceiling'](allow_new_projects=False), 'owner-reduction')
    assert not s['store'].inspect(actor, result['id'])['effective']['project_ids']
    with pytest.raises(BoardError, match='hierarchy_operation_not_authorized'):
        new_project(s, root)
