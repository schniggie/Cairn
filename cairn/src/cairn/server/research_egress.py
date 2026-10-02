"""Controlled research egress: a local enforcement proxy the sandbox may not bypass.

The execution layer starts a loopback ``EgressProxy`` and forces ALL of the sandbox's
outbound TCP through it (sandbox proxy env + a preload ``connect()`` interceptor so
direct-connect cannot skip the proxy). The proxy applies authorization at request time:

* Only ``http`` and ``https`` targets are supported; other protocols are refused.
* Each target request is checked against the approved scope, exclusion list, and the
  remaining request quota BEFORE onward forwarding (quota cap stops new requests but
  the worker may still save evidence + settle).
* Target response redirects are re-checked against the same scope before being relayed.
* Retries are counted separately from new target requests.
* Model-gateway traffic is classified separately and never counts against the
  research-target request quota.

Pure-enough socket logic so it is unit-testable against local HTTP fixtures without a
real model. The worker owns start/stop and reads counters back into the run ledger.
"""
from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

LOG = logging.getLogger(__name__)

HTTP = "http"
HTTPS = "https"
UNSUPPORTED = "unsupported"


def classify_scheme(raw: str) -> str:
    """Return HTTP / HTTPS / UNSUPPORTED for a request target scheme."""
    scheme = (raw or "").strip().lower().split(":", 1)[0] if isinstance(raw, str) else ""
    if scheme in ("http", ""):
        return HTTP
    if scheme == "https":
        return HTTPS
    return UNSUPPORTED


# Well-known model-gateway hosts. The execution layer classifies these as "model"
# traffic so the research model can reach its own provider gateway WITHOUT being
# treated as an out-of-scope target and without consuming the target request quota.
# The operator's own gateway host (from Claude config) is added on top at runtime.
DEFAULT_MODEL_HOSTS = {
    "api.anthropic.com", "api.anthropic.dev", "api.claude.com", "claude.ai",
    "console.anthropic.com", "api.openai.com", "api.deepseek.com",
    "api.x.ai", "api.groq.com",
}


def _host_from_url(value: str) -> str | None:
    try:
        parts = urlsplit(value)
        return (parts.hostname or "").lower() or None
    except Exception:
        return None


