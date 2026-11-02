import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

spec = importlib.util.spec_from_file_location("projects_release", Path(__file__).parents[1] / "deploy/apply_conversations_release.py")
release_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_module)


@pytest.fixture
def release_fixture(tmp_path):
    source = tmp_path / "source"
    release = tmp_path / "release"
    data = tmp_path / "data"
    source.mkdir()
    release.mkdir()
    data.mkdir()
    previous = {}
    files = {}
    for relative in release_module.PINS:
        path = source / relative
        path.parent.mkdir(exist_ok=True)
        if relative == "projects.py":
            previous[relative] = None
        else:
            path.write_text("# original\n")
            previous[relative] = release_module.digest(path)
    for relative in release_module.RUNTIME:
        path = release / "payload" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# candidate\n")
        files[relative] = release_module.digest(path)
    (release / "manifest.json").write_text(json.dumps({"format": "blackoutburst-board-conversations-v1", "files": files, "previous": previous}))
    with sqlite3.connect(data / "board.sqlite3") as db:
        db.executescript("CREATE TABLE messages(body TEXT); INSERT INTO messages VALUES('existing message'); PRAGMA user_version=1;")
    events = []

    def migrate():
        with sqlite3.connect(data / "board.sqlite3") as db:
            db.executescript("CREATE TABLE projects(name TEXT); INSERT INTO projects VALUES('new'); PRAGMA user_version=2;")

    def health():
        events.append("health")

    def run(**overrides):
        values = dict(release=release, source=source, data=data, backup_root=tmp_path / "backups",
                      control=events.append, migrate=migrate, health=health, preflight=lambda: None)
        values.update(overrides)
        return release_module.apply_release(**values)

    return source, release, data, events, run


def database_contents(data):
    with sqlite3.connect(data / "board.sqlite3") as db:
        return db.execute("PRAGMA user_version").fetchone()[0], db.execute("SELECT body FROM messages").fetchall()


def test_success_snapshots_real_database_and_preserves_existing_messages(release_fixture):
    source, release, data, events, run = release_fixture
    snapshot = run()
    assert events == ["stop", "start", "health"]
    assert database_contents(data) == (2, [("existing message",)])
    assert database_contents(snapshot) == (1, [("existing message",)])
    assert (source / "projects.py").read_text() == "# candidate\n"
    assert (snapshot / "source/app.py").read_text() == "# original\n"
    assert (snapshot.stat().st_mode & 0o777) == 0o700
    assert (snapshot / "applied.json").exists()


def test_failed_startup_restores_source_database_and_retains_failed_state(release_fixture):
    source, release, data, events, run = release_fixture

    def failed_health():
        with sqlite3.connect(data / "board.sqlite3") as db:
            db.execute("INSERT INTO messages VALUES('accepted during failed startup')")
        raise RuntimeError("health failure")

    with pytest.raises(RuntimeError, match="health failure"):
        run(health=failed_health)
    assert events == ["stop", "start", "stop", "start"]
    assert database_contents(data) == (1, [("existing message",)])
    assert (source / "app.py").read_text() == "# original\n"
    assert not (source / "projects.py").exists()
    snapshot = next((source.parent / "backups").iterdir())
    with sqlite3.connect(snapshot / "failed-board.sqlite3") as db:
        assert db.execute("SELECT body FROM messages").fetchall()[-1] == ("accepted during failed startup",)
    assert not (snapshot / "applied.json").exists()


def test_migration_corruption_cannot_prevent_rollback(release_fixture):
    source, _, data, events, run = release_fixture

    def corrupt():
        (data / "board.sqlite3").write_bytes(b"bad migration")
        raise RuntimeError("broken migration")

    with pytest.raises(RuntimeError, match="broken migration"):
        run(migrate=corrupt)
    assert database_contents(data) == (1, [("existing message",)])
    assert (source / "app.py").read_text() == "# original\n"
    assert events == ["stop", "stop", "start"]


@pytest.mark.parametrize("relative", ["app.py", "platform_auth.py", "web/style.css"])
def test_concurrent_source_change_refused_before_service_stop(release_fixture, relative):
    source, _, _, events, run = release_fixture
    (source / relative).write_text("# colleague change\n")
    with pytest.raises(RuntimeError, match="Source changed"):
        run()
    assert events == []
    assert (source / relative).read_text() == "# colleague change\n"


def test_source_change_during_preflight_refused(release_fixture):
    source, _, _, events, run = release_fixture
    with pytest.raises(RuntimeError, match="Source changed"):
        run(preflight=lambda: (source / "app.py").write_text("# concurrent\n"))
    assert events == []


def test_invalid_payload_refused_before_service_stop(release_fixture):
    _, release, _, events, run = release_fixture
    (release / "payload/app.py").write_text("# changed candidate\n")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        run()
    assert events == []


def test_failed_preflight_does_not_stop_service(release_fixture):
    _, _, _, events, run = release_fixture
    def fail():
        raise RuntimeError("preflight failure")
    with pytest.raises(RuntimeError, match="preflight failure"):
        run(preflight=fail)
    assert events == []


def test_symlink_payload_refused(release_fixture):
    source, release, _, events, run = release_fixture
    (release / "payload/app.py").unlink()
    (release / "payload/app.py").symlink_to(source / "app.py")
    with pytest.raises(RuntimeError, match="regular file"):
        run()
    assert events == []


def test_source_edit_during_database_snapshot_is_preserved(release_fixture, monkeypatch):
    source, _, data, events, run = release_fixture
    snapshot = release_module.database_snapshot

    def concurrent(database, destination):
        snapshot(database, destination)
        (source / "app.py").write_text("# colleague edit during snapshot\n")

    monkeypatch.setattr(release_module, "database_snapshot", concurrent)
    with pytest.raises(RuntimeError, match="Source changed"):
        run()
    assert events == ["stop", "start"]
    assert (source / "app.py").read_text() == "# colleague edit during snapshot\n"
    assert (source / "board.py").read_text() == "# original\n"
    assert not (source / "projects.py").exists()
    assert database_contents(data)[0] == 1


