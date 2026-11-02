"""Synthetic REST workflows, with the same authentication as legacy discussions."""
import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import csrf_for
from test_api import application, login


def make_project(client, participants=None, key="project-fixture"):
    response = client.post("/api/projects", json={
        "name": "Plateforme", "participants": participants or ["blackoutburst", "thescreamer"],
        "description": "Projet synthétique", "context": "Décisions partagées",
        "repository_url": "https://example.test/board", "idempotency_key": key,
    })
    assert response.status_code == 200, response.text
    return response.json()


def make_ticket(client, project, **values):
    response = client.post(f"/api/projects/{project['id']}/tickets", json={
        "title": "Connexion Google", "acceptance_criteria": "Session test vérifiée",
        "idempotency_key": "ticket-fixture", **values,
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_project_ticket_discussion_claim_and_completion(application):
    with TestClient(application) as client:
        login(client)
        project = make_project(client)
        assert client.get("/api/projects", params={"query": "Plateforme"}).json()[0]["id"] == project["id"]
        updated = client.patch(f"/api/projects/{project['id']}", json={
            "expected_version": project["version"], "context": "Nouvelle décision",
        })
        assert updated.status_code == 200, updated.text
        assert updated.json()["context"] == "Nouvelle décision"
        stale = client.patch(f"/api/projects/{project['id']}", json={
            "expected_version": project["version"], "context": "Ne doit pas écraser",
        })
        assert stale.status_code == 409
        assert client.get(f"/api/projects/{project['id']}").json()["context"] == "Nouvelle décision"

        ticket = make_ticket(client, project, assignee_owner_id="blackoutburst")
        detail = client.get(f"/api/tickets/{ticket['id']}").json()
        assert detail["project"]["id"] == project["id"]
        assert detail["ticket"]["thread_id"] == ticket["thread_id"]
        thread = client.get(f"/api/threads/{ticket['thread_id']}").json()
        assert set(thread["thread"]["participants"]) == set(project["participants"])
        message = client.post(f"/api/threads/{ticket['thread_id']}/messages", json={
            "body": "Critères de validation reçus.", "idempotency_key": "ticket-discussion",
        })
        assert message.status_code == 200, message.text
        assert client.get(f"/api/projects/{project['id']}/tickets", params={"priority": "normal"}).json()[0]["id"] == ticket["id"]

        claimed = client.post(f"/api/tickets/{ticket['id']}/claim", json={"expected_version": ticket["version"]})
        assert claimed.status_code == 200, claimed.text
        ticket = claimed.json()
        assert ticket["status"] == "in_progress"
        assert ticket["claimant"]["owner_id"] == "blackoutburst"
        missing_proof = client.patch(f"/api/tickets/{ticket['id']}", json={
            "expected_version": ticket["version"], "status": "done", "outcome": "Tentative",
        })
        assert missing_proof.status_code == 400
        completed = client.patch(f"/api/tickets/{ticket['id']}", json={
            "expected_version": ticket["version"], "status": "done", "outcome": "Session validée",
            "evidence": "Fixture synthétique : assertions HTTP réussies",
        })
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "done"


def test_patch_omission_null_and_strict_types(application):
    with TestClient(application) as client:
        login(client)
        project = make_project(client)
        ticket = make_ticket(client, project, assignee_owner_id="thescreamer")
        changed = client.patch(f"/api/tickets/{ticket['id']}", json={
            "expected_version": ticket["version"], "description": "Une précision",
        })
        assert changed.status_code == 200, changed.text
        ticket = changed.json()
        assert ticket["assignee_owner_id"] == "thescreamer"
        cleared = client.patch(f"/api/tickets/{ticket['id']}", json={
            "expected_version": ticket["version"], "assignee_owner_id": None,
        })
        assert cleared.status_code == 200, cleared.text
        ticket = cleared.json()
        assert ticket["assignee_owner_id"] is None
        for changes in ({"title": None}, {"priority": "critical"}, {"release_claim": "true"},
                        {"owner_id": "thescreamer"}, {"expected_version": True}, {"expected_version": "1"}):
            invalid = client.patch(f"/api/tickets/{ticket['id']}", json={"expected_version": ticket["version"], **changes})
            assert invalid.status_code == 422, invalid.text
        for changes in ({"archived": "false"}, {"context": None}, {"participants": ["blackoutburst"]}):
            assert client.patch(f"/api/projects/{project['id']}", json={"expected_version": project["version"], **changes}).status_code == 422
        assert client.get(f"/api/tickets/{ticket['id']}").json()["ticket"]["version"] == ticket["version"]
        assert client.get("/api/projects", params={"query": "x" * 201}).status_code == 422


def test_all_project_routes_require_cookie_csrf_and_membership(application):
    outsider, anonymous = TestClient(application), TestClient(application)
    with TestClient(application) as member:
        login(member)
        login(outsider, "thescreamer")
        project = make_project(member, ["blackoutburst"])
        ticket = make_ticket(member, project)
        reads = ["/api/projects", f"/api/projects/{project['id']}",
                 f"/api/projects/{project['id']}/tickets", f"/api/tickets/{ticket['id']}"]
        for path in reads:
            assert anonymous.get(path).status_code == 401
        assert outsider.get("/api/projects").json() == []
        for path in reads[1:] + [f"/api/threads/{ticket['thread_id']}"]:
            assert outsider.get(path).status_code in {403, 404}
        mutations = [
            ("post", "/api/projects", {"name": "Another", "participants": ["blackoutburst"], "idempotency_key": "other"}),
            ("patch", f"/api/projects/{project['id']}", {"expected_version": 1, "name": "Bad"}),
            ("post", f"/api/projects/{project['id']}/tickets", {"title": "Bad", "idempotency_key": "other-ticket"}),
            ("patch", f"/api/tickets/{ticket['id']}", {"expected_version": 1, "title": "Bad"}),
            ("post", f"/api/tickets/{ticket['id']}/claim", {"expected_version": 1}),
        ]
        csrf = member.headers.pop("X-CSRF-Token")
        for method, path, payload in mutations:
            assert anonymous.request(method, path, json=payload).status_code == 401
            assert member.request(method, path, json=payload).status_code == 403
        member.headers["X-CSRF-Token"] = csrf
        for method, path, payload in mutations[1:]:
            assert outsider.request(method, path, json=payload).status_code in {403, 404}
        assert member.patch(f"/api/projects/{project['id']}",
            headers={"Origin": "https://attacker.invalid"}, json={"expected_version": 1, "name": "Bad"}).status_code == 403
        assert member.get(f"/api/projects/{project['id']}").json()["version"] == 1
        assert member.get(f"/api/tickets/{ticket['id']}").json()["ticket"]["version"] == 1


def test_existing_shared_auth_is_used_for_projects(application, monkeypatch):
    monkeypatch.setattr(app_module, "SHARED_AUTH", True)
    cookies_seen = []

    def verified_user(cookie):
        cookies_seen.append(cookie)
        return {"id": "fixture", "username": "TheScreamer", "role": "admin"} if "synthetic-central-session" in cookie else None

    monkeypatch.setattr(app_module, "platform_user", verified_user)
    with TestClient(application) as client:
        assert client.get("/api/projects").status_code == 401
        client.cookies.set("__Secure-blackout_session", "synthetic-central-session")
        client.headers["X-CSRF-Token"] = csrf_for("synthetic-central-session")
        project = make_project(client, ["thescreamer"])
        assert project["participants"] == ["thescreamer"]
        assert any("synthetic-central-session" in cookie for cookie in cookies_seen)
        assert client.post("/api/login", json={"username": "thescreamer", "password": "fixture"}).status_code == 403
