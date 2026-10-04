"""Admin bearer checks for routes that expose secrets or saved content.

``/projects`` stays open when ``CAIRN_ADMIN_TOKEN`` is unset so local development
keeps working. Routes listed here refuse every request until a token is
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


def fails_closed(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _FAIL_CLOSED_PREFIXES)


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