def test_source_edit_during_source_backup_is_preserved(release_fixture, monkeypatch):
    source, _, data, events, run = release_fixture
    copy = release_module.shutil.copy2

    def concurrent(original, saved, *args, **kwargs):
        result = copy(original, saved, *args, **kwargs)
        if original == source / "app.py":
            original.write_text("# colleague edit during source backup\n")
        return result

    monkeypatch.setattr(release_module.shutil, "copy2", concurrent)
    with pytest.raises(RuntimeError, match="Source changed"):
        run()
    assert events == ["stop", "start"]
    assert (source / "app.py").read_text() == "# colleague edit during source backup\n"
    assert not (source / "projects.py").exists()
    assert database_contents(data)[0] == 1


def test_late_target_check_refuses_change_while_copy_is_staged(release_fixture, monkeypatch):
    source, release, data, events, run = release_fixture
    copy = release_module.shutil.copyfileobj

    def concurrent(incoming, outgoing, *args, **kwargs):
        result = copy(incoming, outgoing, *args, **kwargs)
        if incoming.name == str(release / "payload/app.py"):
            (source / "app.py").write_text("# concurrent staging edit\n")
        return result

    monkeypatch.setattr(release_module.shutil, "copyfileobj", concurrent)
    with pytest.raises(RuntimeError, match="Source changed"):
        run()
    assert events == ["stop", "start"]
    assert (source / "app.py").read_text() == "# concurrent staging edit\n"
    assert database_contents(data)[0] == 1


def test_partial_install_rolls_back_only_installed_files(release_fixture, monkeypatch):
    source, release, data, events, run = release_fixture
    copy = release_module.atomic_copy

    def concurrent(original, target, *args, **kwargs):
        result = copy(original, target, *args, **kwargs)
        if original == release / "payload/app.py":
            (source / "board.py").write_text("# uninstalled colleague edit\n")
        return result

    monkeypatch.setattr(release_module, "atomic_copy", concurrent)
    with pytest.raises(RuntimeError, match="Source changed"):
        run()
    assert events == ["stop", "stop", "start"]
    assert (source / "app.py").read_text() == "# original\n"
    assert (source / "board.py").read_text() == "# uninstalled colleague edit\n"
    assert not (source / "projects.py").exists()
    assert database_contents(data)[0] == 1


@pytest.mark.parametrize("fail_health", [True, False])
def test_installed_source_edit_during_health_stays_intact_and_stopped(release_fixture, fail_health):
    source, _, data, events, run = release_fixture

    def concurrent_health():
        (source / "app.py").write_text("# colleague runtime change\n")
        with sqlite3.connect(data / "board.sqlite3") as db:
            db.execute("INSERT INTO messages VALUES('accepted during startup')")
        if fail_health:
            raise RuntimeError("health failure")

    with pytest.raises(RuntimeError, match="service remains stopped.*Private snapshot"):
        run(health=concurrent_health)
    assert events == ["stop", "start", "stop"]
    assert (source / "app.py").read_text() == "# colleague runtime change\n"
    assert (source / "board.py").read_text() == "# candidate\n"
    assert (source / "projects.py").read_text() == "# candidate\n"
    assert database_contents(data) == (2, [("existing message",), ("accepted during startup",)])
    backup = next((source.parent / "backups").iterdir())
    assert database_contents(backup) == (1, [("existing message",)])
    with sqlite3.connect(backup / "failed-board.sqlite3") as db:
        assert db.execute("SELECT body FROM messages").fetchall()[-1] == ("accepted during startup",)
    record = json.loads((backup / "manual-recovery.json").read_text())
    assert record["service_stopped"] is True
    assert set(record["installed"]) == release_module.RUNTIME
    assert record["restored"] == []


def test_late_source_edit_during_rollback_is_preserved(release_fixture, monkeypatch):
    source, _, data, events, run = release_fixture
    copy = release_module.shutil.copyfileobj

    def concurrent(incoming, outgoing, *args, **kwargs):
        result = copy(incoming, outgoing, *args, **kwargs)
        if "/backups/" in incoming.name and incoming.name.endswith("/source/app.py"):
            (source / "app.py").write_text("# colleague edit during rollback\n")
        return result

    def fail():
        raise RuntimeError("health failure")

    monkeypatch.setattr(release_module.shutil, "copyfileobj", concurrent)
    with pytest.raises(RuntimeError, match="service remains stopped.*Private snapshot"):
        run(health=fail)
    assert events == ["stop", "start", "stop"]
    assert (source / "app.py").read_text() == "# colleague edit during rollback\n"
    assert database_contents(data)[0] == 2


def test_failed_restore_keeps_service_stopped(release_fixture, monkeypatch):
    source, _, data, events, run = release_fixture
    copy = release_module.atomic_copy

    def failed_copy(original, target, *args, **kwargs):
        if "/backups/" in str(original) and str(original).endswith("/source/app.py"):
            raise OSError("simulated restore failure")
        return copy(original, target, *args, **kwargs)

    def fail():
        raise RuntimeError("health failure")

    monkeypatch.setattr(release_module, "atomic_copy", failed_copy)
    with pytest.raises(RuntimeError, match="service remains stopped.*simulated restore failure"):
        run(health=fail)
    assert events == ["stop", "start", "stop"]
    assert (source / "app.py").read_text() == "# candidate\n"
    assert database_contents(data)[0] == 2
