"""Test-only reconstruction of stores predating the server-control schema."""


SERVER_SCHEMA14_TABLES = (
    'server_control_receipts',
    'server_instance_commands',
    'server_instances',
    'server_hosts',
)

PROVISIONING_SCHEMA16_TABLES = (
    'server_provisioning_barriers',
    'server_provisioning_receipts',
    'server_provisioning_requests',
    'server_provisioning_grants',
)


def _drop_empty(db, names):
    for name in names:
        assert db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0] == 0, (
            'cannot discard server state while constructing a legacy fixture')
    for name in names:
        db.execute('DROP TABLE ' + name)


def remove_provisioning_schema16_tables(db):
    """Test-only downgrade preparation; never discard a grant or barrier."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    _drop_empty(db, tuple(name for name in PROVISIONING_SCHEMA16_TABLES if name in tables))


def remove_server_schema14_tables(db):
    """Remove only the empty, known schema14 tables from a current fixture.

    Also remove later provisioning and admission-refusal tables only when empty.
    Inspect all tables before the first DROP, so a fixture with real server
    state or an unexpected shape fails without partially deleting its state.
    The order respects foreign keys. The production migrator is unchanged and
    must subsequently recreate all four tables through its ordinary path.
    """
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(SERVER_SCHEMA14_TABLES) <= tables, 'fixture must start with schema14 tables'
    removable = tuple(name for name in PROVISIONING_SCHEMA16_TABLES if name in tables) + SERVER_SCHEMA14_TABLES + tuple(
        name for name in ('runtime_admission_refusals',) if name in tables)
    _drop_empty(db, removable)
    remaining = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not remaining.intersection(removable)
