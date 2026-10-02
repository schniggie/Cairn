"""Platform-agnostic challenge source interface.

A ``ChallengeSource`` is the only piece of the bridge that knows how to talk to
a real (or mock) CTF platform. New platforms are added by subclassing
:class:`ChallengeSource` and registering it in :mod:`cairn.ctfbridge.adapters`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


@dataclass
class Challenge:
    """A single challenge as seen by the bridge (platform-side shape)."""

    external_id: str
    title: str
    category: str = ""
    points: int = 0
    description: str = ""
    target: str = ""  # per-team instance host:port (dynamic targets only)
    attachments: list[str] = field(default_factory=list)  # download URLs
    hints: list[str] = field(default_factory=list)


class SubmissionResult(str, Enum):
    SUCCESS = "success"
    WRONG_FLAG = "wrong_flag"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


class ChallengeSource(ABC):
    """Abstract interface every platform adapter must implement."""

    name = "base"

    @abstractmethod
    def verify_connection(self) -> bool:
        """Probe connectivity/auth.

        Return ``True`` on success; raise ``RuntimeError`` (or similar) with a
        human-readable message on failure.
        """

    @abstractmethod
    def list_challenges(self) -> list[Challenge]:
        """List visible challenges (id/name/category/points)."""

    @abstractmethod
    def get_challenge(self, external_id: str) -> Challenge:
        """Fetch full challenge detail, including the per-team instance.

        For dynamic-target platforms this must return the *team's* host/port,
        otherwise network challenges would target the wrong address.
        """

    @abstractmethod
    def submit_flag(self, external_id: str, flag: str) -> tuple[SubmissionResult, str]:
        """Submit a flag.

        Returns a ``(SubmissionResult, message)`` pair. The message is safe to
        log and must never embed the submitted flag itself.
        """

    def recover_env(self, external_id: str) -> None:
        """Release the per-team target instance, if any.

        Default no-op. Platforms with per-team environments should override so
        that solved/stopped challenges don't hold instances open and consume
        platform quota (e.g. DasCTF's 3-instance limit).
        """

    def overview(self) -> dict | None:
        """Return platform score/rank (e.g. ``{"platform_score": 520}``).

        Default returns ``None`` (no score API). Never raises — the caller
        treats a failed probe as "no score available".
        """
        return None
