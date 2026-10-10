from __future__ import annotations

import os
from pathlib import Path

DEFAULT_RUN_LOG_DIR = Path.home() / ".local" / "share" / "cairn" / "runs"


def run_log_root() -> Path:
    configured = os.environ.get("CAIRN_RUN_LOG_DIR")
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_RUN_LOG_DIR
