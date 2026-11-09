"""Schema20 migration on isolated historical fixtures, never a live Board."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from board import Board
from legacy_schema_fixtures import remove_hierarchy_schema20_tables, HIERARCHY_SCHEMA20_TABLES


def schema19(tmp_path):
    b = Board(tmp_path / 'board')
    b.ensure_owner('alice', 'Alice')
    human = dict(kind='human', owner_id='alice')
    p = b.create_project(human, 'Historical project', ['alice'], idempotency_key='old-project')
    b.create_ticket(human, p['id'], 'Historical work', idempotency_key='old-work')
    with b._tx() as db:
        remove_hierarchy_schema20_tables(db)
        db.execute('PRAGMA user_version=19')
    return b


def legacy_rows(b):
    with b._connection() as db:
        tables = sorted(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                        if r[0] not in HIERARCHY_SCHEMA20_TABLES)
        return {name: [tuple(r) for r in db.execute('SELECT * FROM "' + name + '"')] for name in tables}


def test_schema19_to20_preserves_all_existing_rows_and_grants_nothing(tmp_path):
    old = schema19(tmp_path); before = legacy_rows(old)
    current = Board(old.data_dir)
    assert legacy_rows(current) == before
    with current._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 20
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        for table in HIERARCHY_SCHEMA20_TABLES:
            assert db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0
    assert legacy_rows(Board(old.data_dir)) == before


def test_schema20_failure_rolls_back_tables_version_and_old_rows(tmp_path, monkeypatch):
    import hierarchy_store
    old = schema19(tmp_path); before = legacy_rows(old)
    original = hierarchy_store.migrate_hierarchy
    def failed(db):
        original(db)
        raise RuntimeError('migration interruption')
    monkeypatch.setattr(hierarchy_store, 'migrate_hierarchy', failed)
    with pytest.raises(RuntimeError, match='migration interruption'): Board(old.data_dir)
    assert legacy_rows(old) == before
    with old._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 19
        assert not set(HIERARCHY_SCHEMA20_TABLES) & {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    monkeypatch.setattr(hierarchy_store, 'migrate_hierarchy', original)
    assert Board(old.data_dir)


def test_concurrent_migrators_serialize_without_duplicate_tables_or_effects(tmp_path):
    old = schema19(tmp_path); before = legacy_rows(old)
    with ThreadPoolExecutor(max_workers=2) as pool:
        boards = list(pool.map(lambda _: Board(old.data_dir), range(2)))
    assert all(legacy_rows(b) == before for b in boards)
