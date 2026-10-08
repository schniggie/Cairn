from __future__ import annotations

import re
from dataclasses import dataclass, field

# Patterns that indicate sensitive authentication material leaking into text that is
# about to reach the Cairn graph. Ordered from most to least specific so the most
# dangerous tokens are flagged first.
_SENSITIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("authorization_bearer", re.compile(r"Authorization\s*:\s*Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)),
    ("authorization", re.compile(r"Authorization\s*:\s*[^\s]+", re.IGNORECASE)),
    ("set_cookie", re.compile(r"Set-Cookie\s*:\s*[^\r\n]+", re.IGNORECASE)),
    ("cookie", re.compile(r"Cookie\s*:\s*[^\r\n]+", re.IGNORECASE)),
    ("session", re.compile(r"session\s*=\s*[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)),
    ("access_token", re.compile(r"access_token\s*=\s*[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)),
    ("refresh_token", re.compile(r"refresh_token\s*=\s*[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)),
]

_REDACTED = "<redacted>"


@dataclass(slots=True)
class SanitizeResult:
    text: str
    leaks: list[str] = field(default_factory=list)

    @property
    def has_leaks(self) -> bool:
        return bool(self.leaks)


def detect_leaks(text: str) -> list[str]:
    """Return the list of sensitive-material categories detected in ``text``."""
    found: list[str] = []
    for name, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(text) and name not in found:
            found.append(name)
    return found


def redact(text: str) -> str:
    """Replace every sensitive match with ``<redacted>``, preserving surrounding text."""
    for _name, pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub(lambda m: _redact_match(m.group(0)), text)
    return text


def sanitize(text: str) -> SanitizeResult:
    """Detect sensitive material and redact it in one pass.

    The plan prefers fully deleting leaked fields; ``redact`` replaces the secret value
    while keeping the field name (e.g. ``Authorization: Bearer <redacted>``) so the fact
    remains diagnosable without exposing the credential.
    """
    leaks = detect_leaks(text)
    if not leaks:
        return SanitizeResult(text=text, leaks=[])
    return SanitizeResult(text=redact(text), leaks=leaks)


def _redact_match(match: str) -> str:
    # Keep the field label (up to the value) but never the value itself.
    for label in ("Authorization:", "Set-Cookie:", "Cookie:", "session=", "access_token=", "refresh_token="):
        if label.lower() in match.lower():
            idx = match.lower().find(label.lower())
            prefix = match[: idx + len(label)]
            suffix = match[idx + len(label):]
            if suffix.strip() == "":
                return match
            return f"{prefix} {_REDACTED}"
    return _REDACTED
