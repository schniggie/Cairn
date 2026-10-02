from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse

from fastapi.testclient import TestClient
import pytest

from cairn.ctfbridge.bridge import CtfBridge
from cairn.ctfbridge.client import CairnApi
from cairn.server import db
from cairn.server.app import app


class _TestClientSession:
    """A requests.Session stand-in backed by FastAPI's TestClient."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def request(self, method, url, params=None, json=None, timeout=None):
        parsed = urlparse(url)
        merged = parse_qs(parsed.query)
        for key, value in (params or {}).items():
            merged[key] = [str(value)]
        query = urlencode(merged, doseq=True)
        path = f"{parsed.path}?{query}" if query else parsed.path
        return self.client.request(method, path, json=json)


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as test_client:
        yield test_client


def _make_bridge(client: TestClient) -> CtfBridge:
    api = CairnApi("http://testserver", session=_TestClientSession(client))
    return CtfBridge("http://testserver", api=api)


def _complete_project(client: TestClient, project_id: str, description: str) -> None:
    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "description": description, "worker": "test"},
    )
    assert response.status_code == 200, response.text


def _challenge_by_external(client: TestClient, external_id: str) -> dict:
    rows = client.get("/ctf/challenges").json()
    return next(r for r in rows if r["external_id"] == external_id)


def _enable_ctf(client: TestClient, *, max_concurrent: int = 2) -> None:
    client.put("/ctf/mode", json={"mode": "ctf"})
    client.put(
        "/ctf/config",
        json={"adapter": "mock", "base_url": "http://mock.invalid", "max_concurrent": max_concurrent},
    )


# ------------------------------------------------------------------ idle


def test_bridge_idles_in_manual_mode(client: TestClient) -> None:
    bridge = _make_bridge(client)
    stats = bridge.run_once()
    assert stats["synced"] is False
    assert client.get("/ctf/challenges").json() == []


# ------------------------------------------------------------------ sync


def test_sync_inserts_and_dedupes(client: TestClient) -> None:
    _enable_ctf(client)
    bridge = _make_bridge(client)

    stats = bridge.run_once()
    assert stats["synced"] is True
    rows = client.get("/ctf/challenges").json()
    assert len(rows) == 3
    external_ids = {r["external_id"] for r in rows}
    assert external_ids == {"mock-hello", "mock-trap", "mock-flood"}
    hello = next(r for r in rows if r["external_id"] == "mock-hello")
    # Detail is deferred: sync only stores list metadata (P1-1) so dynamic
    # target envs are not provisioned for every challenge at sync time.
    assert hello["description"] == ""
    assert hello["needs_refresh"] is True
    assert hello["points"] == 10

    # a second sync must not duplicate rows
    bridge.run_once()
    assert len(client.get("/ctf/challenges").json()) == 3
    assert client.get("/ctf/config").json()["last_sync_at"] is not None
    assert client.get("/ctf/config").json()["sync_requested"] is False


# --------------------------------------------------------------- projects


def test_bridge_creates_projects_up_to_max_concurrent(client: TestClient) -> None:
    _enable_ctf(client, max_concurrent=2)
    bridge = _make_bridge(client)
    bridge.run_once()

    solving = [r for r in client.get("/ctf/challenges").json() if r["status"] == "solving"]
    queued = [r for r in client.get("/ctf/challenges").json() if r["status"] == "queued"]
    assert len(solving) == 2
    assert len(queued) == 1
    for row in solving:
        assert row["project_id"]
        detail = client.get(f"/projects/{row['project_id']}").json()
        assert detail["project"]["status"] == "active"
        assert row["title"] in detail["project"]["title"]


def test_max_concurrent_caps_simultaneous_projects(client: TestClient) -> None:
    _enable_ctf(client, max_concurrent=1)
    bridge = _make_bridge(client)
    bridge.run_once()
    solving = [r for r in client.get("/ctf/challenges").json() if r["status"] == "solving"]
    assert len(solving) == 1

    # a second round must not start another project while one is running
    bridge.run_once()
    assert len([r for r in client.get("/ctf/challenges").json() if r["status"] == "solving"]) == 1

    # once the running project settles (goes rate_limited), no project is running
    _complete_project(client, solving[0]["project_id"], "The flag is flag{whatever}.")
    bridge.run_once()
    assert len([r for r in client.get("/ctf/challenges").json() if r["status"] == "solving"]) == 0
    # the next round starts the next queued challenge
    bridge.run_once()
    assert len([r for r in client.get("/ctf/challenges").json() if r["status"] == "solving"]) == 1


# ------------------------------------------------------------- completion


def test_completion_submits_correct_flag_and_marks_solved(client: TestClient) -> None:
    _enable_ctf(client, max_concurrent=3)
    bridge = _make_bridge(client)
    bridge.run_once()

    hello = _challenge_by_external(client, "mock-hello")
    assert hello["status"] == "solving"
    _complete_project(client, hello["project_id"], "The flag is flag{hello}.")

    bridge.run_once()
    updated = _challenge_by_external(client, "mock-hello")
    assert updated["status"] == "solved"
    assert updated["last_flag"] == "flag{hello}"


def test_solved_challenge_releases_env(client: TestClient) -> None:
    """A solved challenge's target instance is recovered immediately."""
    _enable_ctf(client, max_concurrent=3)
    bridge = _make_bridge(client)
    bridge.run_once()

    hello = _challenge_by_external(client, "mock-hello")
    assert hello["status"] == "solving"
    _complete_project(client, hello["project_id"], "The flag is flag{hello}.")

    bridge.run_once()
    updated = _challenge_by_external(client, "mock-hello")
    assert updated["status"] == "solved"
    assert "mock-hello" in bridge.source._recovered


