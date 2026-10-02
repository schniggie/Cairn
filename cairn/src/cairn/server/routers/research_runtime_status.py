"""Expose readiness without leaking provider configuration or implying execution.

``available`` reflects a live research worker that is actually present: a worker that
has refreshed its runtime marker recently enough to claim a session, plus a usable
Claude Code CLI on this host. Hard-coded ``available=true`` is never returned; the UI
only shows "研究已排队" when a real worker is accepting work.
"""
import shutil
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from cairn.server import db
from cairn.server.research_services import read_worker_runtime

router = APIRouter(prefix="/api/research", tags=["research"])

# A worker that refreshed its marker within this window is considered available.
AVAILABLE_FRESHNESS_SECONDS = 90


@router.get("/runtime")
def runtime_status():
    installed = shutil.which("claude") is not None
    worker = None
    try:
        with db.get_conn() as conn:
            worker = read_worker_runtime(conn)
    except Exception:
        worker = None

    available = False
    state = None
    current_session = None
    freshness = None
    if installed and worker is not None:
        try:
            last = datetime.fromisoformat(worker["last_heartbeat_at"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            freshness = int((datetime.now(timezone.utc) - last).total_seconds())
            available = freshness <= AVAILABLE_FRESHNESS_SECONDS
            state = worker.get("state")
            current_session = worker.get("current_session_id")
        except Exception:
            available = False

    if available:
        message = "研究执行器已就绪，可领取并运行新的研究。"
    elif installed:
        message = "会话语已持久保存；研究执行器尚未启动或最近未心跳。"
    else:
        message = "未找到 Claude Code；研究执行器无法运行。"
    return {
        "available": available,
        "cli_installed": installed,
        "worker": {
            "state": state,
            "current_session_id": current_session,
            "last_heartbeat_seconds_ago": freshness,
        } if worker is not None else None,
        "configuration_source": "Claude Code user settings" if installed else None,
        "message": message,
    }