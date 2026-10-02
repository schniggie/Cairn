from __future__ import annotations

import logging
from typing import Any

from cairn.auth.models import AuthVerificationResult
from cairn.dispatcher.config import AuthTargetConfig

LOG = logging.getLogger(__name__)


class AuthVerifier:
    """Independently re-validates a saved storage state.

    Creating a ``state.json`` does not prove it is still valid, so verification is kept
    separate from capture. Three evidence layers are checked, matching the plan:

    1. protected URL returns the expected HTTP status;
    2. the authenticated selector is present (if configured);
    3. the authenticated API returns the expected HTTP status (if configured).

    The verifier only depends on the Playwright sync API so it can be used both by the
    capture flow and by the standalone ``cairn auth verify`` command. Playwright is
    imported lazily so the rest of Cairn keeps working without it installed.
    """

    def __init__(self, timeout_ms: int = 30_000):
        self.timeout_ms = timeout_ms

    def verify_storage_state(
        self,
        target: AuthTargetConfig,
        storage_state: dict[str, Any],
    ) -> AuthVerificationResult:
        from playwright.sync_api import Error, sync_playwright

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    context = browser.new_context(storage_state=storage_state)
                    try:
                        return self._verify_context(target, context)
                    finally:
                        context.close()
                finally:
                    browser.close()
        except Error as exc:
            return AuthVerificationResult(
                valid=False,
                page_ok=False,
                selector_ok=False,
                api_ok=False,
                reason=str(exc),
            )

    def verify_profile(self, target: AuthTargetConfig, state_path: str) -> AuthVerificationResult:
        import json

        with open(state_path, encoding="utf-8") as handle:
            storage_state = json.load(handle)
        return self.verify_storage_state(target, storage_state)

    def _verify_context(self, target: AuthTargetConfig, context: Any) -> AuthVerificationResult:
        page = context.new_page()
        try:
            verify = target.verify
            page_ok = self._check_page(page, verify.url, verify.expect_status)
            selector_ok = True
            if verify.selector:
                selector_ok = self._check_selector(page, verify.url, verify.selector)
            api_ok = True
            if verify.http_url:
                api_ok = self._check_api(context, verify.http_url, verify.http_expect_status)

            valid = page_ok and selector_ok and api_ok
            reason = None
            if not valid:
                reasons: list[str] = []
                if not page_ok:
                    reasons.append("protected page check failed")
                if not selector_ok:
                    reasons.append("authenticated selector missing")
                if not api_ok:
                    reasons.append("authenticated api check failed")
                reason = "; ".join(reasons)
            return AuthVerificationResult(
                valid=valid,
                page_ok=page_ok,
                selector_ok=selector_ok,
                api_ok=api_ok,
                reason=reason,
            )
        finally:
            page.close()

    def _check_page(self, page: Any, url: str, expect_status: int) -> bool:
        response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        if response is None:
            return expect_status == 200
        return response.status == expect_status

    def _check_selector(self, page: Any, url: str, selector: str) -> bool:
        # Ensure we are on the protected page before probing the selector.
        if page.url.rstrip("/") != url.rstrip("/"):
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        try:
            page.wait_for_selector(selector, timeout=self.timeout_ms)
            return True
        except Exception:
            return False

    def _check_api(self, context: Any, http_url: str, expect_status: int) -> bool:
        response = context.request.get(http_url, timeout=self.timeout_ms)
        return response.status == expect_status
