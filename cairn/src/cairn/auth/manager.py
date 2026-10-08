from __future__ import annotations

import logging
from typing import Any

from cairn.auth.models import AuthCaptureResult
from cairn.auth.verifier import AuthVerifier
from cairn.dispatcher.config import AuthTargetConfig

LOG = logging.getLogger(__name__)


class AuthManager:
    """Opens a headed browser so a human operator can log in, then captures the session.

    The design keeps humans responsible for completing identity challenges (password,
    SSO, MFA, CAPTCHA, hardware keys) while the agent later *consumes* the verified
    session. The manager never attempts to solve those challenges automatically.

    The interactive wait loop runs in a separate thread and returns as soon as the
    verifier confirms a valid session, or when the operator stops the wait (login
    timeout). ``on_status`` is an optional callback used to stream progress back to the
    CLI.
    """

    def __init__(self, verifier: AuthVerifier | None = None):
        self.verifier = verifier or AuthVerifier()

    def capture_interactive(
        self,
        target: AuthTargetConfig,
        *,
        login_timeout_seconds: int = 600,
        verify_timeout_seconds: int = 30,
        on_status: Any | None = None,
    ) -> AuthCaptureResult:
        from playwright.sync_api import Error, sync_playwright

        verifier = AuthVerifier(timeout_ms=verify_timeout_seconds * 1000)

        def report(message: str) -> None:
            LOG.info("%s", message)
            if on_status is not None:
                on_status(message)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            try:
                context = browser.new_context()
                page = context.new_page()
                report(f"Opening login page: {target.login_url}")
                page.goto(target.login_url, wait_until="domcontentloaded")
                report(
                    "Please complete login in the browser window "
                    "(password / SSO / MFA / CAPTCHA / hardware key). "
                    "Waiting for a verified authenticated session..."
                )

                verification = self._wait_for_verified_session(
                    target,
                    context,
                    verifier,
                    timeout_seconds=login_timeout_seconds,
                )
                if verification is None:
                    report("Login timed out before an authenticated session was verified.")
                    return AuthCaptureResult(storage_state=None, verification=_invalid_timeout())

                storage_state = context.storage_state(indexed_db=target.indexed_db)
                return AuthCaptureResult(storage_state=storage_state, verification=verification)
            finally:
                browser.close()

    def _wait_for_verified_session(
        self,
        target: AuthTargetConfig,
        context: Any,
        verifier: AuthVerifier,
        *,
        timeout_seconds: int,
    ):
        import time
        from playwright.sync_api import Error

        deadline = time.monotonic() + timeout_seconds
        page = context.new_page()
        try:
            while time.monotonic() < deadline:
                # Lightweight probe: protected page status + selector. Once the operator
                # appears logged in, run the full authoritative verification against the
                # live context (including the authenticated API layer).
                if self._probe(target, verifier, page):
                    try:
                        return verifier._verify_context(target, context)
                    except Error as exc:
                        return _invalid_browser_error(exc)
                time.sleep(1)
            return None
        finally:
            page.close()

    def _probe(self, target: AuthTargetConfig, verifier: AuthVerifier, page: Any) -> bool:
        verify = target.verify
        try:
            response = page.goto(verify.url, wait_until="domcontentloaded", timeout=verifier.timeout_ms)
            page_ok = response is not None and response.status == verify.expect_status
        except Exception:
            page_ok = False
        if not page_ok:
            return False
        if verify.selector:
            try:
                page.wait_for_selector(verify.selector, timeout=verifier.timeout_ms)
            except Exception:
                return False
        return True


def _invalid_timeout():
    from cairn.auth.models import AuthVerificationResult

    return AuthVerificationResult(
        valid=False,
        page_ok=False,
        selector_ok=False,
        api_ok=False,
        reason="login timed out",
    )


def _invalid_browser_error(exc: Exception):
    from cairn.auth.models import AuthVerificationResult

    return AuthVerificationResult(
        valid=False,
        page_ok=False,
        selector_ok=False,
        api_ok=False,
        reason=str(exc),
    )
