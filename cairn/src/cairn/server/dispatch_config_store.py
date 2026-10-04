from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from cairn.dispatcher.config import DispatchConfig, validate_prompt_resources

MASK = "********"
SENSITIVE_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH")
_config_path = Path.cwd() / "dispatch.yaml"


def configure_dispatch_config(path: Path) -> None:
    global _config_path
    _config_path = path.resolve()


def config_path() -> Path:
    return _config_path


def load_redacted_document() -> tuple[str, bool, str | None]:
    path = config_path()
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    redacted, changed = _redact(data)
    rendered = yaml.safe_dump(redacted, sort_keys=False, allow_unicode=True)
    updated = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    return rendered, changed, updated


def validate_and_save(text: str) -> tuple[DispatchConfig, bool]:
    path = config_path()
    incoming = yaml.safe_load(text) or {}
    if not isinstance(incoming, dict):
        raise ValueError("dispatch config must be a YAML object")
    current = yaml.safe_load(path.read_text(encoding="utf-8")) or {} if path.exists() else {}
    if not isinstance(current, dict):
        current = {}
    _reject_credential_redirect(incoming, current)
    merged = _restore_masks(incoming, current)
    config = DispatchConfig.model_validate(merged)
    validate_prompt_resources(config.runtime.prompt_group)
    restart_required = _immutable_projection(current) != _immutable_projection(merged)
    rendered = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(path)
    return config, restart_required


def _is_sensitive(key: str) -> bool:
    upper = key.upper()
    return any(part in upper for part in SENSITIVE_PARTS)


def _redact(value: Any, key: str = "") -> tuple[Any, bool]:
    if key and _is_sensitive(key) and value not in (None, ""):
        return MASK, True
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        changed = False
        for child_key, child_value in value.items():
            result_value, result_changed = _redact(child_value, str(child_key))
            result[child_key] = result_value
            changed = changed or result_changed
        return result, changed
    if isinstance(value, list):
        result = []
        changed = False
        for item in value:
            result_value, result_changed = _redact(item)
            result.append(result_value)
            changed = changed or result_changed
        return result, changed
    return value, False


def _is_endpoint_key(key: str) -> bool:
    upper = key.upper()
    return any(part in upper for part in ("URL", "HOST", "ENDPOINT"))


def _endpoint_identity(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if "://" not in text:
        return text.rstrip("/")
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme.lower()}://{host}{port}{parsed.path.rstrip('/')}"


def _preserved_secrets(incoming: dict, current: dict) -> list[str]:
    preserved: list[str] = []
    for key, value in incoming.items():
        if value != MASK or not _is_sensitive(str(key)):
            continue
        old = current.get(key)
        if old not in (None, "", MASK):
            preserved.append(str(key))
    return preserved


def _changed_endpoints(incoming: dict, current: dict) -> list[str]:
    changed: list[str] = []
    for key, value in incoming.items():
        if not _is_endpoint_key(str(key)):
            continue
        if _endpoint_identity(value) != _endpoint_identity(current.get(key)):
            changed.append(str(key))
    return changed


def _check_mapping(incoming: Any, current: Any, label: str) -> None:
    if not isinstance(incoming, dict) or not isinstance(current, dict):
        return
    changed = _changed_endpoints(incoming, current)
    preserved = _preserved_secrets(incoming, current)
    if changed and preserved:
        raise ValueError(
            f"re-supply {', '.join(preserved)} when changing {label} endpoint ({', '.join(changed)})"
        )


def _workers_by_name(data: Any) -> dict[str, dict]:
    if not isinstance(data, dict) or not isinstance(data.get("workers"), list):
        return {}
    named: dict[str, dict] = {}
    for worker in data["workers"]:
        if isinstance(worker, dict) and isinstance(worker.get("name"), str):
            named[worker["name"]] = worker
    return named


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _effective_env(root: dict, worker: dict) -> dict:
    return {**_mapping(root.get("common_env")), **_mapping(worker.get("env"))}


def _reject_credential_redirect(incoming: dict, current: dict) -> None:
    """A masked secret must not survive a change of the endpoint that receives it."""
    _check_mapping(incoming.get("common_env"), current.get("common_env"), "common_env")
    _check_mapping(incoming.get("safety"), current.get("safety"), "safety")
    current_workers = _workers_by_name(current)
    for name, worker in _workers_by_name(incoming).items():
        env = _mapping(worker.get("env"))
        previous = current_workers.get(name)
        if previous is None:
            if any(value == MASK and _is_sensitive(str(key)) for key, value in env.items()):
                raise ValueError(f"new worker {name} cannot reuse a masked credential")
            continue
        _check_mapping(env, _mapping(previous.get("env")), f"worker {name}")
        old_env = _effective_env(current, previous)
        new_env = _effective_env(incoming, worker)
        changed = _changed_endpoints(new_env, old_env)
        preserved = _preserved_secrets(new_env, old_env)
        if changed and preserved:
            raise ValueError(
                f"re-supply {', '.join(preserved)} when changing worker {name} endpoint ({', '.join(changed)})"
            )


def _restore_worker_list(incoming: list, current: list) -> list:
    by_name: dict[str, dict] = {}
    for worker in current:
        if isinstance(worker, dict) and isinstance(worker.get("name"), str):
            by_name[worker["name"]] = worker
    restored = []
    for item in incoming:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"] in by_name:
            restored.append(_restore_masks(item, by_name[item["name"]], "worker"))
        else:
            restored.append(_restore_masks(item, {}, "worker"))
    return restored


def _restore_masks(incoming: Any, current: Any, key: str = "") -> Any:
    if incoming == MASK and key and _is_sensitive(key):
        return current
    if isinstance(incoming, dict):
        current_dict = current if isinstance(current, dict) else {}
        return {
            child_key: _restore_masks(child_value, current_dict.get(child_key), str(child_key))
            for child_key, child_value in incoming.items()
        }
    if isinstance(incoming, list):
        if key == "workers":
            current_list = current if isinstance(current, list) else []
            return _restore_worker_list(incoming, current_list)
        current_list = current if isinstance(current, list) else []
        return [
            _restore_masks(item, current_list[index] if index < len(current_list) else None)
            for index, item in enumerate(incoming)
        ]
    return incoming


def _immutable_projection(data: Any) -> tuple[Any, ...]:
    if not isinstance(data, dict):
        return (None,)
    runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
    return (
        data.get("server"),
        runtime.get("execution"),
        data.get("container"),
        data.get("local"),
    )
