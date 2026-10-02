from __future__ import annotations

import pytest

from cairn.auth.graph import AuthGraphAdapter, FACT_INVALID, FACT_VERIFIED
from cairn.dispatcher.config import AuthTargetConfig
from cairn.dispatcher.protocol.client import ApiResult


class _RecordingClient:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.concluded: list[tuple[str, str, str]] = []

    def create_intent(self, project_id, from_ids, description, creator, worker=None):
        self.created.append(
            {
                "project_id": project_id,
                "from": from_ids,
                "description": description,
                "creator": creator,
                "worker": worker,
            }
        )
        return ApiResult(201, {"id": "i001"})

    def conclude(self, project_id, intent_id, worker, description):
        self.concluded.append((project_id, intent_id, description))
        return ApiResult(200, {"fact": {"id": "f001"}})


def _target() -> AuthTargetConfig:
    return AuthTargetConfig.model_validate(
        {
            "name": "target-user",
            "base_url": "https://target.example.com",
            "login_url": "https://target.example.com/login",
            "role": "user",
            "verify": {"url": "https://target.example.com/dashboard"},
        }
    )


def test_verified_fact_flow_creates_then_concludes_intent() -> None:
    client = _RecordingClient()
    adapter = AuthGraphAdapter(client)

    intent_id = adapter.verified(
        "proj_001",
        _target(),
        methods=["protected_page", "selector", "api"],
    )

    assert intent_id == "i001"
    assert len(client.created) == 1
    created = client.created[0]
    # Creator == worker == operator.auth.
    assert created["creator"] == "operator.auth"
    assert created["worker"] == "operator.auth"
    assert created["from"] == ["origin"]
    assert created["description"] == "Verify authenticated session"

    assert len(client.concluded) == 1
    concluded = client.concluded[0][2]
    assert FACT_VERIFIED in concluded
    assert "target=target-user" in concluded
    assert "role=user" in concluded
    assert "scope=https://target.example.com" in concluded
    assert "verification=protected_page+selector+api" in concluded


def test_invalid_fact_flow_records_evidence_without_secrets() -> None:
    client = _RecordingClient()
    adapter = AuthGraphAdapter(client)

    intent_id = adapter.invalid(
        "proj_001",
        _target(),
        evidence="protected endpoint redirected to login",
    )

    assert intent_id == "i001"
    concluded = client.concluded[0][2]
    assert FACT_INVALID in concluded
    assert "target=target-user" in concluded
    assert "role=user" in concluded
    assert "evidence=authentication check failed" in concluded


def test_invalid_fact_sanitizes_secret_bearing_verifier_error() -> None:
    client = _RecordingClient()
    adapter = AuthGraphAdapter(client)
    secret = "super-secret-token"

    adapter.invalid(
        "proj_001",
        _target(),
        evidence=f"page navigation failed: https://target.example.com/login?token={secret}",
    )

    concluded = client.concluded[0][2]
    assert secret not in concluded
    assert "evidence=authentication check failed" in concluded


def test_verified_fact_never_contains_secret_markers() -> None:
    client = _RecordingClient()
    adapter = AuthGraphAdapter(client)
    adapter.verified("proj_001", _target(), methods=["api"])
    description = client.concluded[0][2]
    for marker in ("cookie=", "token=", "password=", "Authorization=", "Bearer "):
        assert marker not in description
