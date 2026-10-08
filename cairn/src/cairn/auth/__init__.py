"""Cairn real-environment login / authentication capability.

This package implements the auth plane described in the development plan:

- ``models``   -- auth-related data models (verification result, capture result, meta).
- ``store``    -- on-disk layout for project-scoped auth profiles (state.json / meta.json).
- ``manager``  -- headed-browser interactive login (human-in-the-loop) producing a
                  Playwright ``storage_state``.
- ``verifier`` -- independent re-validation of a saved storage state (page / selector / api).
- ``graph``    -- maps external auth results onto the Cairn Intent -> Fact protocol.
- ``sanitizer``-- detects and redacts sensitive auth material before it reaches the graph.

The core principle is a strict separation between the **secret plane** (passwords,
cookies, JWTs, tokens, MFA secrets, storage state) and the **reasoning plane**
(AuthSessionVerified / AuthSessionInvalid facts). Secrets never enter Facts, Intents,
Hints or Agent prompts.
"""

from cairn.auth.models import (
    AuthCaptureResult,
    AuthCaptureManifest,
    AuthVerificationResult,
    AuthMeta,
)

__all__ = [
    "AuthCaptureResult",
    "AuthCaptureManifest",
    "AuthVerificationResult",
    "AuthMeta",
]
