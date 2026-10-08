from __future__ import annotations

import logging
import platform

from cairn.auth_helper.client import AuthHelperRequest

LOG = logging.getLogger(__name__)


class DesktopNotifier:
    """Sends a desktop notification when the operator must log in.

    Notifications are best-effort: if no notification backend is available the helper
    falls back to logging and continues (the headed browser is the primary signal).
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def notify_auth_required(self, request: AuthHelperRequest) -> None:
        message = self._format(request)
        LOG.info("auth notification: %s", message.replace("\n", " | "))
        if not self.enabled:
            return
        self._emit(message)

    @staticmethod
    def _format(request: AuthHelperRequest) -> str:
        lines = [
            "Cairn requires authentication",
            "",
            f"Project: {request.project_id}",
            f"Target: {request.auth_ref}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _emit(message: str) -> None:
        system = platform.system()
        if system == "Windows":
            DesktopNotifier._emit_windows(message)
        elif system == "Darwin":
            DesktopNotifier._emit_macos(message)
        else:
            DesktopNotifier._emit_linux(message)

    @staticmethod
    def _emit_windows(message: str) -> None:
        # Windows toast via PowerShell is simplest without extra deps; degrade gracefully.
        try:
            import subprocess

            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        "$n = New-Object -ComObject WScript.Shell; "
                        "$n.Popup($args[0], 10, 'Cairn Auth', 64) | Out-Null"
                    ),
                    message,
                ],
                check=False,
                timeout=30,
            )
        except Exception as exc:  # pragma: no cover - best effort
            LOG.debug("windows notification failed: %s", exc)

    @staticmethod
    def _emit_macos(message: str) -> None:
        try:
            import subprocess

            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "Cairn Auth"'],
                check=False,
                timeout=30,
            )
        except Exception as exc:  # pragma: no cover - best effort
            LOG.debug("macos notification failed: %s", exc)

    @staticmethod
    def _emit_linux(message: str) -> None:
        try:
            import subprocess

            subprocess.run(
                ["notify-send", "Cairn Auth", message],
                check=False,
                timeout=30,
            )
        except Exception as exc:  # pragma: no cover - best effort
            LOG.debug("linux notification failed: %s", exc)
