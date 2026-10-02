"""CTF platform bridge.

A standalone process that drives Cairn projects through the Cairn HTTP API to
pull challenges from a CTF platform, solve them, and submit flags.
"""

from cairn.ctfbridge.bridge import CtfBridge

__all__ = ["CtfBridge"]
