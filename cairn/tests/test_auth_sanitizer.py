from __future__ import annotations

from cairn.auth.sanitizer import detect_leaks, redact, sanitize


def test_detect_authorization_bearer() -> None:
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def"
    leaks = detect_leaks(text)
    assert "authorization_bearer" in leaks


def test_redact_strips_token_value() -> None:
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def"
    redacted = redact(text)
    assert "eyJ" not in redacted
    assert "<redacted>" in redacted
    assert "Authorization" in redacted


def test_sanitize_reports_and_redacts_session_cookie() -> None:
    result = sanitize("Cookie: session=abc123def456")
    assert result.has_leaks
    assert "abc123def456" not in result.text
    assert "<redacted>" in result.text


def test_sanitize_detects_set_cookie() -> None:
    result = sanitize("Set-Cookie: sessionid=sekret; Path=/; HttpOnly")
    assert result.has_leaks
    assert "sekret" not in result.text


def test_sanitize_detects_access_and_refresh_tokens() -> None:
    text = "access_token=aaaa refresh_token=bbbb"
    result = sanitize(text)
    assert "access_token" in result.leaks
    assert "refresh_token" in result.leaks
    assert "aaaa" not in result.text
    assert "bbbb" not in result.text


def test_sanitize_clean_text_is_unchanged() -> None:
    text = "The dashboard returned HTTP 200 and the user menu is visible."
    result = sanitize(text)
    assert not result.has_leaks
    assert result.text == text
