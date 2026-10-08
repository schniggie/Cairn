"""Benchmark adapter contract for Cairn.

Every benchmark (Cybench, NYU CTF, AutoPenBench, ...) is wrapped as an adapter
implementing this contract. New benchmark == new adapter file; the runner never
changes.

An adapter tells the runner:
  - What challenges exist (list_challenges)
  - How to set up each challenge (setup)
  - How to tear it down (teardown)
  - How to verify the result (verify)
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ChallengeSpec:
    challenge_id: str
    name: str
    category: str
    difficulty: int
    origin: str
    goal: str
    flag: str
    files_dir: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class BenchmarkAdapter(abc.ABC):
    name: str

    @abc.abstractmethod
    def list_challenges(self, **filters) -> list[dict]:
        """Return a list of challenge metadata dicts.

        Each dict must have at least: challenge_id, name, category, difficulty.
        Additional fields are benchmark-specific.
        """

    @abc.abstractmethod
    def setup(self, challenge: dict) -> ChallengeSpec:
        """Prepare the challenge environment and return a ChallengeSpec.

        This should:
          - Start Docker containers if needed
          - Run init scripts to prepare files
          - Build the origin prompt
          - Return a ChallengeSpec with all info the runner needs
        """

    @abc.abstractmethod
    def teardown(self, challenge: dict) -> None:
        """Clean up the challenge environment (stop Docker, remove temp files)."""

    def verify(self, challenge: dict, facts: list[dict]) -> bool:
        """Check if the flag appears in any fact description.

        Default: exact substring match. Override for custom verification.
        """
        flag = challenge.get("flag", "")
        if not flag:
            return False
        return any(flag in f.get("description", "") for f in facts)

    def filter_challenges(self, challenges: list[dict], **filters) -> list[dict]:
        """Apply common filters (category, difficulty, name substring)."""
        result = challenges
        if filters.get("category"):
            result = [c for c in result if c.get("category", "").lower() == filters["category"].lower()]
        if filters.get("difficulty") is not None:
            result = [c for c in result if c.get("difficulty") == filters["difficulty"]]
        if filters.get("challenge"):
            q = filters["challenge"].lower()
            result = [c for c in result if q in c.get("name", "").lower()]
        return result


_REGISTRY: dict[str, BenchmarkAdapter] = {}


def register(adapter: BenchmarkAdapter) -> BenchmarkAdapter:
    _REGISTRY[adapter.name] = adapter
    return adapter


def get_adapter(name: str) -> BenchmarkAdapter:
    if name not in _REGISTRY:
        raise KeyError(f"No benchmark adapter '{name}'. Registered: {list(_REGISTRY)}")
    return _REGISTRY[name]


def list_adapters() -> list[str]:
    return sorted(_REGISTRY)
