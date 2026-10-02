from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sqlite3
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit


RUNNER_VERSION = "1.0.0"
HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
FORBIDDEN_HEADERS = {"host", "content-length"}
SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
}
TEXT_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/javascript",
    "application/xml",
    "application/xhtml+xml",
    "application/graphql",
)
MAX_RESPONSE_BODY_BYTES = 65_536
MAX_SCREENSHOT_BYTES = 1_048_576
RATE_WINDOW_SECONDS = 1.01


class CaptureConfigurationError(ValueError):
    pass


def _origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CaptureConfigurationError("target must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise CaptureConfigurationError("target may not contain credentials")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise CaptureConfigurationError("target contains an invalid port") from exc
    return parsed.scheme, parsed.hostname.casefold().rstrip("."), port


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    scheme, host, port = _origin(value)
    default_port = 443 if scheme == "https" else 80
    bracketed = f"[{host}]" if ":" in host else host
    netloc = bracketed if port == default_port else f"{bracketed}:{port}"
    return urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))


def _bounded_int(config: dict[str, Any], key: str, low: int, high: int) -> int:
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise CaptureConfigurationError(f"{key} must be between {low} and {high}")
    return value


def validate_capture_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CaptureConfigurationError("capture configuration must be an object")
    allowed = {
        "target",
        "campaign_id",
        "task_id",
        "request_header",
        "request_header_value",
        "max_requests_per_second",
        "max_requests",
        "max_pages",
        "depth",
        "timeout_seconds",
        "database_path",
        "proxy_url",
    }
    unknown = set(value) - allowed
    if unknown:
        raise CaptureConfigurationError(
            "capture configuration contains unsupported fields: "
            + ", ".join(sorted(unknown))
        )
    target = _canonical_url(str(value.get("target") or ""))
    campaign_id = str(value.get("campaign_id") or "").strip()
    task_id = str(value.get("task_id") or "").strip()
    if not campaign_id or not task_id or any(
        character in campaign_id + task_id for character in "\r\n"
    ):
        raise CaptureConfigurationError("campaign_id and task_id must be single-line values")
    header = str(value.get("request_header") or "").strip()
    header_value = str(value.get("request_header_value") or "").strip()
    if (
        not HEADER_NAME_PATTERN.fullmatch(header)
        or header.casefold() in FORBIDDEN_HEADERS
        or "\r" in header_value
        or "\n" in header_value
    ):
        raise CaptureConfigurationError("request header is unsafe")
    database_path = str(value.get("database_path") or "").strip()
    if database_path and not Path(database_path).is_absolute():
        raise CaptureConfigurationError("database_path must be absolute when provided")
    proxy_url = str(value.get("proxy_url") or "").strip() or None
    if proxy_url is not None:
        parsed_proxy = urlsplit(proxy_url)
        if (
            parsed_proxy.scheme not in {"http", "https", "socks5"}
            or not parsed_proxy.hostname
            or parsed_proxy.username is not None
            or parsed_proxy.password is not None
            or "\r" in proxy_url
            or "\n" in proxy_url
        ):
            raise CaptureConfigurationError("proxy_url is unsafe")
    return {
        "target": target,
        "campaign_id": campaign_id,
        "task_id": task_id,
        "request_header": header,
        "request_header_value": header_value,
        "max_requests_per_second": _bounded_int(
            value, "max_requests_per_second", 1, 1000
        ),
        "max_requests": _bounded_int(value, "max_requests", 1, 50),
        "max_pages": _bounded_int(value, "max_pages", 1, 10),
        "depth": _bounded_int(value, "depth", 1, 2),
        "timeout_seconds": _bounded_int(value, "timeout_seconds", 5, 60),
        "database_path": database_path or None,
        "proxy_url": proxy_url,
    }


