from __future__ import annotations

from cairn.dispatcher.workers.adapters import (
    ClaudeCodeDriver,
    CodexDriver,
    GeminiDriver,
    MockDriver,
    OpenCodeDriver,
    PiDriver,
)
from cairn.dispatcher.workers.base import WorkerDriver


_CLAUDE = ClaudeCodeDriver()
_GEMINI = GeminiDriver()
_MOCK = MockDriver()
_OPENCODE = OpenCodeDriver()

DRIVERS: dict[str, WorkerDriver] = {
    "claudecode": _CLAUDE,
    "codex": CodexDriver(),
    "gemini": _GEMINI,
    "opencode": _OPENCODE,
    "pi": PiDriver(),
    "mock": _MOCK,
}

# Local variants invoke the host CLIs in their native configuration (no cairn provider
# injection). claudecode, gemini, opencode, and mock build identical commands in both modes.
LOCAL_DRIVERS: dict[str, WorkerDriver] = {
    "claudecode": _CLAUDE,
    "codex": CodexDriver(local=True),
    "gemini": _GEMINI,
    "opencode": _OPENCODE,
    "pi": PiDriver(local=True),
    "mock": _MOCK,
}


def get_driver(name: str, execution: str = "container") -> WorkerDriver:
    drivers = LOCAL_DRIVERS if execution == "local" else DRIVERS
    return drivers[name]


def execution_mode_for(backend: object, fallback: str) -> str:
    """Pick container vs local drivers from the backend object a task was given."""
    if type(backend).__name__ == "LocalBackend":
        return "local"
    if type(backend).__name__ == "ContainerManager":
        return "container"
    return fallback
