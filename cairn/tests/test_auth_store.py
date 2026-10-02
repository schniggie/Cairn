from __future__ import annotations

from pathlib import Path

import pytest

from cairn.auth.models import AuthMeta
from cairn.auth.store import AuthStore, PathTraversalError
from hashlib import sha256
import json


def test_store_project_and_profile_paths(tmp_path: Path) -> None:
    store = AuthStore(tmp_path)
    assert store.project_dir("proj_001") == tmp_path / "proj_001"
    assert store.profile_dir("proj_001", "target-user") == tmp_path / "proj_001" / "target-user"
    assert store.state_file("proj_001", "target-user") == tmp_path / "proj_001" / "target-user" / "state.json"
    assert store.meta_file("proj_001", "target-user") == tmp_path / "proj_001" / "target-user" / "meta.json"


def test_store_write_and_load_state_roundtrip(tmp_path: Path) -> None:
    store = AuthStore(tmp_path)
    state = {"cookies": [{"name": "session", "value": "abc"}], "origins": []}
    path = store.write_state("proj_001", "target-user", state)
    assert path.is_file()
    assert store.load_state("proj_001", "target-user") == state
    assert store.profile_exists("proj_001", "target-user")


def test_store_meta_roundtrip(tmp_path: Path) -> None:
    store = AuthStore(tmp_path)
    meta = AuthMeta(
        target="target-user",
        role="user",
        base_url="https://target.example.com",
        created_at="2026-01-01T00:00:00Z",
        verified_at="2026-01-01T00:01:00Z",
        verification={"page": True, "selector": True, "api": True},
    )
    store.write_meta("proj_001", "target-user", meta)
    loaded = store.load_meta("proj_001", "target-user")
    assert loaded.target == "target-user"
    assert loaded.role == "user"
    assert loaded.verification == {"page": True, "selector": True, "api": True}


def test_store_rejects_path_traversal(tmp_path: Path) -> None:
    store = AuthStore(tmp_path)
    with pytest.raises(PathTraversalError):
        store.project_dir("../escape")
    with pytest.raises(PathTraversalError):
        store.profile_dir("proj_001", "../../etc")
    with pytest.raises(PathTraversalError):
        store.profile_dir("proj_001", "/absolute")
    with pytest.raises(PathTraversalError):
        store.profile_dir("proj_001", "..\\..\\windows")
    with pytest.raises(PathTraversalError):
        store.project_dir("")


def test_store_list_profiles(tmp_path: Path) -> None:
    store = AuthStore(tmp_path)
    store.write_state("proj_001", "user-a", {"cookies": []})
    store.write_state("proj_001", "admin", {"cookies": []})
    # A directory without a state.json must not be listed.
    (store.project_dir("proj_001") / "incomplete").mkdir(parents=True)
    assert store.list_profiles("proj_001") == ["admin", "user-a"]
    assert store.list_profiles("proj_002") == []


def test_store_remove_profile(tmp_path: Path) -> None:
    store = AuthStore(tmp_path)
    store.write_state("proj_001", "target-user", {"cookies": []})
    assert store.profile_exists("proj_001", "target-user")
    assert store.remove_profile("proj_001", "target-user") is True
    assert not store.profile_exists("proj_001", "target-user")
    assert store.remove_profile("proj_001", "target-user") is False


def test_store_capture_writes_dispatcher_manifest_bound_to_state(tmp_path: Path) -> None:
    store = AuthStore(tmp_path)
    manifest = store.write_capture(
        "proj_001",
        "target-user",
        {"cookies": [{"name": "sid", "value": "opaque"}]},
        request_id="auth_001",
        actor_id="helper-a",
    )

    state_bytes = store.state_file("proj_001", "target-user").read_bytes()
    assert manifest.request_id == "auth_001"
    assert manifest.auth_ref == "target-user"
    assert manifest.actor_id == "helper-a"
    assert manifest.capture_generation == 1
    assert manifest.state_sha256 == sha256(state_bytes).hexdigest()
    assert store.load_manifest("proj_001", "target-user") == manifest
    assert store.validate_capture(
        "proj_001",
        "target-user",
        request_id="auth_001",
        actor_id="helper-a",
        capture_generation=1,
    ) == manifest


def test_store_capture_rejects_state_tampering_and_binding_mismatch(tmp_path: Path) -> None:
    store = AuthStore(tmp_path)
    store.write_capture(
        "proj_001",
        "target-user",
        {"cookies": []},
        request_id="auth_001",
        actor_id="helper-a",
    )
    state = store.state_file("proj_001", "target-user")
    state.write_text(json.dumps({"cookies": [{"name": "sid", "value": "tampered"}]}), encoding="utf-8")

    with pytest.raises(ValueError, match="capture mismatch"):
        store.validate_capture(
            "proj_001",
            "target-user",
            request_id="auth_001",
            actor_id="helper-a",
            capture_generation=1,
        )
    with pytest.raises(ValueError, match="capture mismatch"):
        store.validate_capture(
            "proj_001",
            "target-user",
            request_id="auth-else",
            actor_id="helper-a",
            capture_generation=1,
        )