def test_terminal_challenge_releases_env_via_cleanup(client: TestClient) -> None:
    """Stopped/paused/failed challenges have their env released by the cleanup pass."""
    _enable_ctf(client, max_concurrent=3)
    bridge = _make_bridge(client)
    bridge.run_once()

    hello = _challenge_by_external(client, "mock-hello")
    # Simulate the user stopping the challenge (lifecycle endpoint sets status).
    client.post(f"/ctf/challenges/{hello['id']}/stop")
    bridge.run_once()

    assert "mock-hello" in bridge.source._recovered


def test_auto_submit_disabled_marks_submitted(client: TestClient) -> None:
    _enable_ctf(client, max_concurrent=3)
    client.put("/ctf/config", json={"auto_submit": False})
    bridge = _make_bridge(client)
    bridge.run_once()

    hello = _challenge_by_external(client, "mock-hello")
    _complete_project(client, hello["project_id"], "The flag is flag{hello}.")

    bridge.run_once()
    updated = _challenge_by_external(client, "mock-hello")
    assert updated["status"] == "submitted"
    assert updated["last_flag"] == "flag{hello}"


def test_wrong_flag_reopens_project(client: TestClient) -> None:
    _enable_ctf(client, max_concurrent=3)
    bridge = _make_bridge(client)
    bridge.run_once()

    trap = _challenge_by_external(client, "mock-trap")
    assert trap["status"] == "solving"
    _complete_project(client, trap["project_id"], "The flag is flag{guess}.")

    bridge.run_once()
    updated = _challenge_by_external(client, "mock-trap")
    assert updated["status"] == "solving"  # reopened for another attempt
    assert updated["attempt_count"] == 1
    detail = client.get(f"/projects/{trap['project_id']}").json()
    assert detail["project"]["status"] == "active"  # reopened
    assert any("错误" in f["description"] for f in detail["facts"])


def test_wrong_flag_exhausts_attempts_to_failed(client: TestClient) -> None:
    _enable_ctf(client, max_concurrent=3)
    bridge = _make_bridge(client)
    bridge.run_once()

    trap = _challenge_by_external(client, "mock-trap")
    # pre-exhaust: two more attempts will hit the max of 5
    client.put(f"/ctf/challenges/{trap['id']}", json={"attempt_count": 4})
    _complete_project(client, trap["project_id"], "The flag is flag{guess}.")

    bridge.run_once()
    updated = _challenge_by_external(client, "mock-trap")
    assert updated["status"] == "failed"
    assert updated["attempt_count"] == 5


def test_rate_limited_then_backoff_and_wrong_flag(client: TestClient) -> None:
    _enable_ctf(client, max_concurrent=3)
    bridge = _make_bridge(client)
    bridge.run_once()

    flood = _challenge_by_external(client, "mock-flood")
    assert flood["status"] == "solving"
    _complete_project(client, flood["project_id"], "The flag is flag{guess}.")

    bridge.run_once()
    updated = _challenge_by_external(client, "mock-flood")
    assert updated["status"] == "rate_limited"
    assert updated["last_flag"] == "flag{guess}"

    # expire the in-memory backoff and retry -> mock now rejects -> reopen
    bridge._rate_limit_retry_at["mock-flood"] = 0
    bridge.run_once()
    updated = _challenge_by_external(client, "mock-flood")
    assert updated["status"] == "solving"
    assert updated["attempt_count"] == 1


def test_completed_project_without_flag_marks_failed(client: TestClient) -> None:
    _enable_ctf(client, max_concurrent=3)
    bridge = _make_bridge(client)
    bridge.run_once()

    hello = _challenge_by_external(client, "mock-hello")
    _complete_project(client, hello["project_id"], "No flag here, just a conclusion.")

    bridge.run_once()
    updated = _challenge_by_external(client, "mock-hello")
    assert updated["status"] == "failed"


def test_stopped_project_is_requeued(client: TestClient) -> None:
    _enable_ctf(client, max_concurrent=2)
    bridge = _make_bridge(client)
    bridge.run_once()

    trap = _challenge_by_external(client, "mock-trap")
    client.put(f"/projects/{trap['project_id']}/status", json={"status": "stopped"})

    bridge.run_once()
    updated = _challenge_by_external(client, "mock-trap")
    assert updated["status"] == "queued"
    assert updated["project_id"] is None


def test_deleted_project_is_requeued(client: TestClient) -> None:
    _enable_ctf(client, max_concurrent=3)
    bridge = _make_bridge(client)
    bridge.run_once()

    trap = _challenge_by_external(client, "mock-trap")
    assert client.delete(f"/projects/{trap['project_id']}").status_code == 204

    bridge.run_once()
    updated = _challenge_by_external(client, "mock-trap")
    assert updated["status"] == "queued"
    assert updated["project_id"] is None


# ------------------------------------------------------- manual sync request


def test_manual_sync_request_is_handled(client: TestClient) -> None:
    _enable_ctf(client)
    bridge = _make_bridge(client)

    client.post("/ctf/sync")
    assert client.get("/ctf/config").json()["sync_requested"] is True

    bridge.run_once()
    assert len(client.get("/ctf/challenges").json()) == 3
    assert client.get("/ctf/config").json()["sync_requested"] is False
