"""Bridge configuration, read from the Cairn server's ``/ctf/config``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from cairn.ctfbridge.client import CairnApi


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return str(value).lower() in ("1", "true", "yes")


def _parse_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class BridgeConfig:
    mode: str = "manual"
    adapter: str = "ctfd"
    base_url: str = ""
    token: str = ""
    team_name: str = ""
    flag_regex: str = "flag\\{[^}]+\\}"
    auto_submit: bool = True
    max_concurrent: int = 2
    poll_interval: int = 10
    env_poll_interval: int = 5
    env_timeout: int = 180
    submission_max_retries: int = 5
    rate_limit_backoff: int = 30
    model_base_url: str = ""
    model_name: str = ""
    model_api_key: str = ""
    budget_easy: int = 12
    budget_medium: int = 25
    budget_hard: int = 40
    last_sync_at: str | None = None
    sync_requested: bool = False
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_server(cls, data: dict) -> "BridgeConfig":
        return cls(
            mode=str(data.get("mode") or "manual"),
            adapter=str(data.get("adapter") or "ctfd"),
            base_url=str(data.get("base_url") or ""),
            token=str(data.get("token") or ""),
            team_name=str(data.get("team_name") or ""),
            flag_regex=str(data.get("flag_regex") or "flag\\{[^}]+\\}"),
            auto_submit=_parse_bool(data.get("auto_submit"), True),
            max_concurrent=_parse_int(data.get("max_concurrent"), 2),
            poll_interval=_parse_int(data.get("poll_interval"), 10),
            env_poll_interval=_parse_int(data.get("env_poll_interval"), 5),
            env_timeout=_parse_int(data.get("env_timeout"), 180),
            submission_max_retries=_parse_int(data.get("submission_max_retries"), 5),
            rate_limit_backoff=_parse_int(data.get("rate_limit_backoff"), 30),
            model_base_url=str(data.get("model_base_url") or ""),
            model_name=str(data.get("model_name") or ""),
            model_api_key=str(data.get("model_api_key") or ""),
            budget_easy=_parse_int(data.get("budget_easy"), 12),
            budget_medium=_parse_int(data.get("budget_medium"), 25),
            budget_hard=_parse_int(data.get("budget_hard"), 40),
            last_sync_at=data.get("last_sync_at"),
            sync_requested=_parse_bool(data.get("sync_requested"), False),
            extra=data,
        )

    def is_active(self) -> bool:
        return self.mode == "ctf" and bool(self.base_url)


def load_bridge_config(api: CairnApi) -> BridgeConfig:
    """Fetch and parse the current bridge configuration from the server."""
    return BridgeConfig.from_server(api.get_config(full=True))
