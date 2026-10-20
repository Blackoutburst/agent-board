"""Stopped recovery opens must not create or repair local authority."""
import hashlib
from pathlib import Path
import sqlite3

import pytest
from blackoutburst_agents.run_store import RunStore,StoreError


def contents(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('kind',['directory','file','turns','index','view','trigger','app','version','sidecar'])
def test_existing_only_refuses_missing_or_altered_authority_without_mutation(tmp_path,kind):
    path=tmp_path/'runs'
    if kind=='directory':
        with pytest.raises(StoreError):RunStore(path,existing_only=True)
        assert not path.exists();return
    with_store=RunStore(path);with_store.close();dbpath=path/'runs.sqlite3'
    if kind=='file':dbpath.unlink()
    elif kind=='sidecar':(path/'runs.sqlite3-wal').write_text('SYNTHETIC')
    else:
        with sqlite3.connect(dbpath) as db:
            if kind=='turns':db.execute('DROP TABLE turns')
            if kind=='index':db.execute('DROP INDEX one_running_turn')
            if kind=='view':db.execute('CREATE VIEW unexpected AS SELECT 1')
            if kind=='trigger':db.execute('CREATE TRIGGER unexpected AFTER UPDATE ON works BEGIN SELECT 1;END')
            if kind=='app':db.execute('PRAGMA application_id=1')
            if kind=='version':db.execute('PRAGMA user_version=1')
    before=contents(tmp_path)
    with pytest.raises(Exception):RunStore(path,existing_only=True)
    assert contents(tmp_path)==before


def test_existing_only_valid_database_keeps_exact_bytes_and_schema(tmp_path):
    path=tmp_path/'runs';store=RunStore(path);store.close();before=contents(tmp_path)
    store=RunStore(path,existing_only=True);store.close()
    assert contents(tmp_path)==before


@pytest.mark.parametrize('literal',["'RUNNING'","'run ning'"])
def test_semantically_different_index_literal_refused_without_mutation(tmp_path,literal):
    path=tmp_path/'runs';store=RunStore(path);store.close()
    with sqlite3.connect(path/'runs.sqlite3') as db:
        db.execute('DROP INDEX one_running_turn')
        db.execute('CREATE UNIQUE INDEX one_running_turn ON turns(work_id) WHERE status = '+literal)
    before=contents(tmp_path)
    with pytest.raises(StoreError):RunStore(path,existing_only=True)
    assert contents(tmp_path)==before


def test_sql_whitespace_cannot_merge_tokens_into_a_different_column_type(tmp_path):
    path=tmp_path/'runs';store=RunStore(path);store.close()
    with sqlite3.connect(path/'runs.sqlite3') as db:
        db.execute('PRAGMA writable_schema=ON')
        # A metadata-only fixture whose visible column names are identical but
        # SQL tokenization is no longer the known schema. Never opened normally.
        db.execute("UPDATE sqlite_master SET sql=replace(sql,'cancel_requested INTEGER NOT NULL','cancel_requested INTEGERNOT NULL') WHERE name='turns'")
        db.execute('PRAGMA writable_schema=OFF')
    before=contents(tmp_path)
    with pytest.raises(StoreError):RunStore(path,existing_only=True)
    assert contents(tmp_path)==before