def request_is_allowed(
    root_url: str, request_url: str, method: str, consumed: int, limit: int
) -> tuple[bool, str]:
    if method.upper() not in {"GET", "HEAD"}:
        return False, "state_changing_method"
    try:
        if _origin(request_url) != _origin(root_url):
            return False, "cross_origin"
    except CaptureConfigurationError:
        return False, "unsupported_scheme"
    if consumed >= limit:
        return False, "request_budget_exhausted"
    return True, "allowed"


class DurableRateLimiter:
    def __init__(
        self,
        *,
        database_path: str | None,
        campaign_id: str,
        task_id: str,
        limit: int,
    ) -> None:
        self.database_path = database_path
        self.campaign_id = campaign_id
        self.task_id = task_id
        self.limit = max(1, limit)
        self.local_events: deque[float] = deque()

    def reserve(self) -> None:
        while True:
            now = time.time()
            if self.database_path is None:
                while self.local_events and self.local_events[0] <= now - RATE_WINDOW_SECONDS:
                    self.local_events.popleft()
                if len(self.local_events) < self.limit:
                    self.local_events.append(now)
                    return
                oldest = self.local_events[0]
            else:
                conn = sqlite3.connect(self.database_path, timeout=30)
                conn.row_factory = sqlite3.Row
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "DELETE FROM vuln_http_rate_events "
                        "WHERE campaign_id = ? AND observed_at <= ?",
                        (self.campaign_id, now - RATE_WINDOW_SECONDS),
                    )
                    rows = conn.execute(
                        "SELECT observed_at FROM vuln_http_rate_events "
                        "WHERE campaign_id = ? ORDER BY observed_at",
                        (self.campaign_id,),
                    ).fetchall()
                    if len(rows) < self.limit:
                        conn.execute(
                            "INSERT INTO vuln_http_rate_events "
                            "(campaign_id, task_id, observed_at) VALUES (?, ?, ?)",
                            (self.campaign_id, self.task_id, now),
                        )
                        conn.commit()
                        return
                    oldest = float(rows[0]["observed_at"])
                    conn.commit()
                finally:
                    conn.close()
            time.sleep(max(0.001, oldest + RATE_WINDOW_SECONDS - now))


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        str(name)[:256]: (
            "[REDACTED]" if str(name).casefold() in SENSITIVE_HEADERS else str(value)[:4096]
        )
        for name, value in list(headers.items())[:100]
    }


@dataclass
class CaptureState:
    config: dict[str, Any]
    limiter: DurableRateLimiter
    request_count: int = 0
    blocked: list[dict[str, str]] | None = None
    responses: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        self.blocked = []
        self.responses = []

    def route(self, route: Any, request: Any) -> None:
        allowed, reason = request_is_allowed(
            self.config["target"],
            request.url,
            request.method,
            self.request_count,
            self.config["max_requests"],
        )
        if not allowed:
            assert self.blocked is not None
            self.blocked.append(
                {"url": str(request.url)[:4096], "method": request.method, "reason": reason}
            )
            route.abort("blockedbyclient")
            return
        self.limiter.reserve()
        self.request_count += 1
        route.continue_()

    def response(self, response: Any) -> None:
        request = response.request
        allowed, _ = request_is_allowed(
            self.config["target"], request.url, request.method, 0, 1
        )
        if not allowed:
            return
        headers = _safe_headers(dict(response.headers))
        assert self.responses is not None
        self.responses.append(
            {
                "url": _canonical_url(request.url),
                "method": request.method,
                "resource_type": request.resource_type,
                "status_code": response.status,
                "request_headers": _safe_headers(dict(request.headers)),
                "response_headers": headers,
                # Reading response.body() from the synchronous response callback
                # can deadlock navigation.  The completed document DOM is attached
                # outside the callback by attach_document_body().
                "body_text": "",
                "body_sha256": hashlib.sha256(b"").hexdigest(),
                "body_truncated": False,
                "body_source": "not_captured",
            }
        )

    def attach_document_body(self, url: str, content: str) -> None:
        """Attach bounded rendered DOM to the matching completed document response."""
        assert self.responses is not None
        canonical = _canonical_url(url)
        raw = content.encode("utf-8")
        truncated = len(raw) > MAX_RESPONSE_BODY_BYTES
        body = raw[:MAX_RESPONSE_BODY_BYTES]
        for record in reversed(self.responses):
            if (
                record.get("resource_type") == "document"
                and record.get("url") == canonical
            ):
                record["body_text"] = body.decode("utf-8", errors="replace")
                record["body_sha256"] = hashlib.sha256(body).hexdigest()
                record["body_truncated"] = truncated
                record["body_source"] = "rendered_dom"
                return


