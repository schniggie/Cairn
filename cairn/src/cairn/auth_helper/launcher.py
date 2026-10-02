from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from cairn.server.models import AuthRequest

LOG = logging.getLogger(__name__)


class AuthLoginLauncher:
    """Launches ``cairn auth login`` in a separate process for a claimed auth request.

    The headed Chromium opened by that command is the primary "popup". Running it in a
    subprocess keeps the helper's poll loop responsive and isolates any crash in the
    login flow from the daemon.
    """

    def __init__(self, *, config_path: Path, project_id: str, target: str):
        self.config_path = config_path
        self.project_id = project_id
        self.target = target

    def launch(self, request: AuthRequest) -> subprocess.Popen:
        argv = [
            sys.executable,
            "-m",
            "cairn.cli",
            "auth",
            "login",
            "--config",
            str(self.config_path),
            "--project",
            self.project_id,
            "--target",
            self.target,
            "--request",
            request.id,
        ]
        LOG.info("launching auth login request=%s target=%s", request.id, self.target)
        return subprocess.Popen(argv)
