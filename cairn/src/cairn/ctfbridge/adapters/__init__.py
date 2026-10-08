"""Platform adapter registry.

Adapters are looked up by name (the ``adapter`` field of ``ctf_config``).
"""

from __future__ import annotations

from typing import Callable

from cairn.ctfbridge.adapters.base import ChallengeSource
from cairn.ctfbridge.adapters.ctfd import CtfdSource
from cairn.ctfbridge.adapters.dasctf import DasctfSource
from cairn.ctfbridge.adapters.mock import MockSource

# Each factory receives the full bridge config as keyword arguments and must
# tolerate unknown keys (``**kwargs``).
_FACTORIES: dict[str, Callable[..., ChallengeSource]] = {
    "ctfd": CtfdSource,
    "dasctf": DasctfSource,
    "mock": MockSource,
}


def get_adapter(name: str, **cfg) -> ChallengeSource:
    """Build an adapter instance for ``name``.

    Raises ``ValueError`` for an unknown adapter name.
    """
    try:
        factory = _FACTORIES[name]
    except KeyError:
        raise ValueError(f"unknown CTF adapter: {name!r}") from None
    return factory(**cfg)


def available_adapters() -> list[str]:
    return sorted(_FACTORIES)


__all__ = ["ChallengeSource", "get_adapter", "available_adapters"]
