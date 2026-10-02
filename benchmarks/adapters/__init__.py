from __future__ import annotations

from .base import BenchmarkAdapter, ChallengeSpec, register, get_adapter, list_adapters
from .cybench import CybenchAdapter
from .intercode_ctf import InterCodeCTFAdapter
from .autopenbench import AutoPenBenchAdapter
