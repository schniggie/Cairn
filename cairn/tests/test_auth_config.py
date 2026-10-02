from __future__ import annotations

import pytest
from pydantic import ValidationError

from cairn.dispatcher.config import AuthConfig, AuthTargetConfig, DispatchConfig

from conftest import make_config


def _auth_payload() -> dict:
    return {
        "store_root": "/opt/cairn/auth",
        "worker_mount_root": "/run/cairn-auth",
        "login_timeout": 900,
        "verify_timeout": 30,
        "targets": [
            {
                "name": "target-user",
                "base_url": "https://target.example.com",
                "login_url": "https://target.example.com/login",
                "role": "user",
                "indexed_db": True,
                "verify": {
                    "url": "https://target.example.com/dashboard",
                    "expect_status": 200,
                    "selector": "[data-testid='user-menu']",
                    "http_url": "https://target.example.com/api/me",
                    "http_expect_status": 200,
                },
            }
        ],
    }


def test_auth_config_parses_full_target() -> None:
    config = AuthConfig.model_validate(_auth_payload())
    assert config.store_root == "/opt/cairn/auth"
    assert config.worker_mount_root == "/run/cairn-auth"
    assert config.login_timeout == 900
    assert config.verify_timeout == 30
    assert len(config.targets) == 1
    target = config.targets[0]
    assert target.name == "target-user"
    assert target.role == "user"
    assert target.indexed_db is True
    assert target.verify.selector == "[data-testid='user-menu']"


def test_auth_config_empty_targets_allowed() -> None:
    payload = _auth_payload()
    payload["targets"] = []
    config = AuthConfig.model_validate(payload)
    assert config.targets == []


def test_auth_config_rejects_duplicate_targets() -> None:
    payload = make_config().model_dump()
    payload["auth"] = _auth_payload()
    payload["auth"]["targets"].append(dict(payload["auth"]["targets"][0]))
    with pytest.raises(ValidationError, match="auth target names must be unique"):
        DispatchConfig.model_validate(payload)


def test_auth_config_rejects_non_positive_timeouts() -> None:
    payload = _auth_payload()
    payload["login_timeout"] = 0
    with pytest.raises(ValidationError):
        AuthConfig.model_validate(payload)

    payload = _auth_payload()
    payload["verify_timeout"] = -1
    with pytest.raises(ValidationError):
        AuthConfig.model_validate(payload)


def test_auth_config_defaults() -> None:
    target = AuthTargetConfig.model_validate(
        {
            "name": "t",
            "base_url": "https://x.example.com",
            "login_url": "https://x.example.com/login",
            "role": "user",
            "verify": {"url": "https://x.example.com/dashboard"},
        }
    )
    assert target.indexed_db is True
    assert target.verify.expect_status == 200
    assert target.verify.selector is None
    assert target.verify.http_url is None
    assert target.verify.http_expect_status == 200


def test_dispatch_config_without_auth_is_valid() -> None:
    config = DispatchConfig.model_validate(make_config().model_dump())
    assert config.auth is None


def test_auth_target_lookup_by_name() -> None:
    config = AuthConfig.model_validate(_auth_payload())
    assert config.target("target-user").name == "target-user"
    with pytest.raises(KeyError):
        config.target("missing")
