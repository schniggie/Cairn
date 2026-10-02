from __future__ import annotations

import re


AUTH_BEARER_RE = re.compile(r"(Authorization:\s*Bearer\s+)([^\s'\"\\]+)", re.IGNORECASE)
JSON_API_KEY_RE = re.compile(r'("(?:apiKey|api_key)"\s*:\s*")([^"]+)(")', re.IGNORECASE)
SK_RE = re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9._-]*")


def redact_text(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    redacted = str(text)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    redacted = AUTH_BEARER_RE.sub(r"\1[redacted]", redacted)
    redacted = JSON_API_KEY_RE.sub(r'\1[redacted]\3', redacted)
    return SK_RE.sub("[redacted]", redacted)
