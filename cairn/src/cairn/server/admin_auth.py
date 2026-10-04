"""Admin bearer checks for routes that expose secrets or perform privileged side effects.

Plain reads of ``/projects``, ``/skills``, and ``/engines`` stay open when
``CAIRN_ADMIN_TOKEN`` is unset so local development can list data. Routes that
send stored credentials, change where those credentials go, or write files
outside a normal project create refuse every request until a token is
configured and presented. Same-origin checks are not authentication.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request

_FAIL_CLOSED_PREFIXES = (
    "/api/research",
    "/research",
    "/dispatch-config",
)

# Credentialed CTF calls and config changes. Challenge reads and the bridge
# heartbeat stay off this list so a local bridge can run, but they still
# require the bearer once CAIRN_ADMIN_TOKEN is set.
_CTF_FAIL_CLOSED = frozenset(
    {
        ("PUT", "/ctf/config"),
        ("PUT", "/ctf/mode"),
        ("POST", "/ctf/test"),
        ("POST", "/ctf/test-model"),
        ("POST", "/ctf/submit"),
    }
)


def fails_closed(path: str, method: str = "GET") -> bool:
    if any(path == prefix or path.startswith(prefix + "/") for prefix in _FAIL_CLOSED_PREFIXES):
        return True
    return (method.upper(), path) in _CTF_FAIL_CLOSED


def configured_admin_token() -> str:
    from cairn.server import app as server_app

    return server_app.ADMIN_TOKEN.strip()


def admin_token_matches(authorization_header: str) -> bool:
    expected = configured_admin_token()
    presented = authorization_header.removeprefix("Bearer ").strip()
    if not expected or len(presented) != len(expected):
        return False
    return secrets.compare_digest(presented, expected)


def require_admin(request: Request) -> None:
    if not admin_token_matches(request.headers.get("Authorization", "")):
        raise HTTPException(status_code=403, detail="Forbidden")