def extract_gateway_hosts(operator_home) -> list[str]:
    """Best-effort extraction of the operator's model-gateway host(s) from their Claude
    config files (host names only — never credentials). Used to classify model traffic
    separately from research-target traffic in the egress proxy."""
    import json as _json
    home = Path(operator_home) if operator_home else None
    if home is None or not home.is_dir():
        return []
    hosts: list[str] = []

    def _collect(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and v and not v.startswith("sk-"):
                    key = k.lower()
                    if "url" in key or "host" in key or key in ("baseurl", "endpoint"):
                        h = _host_from_url(v)
                        if h:
                            hosts.append(h)
                elif isinstance(v, (dict, list)):
                    _collect(v)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    _collect(item)

    for name in (".claude.json", ".claude/settings.json", ".claude/settings.local.json"):
        p = home / name
        if not p.is_file() or p.is_symlink():
            continue
        try:
            _collect(_json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return list(dict.fromkeys(hosts))


@dataclass
class ScopeRule:
    host: str
    port: int | None = None
    prefix: str = ""


def parse_scope(targets: list[str]) -> list[ScopeRule]:
    """Turn approved/excluded target URL strings into match rules. Non-http(s) schemes
    are dropped from scope because they are never reachable through this proxy."""
    rules: list[ScopeRule] = []
    for raw in targets or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        if classify_scheme(raw) == UNSUPPORTED:
            continue
        parts = urlsplit(raw if (":" in raw.split("/", 1)[0]) else "//" + raw)
        host = (parts.hostname or "").lower()
        if not host:
            continue
        rules.append(ScopeRule(host=host, port=parts.port or (443 if parts.scheme == 'https' else 80),
                               prefix=parts.path or ""))
    return rules


def scope_decision(host: str, port: int, allow_rules: list[ScopeRule],
                   block_rules: list[ScopeRule]) -> str:
    """Pure engine: exclusion wins, then allow-list. Returns
    'allowed' | 'excluded' | 'blocked'."""
    host_m = (host or "").lower().strip()
    for r in block_rules:
        if host_m == r.host and (r.port is None or r.port == port):
            return "excluded"
    for r in allow_rules:
        if host_m == r.host and (r.port is None or r.port == port):
            return "allowed"
    return "blocked"


@dataclass
class QuotaState:
    """Target-request quota. Model hosts never consume target quota. ``requests``
    counts new target requests; ``retries`` counts repeated attempts separately."""
    remaining: int
    model_hosts: set[str] = field(default_factory=set)
    requests: int = 0
    retries: int = 0
    _recent: dict = field(default_factory=dict)

    RETRY_WINDOW_SECONDS = 30.0

    def _targets(self):
        for raw in self.model_hosts:
            parts = urlsplit(raw if (":" in raw.split("/", 1)[0]) else "//" + raw)
            yield (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)

    def classify(self, host: str, port: int) -> str:
        # A model-gateway host is model traffic regardless of which port the provider
        # endpoint uses (413 vs 80 for a bare host name), so match on host only.
        model_hosts = {h for h, _p in self._targets()}
        if (host or "").lower() in model_hosts:
            return "model"
        return "target"

    def authorize(self, host: str, port: int) -> tuple[bool, str]:
        if self.classify(host, port) == "model":
            return True, "model"
        if self.remaining <= 0:
            return False, "quota_exhausted"
        self.remaining -= 1
        self.requests += 1
        return True, "target"

    def is_retry(self, host: str, port: int, prefix: str) -> bool:
        key = (host or "").lower(), port
        now = time.monotonic()
        prev = self._recent.get(key)
        if prev is not None and prev[0] == prefix and (now - prev[1]) <= self.RETRY_WINDOW_SECONDS:
            self.retries += 1
            self._recent[key] = (prefix, now)
            return True
        self._recent[key] = (prefix, now)
        return False


class _EgressServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def _proxy(self) -> "EgressProxy":
        return self.server.proxy  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # keep the console quiet
        LOG.debug("egress: " + fmt, *args)

    def _target(self):
        line = self.path or ""
        if self.command == "CONNECT":
            # CONNECT host:port  -> TLS tunnel
            hostpart, p = line, ""
            if ":" in line:
                hostpart, p = line.split(":", 1)
            port = int(p) if p.isdigit() else 443
            return ("https", hostpart, port, "")
        parts = urlsplit(line)
        host = parts.hostname or (self.headers.get("Host") or "").split(":")[0]
        port = parts.port if parts.port else 80
        scheme = "https" if line.startswith("https://") else "http"
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        return (scheme, (host or "").lower(), port, path)

    def _reply(self, code, msg, body=b"", headers=()):
        # send_response writes the reason-line as latin-1; keep it ASCII and carry any
        # human-readable (possibly non-ASCII) detail only in the response body.
        safe = "".join(ch for ch in str(msg)[:60] if ord(ch) < 128) or str(code)
        self.send_response(code, safe[:60] or str(code))
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))

    def do_CONNECT(self):
        scheme, host, port, path = self._target()
        action, reason = self._proxy.decide(scheme, host, port)
        if action.startswith("refuse"):
            self._reply(403, reason or "refused", body=(reason or "").encode("utf-8"))
            return
        try:
            upstream = socket.create_connection((host, port), timeout=self._proxy.timeout)
        except OSError:
            self._reply(502, "upstream unreachable")
            return
        self.send_response(200, "Connection established")
        self.end_headers()
        self._tunnel(upstream)

    def do_GET(self):
        self._via("GET")

    def do_POST(self):
        self._via("POST")

    def do_HEAD(self):
        self._via("HEAD")

    def do_PUT(self):
        self._via("PUT")

    def do_DELETE(self):
        self._via("DELETE")

    def _via(self, method):
        scheme, host, port, path = self._target()
        action, reason = self._proxy.decide(scheme, host, port)
        if action.startswith("refuse"):
            self._reply(403, reason or "refused", body=(reason or "").encode("utf-8"))
            return
        body = self._body()
        try:
            status, headers, resp_body = self._proxy.forward_http(
                method, host, port, path, self.headers, body)
        except OSError:
            self._reply(502, "upstream unreachable")
            return
        location = headers.get("location") or headers.get("Location")
        if action == "proxy_target" and status and status[0] in ("3",) and location:
            loc = urlsplit(location)
            dhost = loc.hostname or host
            dport = loc.port or (443 if loc.scheme == "https" else 80)
            if scope_decision(dhost, dport, self._proxy.allow_rules, self._proxy.block_rules) != "allowed":
                self._proxy.stats["redirect_blocked"] += 1
                self._reply(403, "redirect out of authorized scope",
                            body="重定向目标超出授权范围".encode("utf-8"))
                return
        # relay the captured response back to the sandbox
        self.send_response(int(status.split(" ")[0]))
        for k, v in headers.items():
            if k.lower() in ("transfer-encoding", "connection"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        if method != "HEAD" and resp_body:
            self.wfile.write(resp_body)

    def _tunnel(self, upstream):
        self.connection.settimeout(30)
        upstream.settimeout(30)
        try:
            both = [self.connection, upstream]
            while True:
                for sock in list(both):
                    if sock not in both:
                        continue
                    data = sock.recv(65536)
                    if not data:
                        return
                    dst = upstream if sock is self.connection else self.connection
                    dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                upstream.close()
            except OSError:
                pass


class EgressProxy:
    """Loopback enforcement proxy. ``allow``/``block`` are URL lists; ``model_hosts``
    are gateway hosts classified separately (never quota-counted). Unsupported
    protocols are refused outright. ``strict`` controls whether an absent allow-list
    refuses everything (default) or allows loopback fixtures only in tests."""
    timeout = 8.0

    def __init__(self, *, allow=None, block=None, request_quota=0, model_hosts=None,
                 bind_host="127.0.0.1", bind_port=0, strict=True, allow_loopback=None):
        self.allow_rules = parse_scope(allow or [])
        self.block_rules = parse_scope(block or [])
        self.model_hosts = list(model_hosts or [])
        # Always reachable loopback fixtures for tests are not part of the real allow-list.
        self.quota = QuotaState(remaining=request_quota, model_hosts=self.model_hosts)
        self._server = None
        self._thread = None
        self.port = bind_port
        self.host = bind_host
        self.stash_model_host_rule = allow_loopback
        self.stats = {"requests": 0, "blocked": 0, "excluded": 0, "unsupported": 0,
                      "quota_exhausted": 0, "redirect_blocked": 0, "model": 0}

    def start(self):
        server = _EgressServer((self.host, self.port), _Handler)
        server.proxy = self  # type: ignore[attr-defined]
        self._server = server
        self.port = server.server_address[1]
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def decide(self, scheme, host, port):
        """Return (action, reason); action ∈ proxy_model | proxy_target | refuse_*."""
        if scheme == UNSUPPORTED:
            self.stats["unsupported"] += 1
            return ("refuse_protocol", "仅支持 HTTP/HTTPS 目标")
        host = (host or "").lower()
        if self.quota.classify(host, port) == "model":
            self.stats["model"] += 1
            return ("proxy_model", None)
        d = scope_decision(host, port, self.allow_rules, self.block_rules)
        if d == "excluded":
            self.stats["excluded"] += 1
            return ("refuse_scope", f"目标在排除列表：{host}")
        if d == "blocked":
            self.stats["blocked"] += 1
            return ("refuse_scope", f"目标超出授权范围：{host}")
        ok, reason = self.quota.authorize(host, port)
        if not ok:
            self.stats["quota_exhausted"] += 1
            return ("refuse_quota", f"研究请求配额已耗尽")
        self.stats["requests"] += 1
        return ("proxy_target", None)

    def forward_http(self, method, host, port, path, headers, body):
        import http.client
        if port == 443:
            conn = http.client.HTTPSConnection(host, port, timeout=self.timeout)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
        conn.request(method, path, body=body or None,
                     headers={k: v for k, v in headers.items() if k.lower() not in
                              ("proxy-connection", "connection", "content-length")})
        resp = conn.getresponse()
        resp_body = resp.read()
        status = f"{resp.status} {resp.reason}"
        out_headers = {k: v for k, v in resp.getheaders()}
        conn.close()
        return status, out_headers, resp_body

def build_egress_preload(out_dir=None):
    """Compile ``egress_preload.c`` (the LD_PRELOAD ``connect()`` interceptor) into a
    shared object. Returns the path to ``libcairn_egress.so`` or None when no C
    compiler is available (execution stops being bypass-hardened rather than silently
    skipping in production)."""
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        return None
    src = Path(__file__).with_name("egress_preload.c")
    if not src.is_file():
        return None
    out = (Path(out_dir) if out_dir else Path(tempfile.gettempdir())) / "libcairn_egress.so"
    try:
        r = subprocess.run([cc, "-shared", "-fPIC", "-O2", str(src), "-ldl",
                            "-o", str(out)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return str(out)
