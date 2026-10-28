import pytest
from fastapi.testclient import TestClient

from app import create_app

LOCAL = "http://127.0.0.1:9140"


@pytest.fixture(autouse=True)
def no_filebrowser_map(monkeypatch):
    monkeypatch.delenv("BOARD_FILEBROWSER_MAP", raising=False)


@pytest.fixture
def local_app(tmp_path):
    return create_app(tmp_path / "data", base_url=LOCAL, filebrowser_url="", require_login=False,
                      owners={"local": "Local"})


def test_local_board_needs_no_login(local_app):
    with TestClient(local_app, base_url=LOCAL) as client:
        me = client.get("/api/me")
        assert me.status_code == 200, me.text
        assert me.json()["actor"]["owner_id"] == "local"
        assert me.json()["login_required"] is False
        client.headers["X-CSRF-Token"] = me.json()["csrf_token"]
        created = client.post("/api/threads", json={"title": "Local work", "participants": ["local"],
                                                    "idempotency_key": "local-thread"})
        assert created.status_code == 200, created.text
        assert client.post("/api/logout").json() == {"ok": True}
        assert client.post("/api/login", json={"username": "local", "password": "whatever-pass"}).status_code == 409


def test_local_board_still_requires_csrf_and_same_origin(local_app):
    with TestClient(local_app, base_url=LOCAL) as client:
        token = client.get("/api/me").json()["csrf_token"]
        body = {"title": "x", "participants": ["local"], "idempotency_key": "k"}
        assert client.post("/api/threads", json=body).status_code == 403
        rejected = client.post("/api/threads", json=body,
                               headers={"X-CSRF-Token": token, "Origin": "https://evil.example"})
        assert rejected.status_code == 403


def test_local_board_rejects_foreign_host_names(local_app):
    # A rebound DNS name reaching the loopback port must not act as the local owner.
    with TestClient(local_app, base_url="http://testserver") as client:
        assert client.get("/api/me").status_code in (400, 403)


def test_no_login_refuses_public_base_url(tmp_path, monkeypatch):
    monkeypatch.delenv("BOARD_ALLOW_REMOTE_WITHOUT_LOGIN", raising=False)
    with pytest.raises(RuntimeError):
        create_app(tmp_path / "data", base_url="https://board.example.org", require_login=False,
                   owners={"local": "Local"})


def test_login_mode_still_requires_a_session(tmp_path):
    app = create_app(tmp_path / "data", base_url="http://testserver", filebrowser_url="",
                     allow_local_passwords=True, require_login=True, owners={"local": "Local"})
    with TestClient(app) as client:
        assert client.get("/api/me").status_code == 401
