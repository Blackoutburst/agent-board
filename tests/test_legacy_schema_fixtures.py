"""Legacy fixture reconstruction remains strict and does not discard records."""
import sqlite3

import pytest

from board import Board
from legacy_schema_fixtures import SERVER_SCHEMA14_TABLES, remove_server_schema14_tables


def test_empty_schema14_tables_are_absent_in_legacy_store_and_recreated(tmp_path):
    board = Board(tmp_path / 'legacy')
    with board._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 14
        remove_server_schema14_tables(db)
        db.execute('PRAGMA user_version=13')
        db.commit()
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not tables.intersection(SERVER_SCHEMA14_TABLES)
    migrated = Board(board.data_dir)
    with migrated._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 14
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(SERVER_SCHEMA14_TABLES) <= tables
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('nonempty', SERVER_SCHEMA14_TABLES)
def test_nonempty_table_refuses_before_any_drop(nonempty):
    with sqlite3.connect(':memory:') as db:
        for name in SERVER_SCHEMA14_TABLES:
            db.execute('CREATE TABLE ' + name + '(id TEXT PRIMARY KEY)')
        db.execute('INSERT INTO ' + nonempty + ' VALUES(?)', ('retained-fixture-row',))
        before = tuple(db.iterdump())
        with pytest.raises(AssertionError, match='cannot discard server state'):
            remove_server_schema14_tables(db)
        assert tuple(db.iterdump()) == before
