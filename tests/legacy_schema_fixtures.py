"""Test-only reconstruction of stores predating the server-control schema."""


SERVER_SCHEMA14_TABLES = (
    'server_control_receipts',
    'server_instance_commands',
    'server_instances',
    'server_hosts',
)


def remove_server_schema14_tables(db):
    """Remove only the empty, known schema14 tables from a current fixture.

    Also remove the later admission-refusal table when present and empty.
    Inspect all tables before the first DROP, so a fixture with real server
    state or an unexpected shape fails without partially deleting its state.
    The order respects foreign keys. The production migrator is unchanged and
    must subsequently recreate all four tables through its ordinary path.
    """
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(SERVER_SCHEMA14_TABLES) <= tables, 'fixture must start with schema14 tables'
    removable = SERVER_SCHEMA14_TABLES + tuple(
        name for name in ('runtime_admission_refusals',) if name in tables)
    for name in removable:
        assert db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0] == 0, (
            'cannot discard server state while constructing a legacy fixture')
    for name in removable:
        db.execute('DROP TABLE ' + name)
    remaining = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not remaining.intersection(removable)