def run_capture(config: dict[str, Any]) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright Python package is not installed") from exc

    limiter = DurableRateLimiter(
        database_path=config["database_path"],
        campaign_id=config["campaign_id"],
        task_id=config["task_id"],
        limit=config["max_requests_per_second"],
    )
    state = CaptureState(config=config, limiter=limiter)
    pages: list[dict[str, Any]] = []
    queued: deque[tuple[str, int]] = deque([(config["target"], 1)])
    seen: set[str] = set()
    started = time.monotonic()
    with sync_playwright() as playwright:
        launch_options: dict[str, Any] = {
            "headless": True,
            "args": [
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-sync",
                "--metrics-recording-only",
                "--no-first-run",
            ],
        }
        if config["proxy_url"]:
            launch_options["proxy"] = {"server": config["proxy_url"]}
        browser = playwright.chromium.launch(**launch_options)
        try:
            context = browser.new_context(
                accept_downloads=False,
                service_workers="block",
                ignore_https_errors=False,
                java_script_enabled=True,
                viewport={"width": 1280, "height": 720},
                extra_http_headers={
                    config["request_header"]: config["request_header_value"]
                },
            )
            context.set_default_timeout(10_000)
            context.set_default_navigation_timeout(10_000)
            context.route("**/*", state.route)
            context.route_web_socket(
                "**/*", lambda websocket: websocket.close(code=1000, reason="Cairn blocked")
            )
            page = context.new_page()
            page.on("dialog", lambda dialog: dialog.dismiss())
            page.on("response", state.response)
            while queued and len(pages) < config["max_pages"]:
                if time.monotonic() - started >= config["timeout_seconds"]:
                    break
                url, depth = queued.popleft()
                canonical = _canonical_url(url)
                if canonical in seen or _origin(canonical) != _origin(config["target"]):
                    continue
                seen.add(canonical)
                try:
                    response = page.goto(canonical, wait_until="domcontentloaded")
                    final_url = _canonical_url(page.url)
                    if _origin(final_url) != _origin(config["target"]):
                        raise RuntimeError("navigation escaped the approved origin")
                    links = page.eval_on_selector_all(
                        "a[href]", "elements => elements.map(item => item.href).slice(0, 200)"
                    )
                    scripts = page.eval_on_selector_all(
                        "script[src]", "elements => elements.map(item => item.src).slice(0, 100)"
                    )
                    forms = page.eval_on_selector_all(
                        "form",
                        "elements => elements.slice(0, 50).map(item => ({"
                        "action: item.action, method: (item.method || 'get').toUpperCase(), "
                        "fields: Array.from(item.elements).slice(0, 100).map(field => ({"
                        "name: field.name || '', type: field.type || field.tagName.toLowerCase()"
                        "}))}))",
                    )
                    scoped_links = []
                    for link in links if isinstance(links, list) else []:
                        try:
                            resolved = _canonical_url(urljoin(final_url, str(link)))
                        except CaptureConfigurationError:
                            continue
                        if _origin(resolved) == _origin(config["target"]):
                            scoped_links.append(resolved)
                            if depth < config["depth"]:
                                queued.append((resolved, depth + 1))
                    scoped_scripts = []
                    for script in scripts if isinstance(scripts, list) else []:
                        try:
                            resolved = _canonical_url(urljoin(final_url, str(script)))
                        except CaptureConfigurationError:
                            continue
                        if _origin(resolved) == _origin(config["target"]):
                            scoped_scripts.append(resolved)
                    scoped_forms = []
                    for form in forms if isinstance(forms, list) else []:
                        if not isinstance(form, dict):
                            continue
                        try:
                            action = _canonical_url(
                                urljoin(final_url, str(form.get("action") or final_url))
                            )
                        except CaptureConfigurationError:
                            continue
                        if _origin(action) != _origin(config["target"]):
                            continue
                        scoped_forms.append(
                            {
                                "action": action,
                                "method": str(form.get("method") or "GET").upper()[:16],
                                "fields": form.get("fields")
                                if isinstance(form.get("fields"), list)
                                else [],
                            }
                        )
                    dom_content = page.content()
                    state.attach_document_body(final_url, dom_content)
                    screenshot = page.screenshot(type="jpeg", quality=60, full_page=False)
                    if len(screenshot) > MAX_SCREENSHOT_BYTES:
                        screenshot = b""
                    pages.append(
                        {
                            "url": canonical,
                            "final_url": final_url,
                            "depth": depth,
                            "status_code": response.status if response is not None else None,
                            "title": page.title()[:1000],
                            "links": sorted(set(scoped_links))[:200],
                            "scripts": sorted(set(scoped_scripts))[:100],
                            "forms": scoped_forms,
                            "dom_sha256": hashlib.sha256(dom_content.encode("utf-8")).hexdigest(),
                            "screenshot_sha256": hashlib.sha256(screenshot).hexdigest() if screenshot else None,
                            "screenshot_base64": base64.b64encode(screenshot).decode("ascii") if screenshot else None,
                        }
                    )
                except Exception as exc:
                    pages.append(
                        {
                            "url": canonical,
                            "depth": depth,
                            "error": f"{type(exc).__name__}: {exc}"[:1000],
                        }
                    )
            context.close()
        finally:
            browser.close()
    if not pages or all(page.get("error") for page in pages):
        raise RuntimeError("browser session produced no successful page capture")
    return {
        "target": config["target"],
        "pages": pages,
        "responses": (state.responses or [])[: config["max_requests"]],
        "blocked_requests": (state.blocked or [])[:200],
        "request_count": state.request_count,
        "request_budget": config["max_requests"],
        "rate_limit": config["max_requests_per_second"],
        "same_origin_only": True,
        "allowed_methods": ["GET", "HEAD"],
        "websockets_blocked": True,
        "service_workers_blocked": True,
        "persistent_profile": False,
        "network_path": "proxy" if config["proxy_url"] else "direct",
    }


def probe_runtime() -> dict[str, str]:
    from importlib.metadata import version

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        executable = Path(playwright.chromium.executable_path)
        if not executable.is_file():
            raise RuntimeError("Playwright Chromium is not installed")
        browser = playwright.chromium.launch(headless=True)
        try:
            browser_version = browser.version
        finally:
            browser.close()
    return {
        "package_version": version("playwright"),
        "browser_version": browser_version,
        "browser_executable": str(executable),
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Cairn controlled Playwright capture")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if args.version:
        print(f"cairn-playwright-capture {RUNNER_VERSION}")
        return 0
    if args.probe:
        try:
            print(json.dumps(probe_runtime(), sort_keys=True))
            return 0
        except Exception as exc:
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    if not args.run:
        parser.error("--run is required")
    try:
        raw = sys.stdin.read(65_537)
        if len(raw.encode("utf-8")) > 65_536:
            raise CaptureConfigurationError("capture configuration exceeds 64 KiB")
        config = validate_capture_config(json.loads(raw))
        print(json.dumps(run_capture(config), ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
