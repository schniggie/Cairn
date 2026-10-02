"""Reliability hardening #5: controlled research egress (scope / protocol / quota /
redirect re-check / retry / model-vs-target classification). Pure decision logic and a
loopback enforcement proxy against local HTTP fixtures — no real model needed."""
import http.server
import socket
import sys
import threading
import urllib.parse

import pytest

from cairn.server.research_egress import (EgressProxy, QuotaState, UNSUPPORTED,
                                          classify_scheme, parse_scope,
                                          scope_decision)


class _Fixture(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://evil.example/inject")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/echo"):
            body = self.path.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b"fixture-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_fixture():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fixture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address
    return srv, "127.0.0.1", port


def _proxied_get(proxy_port, host, port, path="/"):
    """Issue a plain HTTP GET through the enforcement proxy using absolute-URI form,
    the same shape a model behind HTTP_PROXY would produce."""
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=8)
    req = (f"GET http://{host}:{port}{path} HTTP/1.1\r\n"
           f"Host: {host}:{port}\r\nConnection: close\r\n\r\n")
    sock.sendall(req.encode("utf-8"))
    data = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    sock.close()
    head, _, body = data.partition(b"\r\n\r\n")
    status = head.split(b" ", 2)[1].decode() if len(head.split(b" ", 2)) > 1 else "000"
    return int(status), body


# -- pure decision logic -----------------------------------------------------


def test_classify_scheme():
    assert classify_scheme("http") == "http"
    assert classify_scheme("https://x") == "https"
    assert classify_scheme("ftp://x") == UNSUPPORTED
    assert classify_scheme("file:///etc/passwd") == UNSUPPORTED
    assert classify_scheme("gopher://x") == UNSUPPORTED


def test_scope_decision_exclusion_wins():
    allow = parse_scope(["http://target.example", "https://api.example"])
    assert scope_decision("target.example", 80, allow, []) == "allowed"
    assert scope_decision("api.example", 443, allow, []) == "allowed"
    assert scope_decision("target.example", 443, allow, []) == "blocked"  # wrong port
    assert scope_decision("elsewhere.example", 80, allow, []) == "blocked"
    # exclusion wins at the same host:port
    block = parse_scope(["http://target.example/critical"])
    assert scope_decision("target.example", 80, allow, block) == "excluded"


def test_quota_model_not_counted_and_retries_separate():
    q = QuotaState(remaining=2, model_hosts=["https://model.example"])
    assert q.classify("model.example", 443) == "model"
    ok, _ = q.authorize("model.example", 443)
    assert ok and q.requests == 0  # model traffic never consumes target quota
    ok, _ = q.authorize("target.example", 80)
    assert ok and q.requests == 1
    ok, _ = q.authorize("target2.example", 80)
    assert ok and q.requests == 2
    ok, reason = q.authorize("target3.example", 80)
    assert ok is False and reason == "quota_exhausted"
    # retries counted separately from new requests
    q2 = QuotaState(remaining=10)
    assert q2.is_retry("a.example", 80, "/x") is False
    assert q2.is_retry("a.example", 80, "/x") is True
    assert q2.requests == 0 and q2.retries == 1


# -- enforcement proxy against local fixtures ---------------------------------


@pytest.fixture
def fixture():
    srv, host, port = _start_fixture()
    yield host, port
    srv.shutdown()
    srv.server_close()


def test_proxy_allows_in_scope_and_blocks_out_of_scope(fixture):
    host, port = fixture
    proxy = EgressProxy(allow=[f"http://{host}:{port}"], request_quota=5)
    pp = proxy.start()
    try:
        status, body = _proxied_get(pp, host, port)
        assert status == 200 and b"fixture-ok" in body
        # out-of-scope host refused (403) — cannot be reached
        status, body = _proxied_get(pp, "notallowed.example", 80)
        assert status == 403
        assert proxy.stats["blocked"] >= 1
    finally:
        proxy.stop()


def test_proxy_exclusion_list(fixture):
    host, port = fixture
    proxy = EgressProxy(allow=[f"http://{host}:{port}"],
                        block=[f"http://{host}:{port}"], request_quota=5)
    pp = proxy.start()
    try:
        status, _ = _proxied_get(pp, host, port)
        assert status == 403
        assert proxy.stats["excluded"] >= 1
    finally:
        proxy.stop()


def test_proxy_quota_stops_new_target_requests_but_keeps_model(fixture):
    host, port = fixture
    proxy = EgressProxy(allow=[f"http://{host}:{port}", "http://other.example"],
                        model_hosts=["http://model.example"],
                        request_quota=1)
    pp = proxy.start()
    try:
        status, _ = _proxied_get(pp, host, port)
        assert status == 200
        # quota exhausted: next target request refused (quota counter incremented)
        status, _ = _proxied_get(pp, "other.example", 80)
        assert status == 403
        assert proxy.stats["quota_exhausted"] == 1
    finally:
        proxy.stop()


def test_proxy_redirect_out_of_scope_reblocked(fixture):
    host, port = fixture
    proxy = EgressProxy(allow=[f"http://{host}:{port}"], request_quota=5)
    pp = proxy.start()
    try:
        status, body = _proxied_get(pp, host, port, "/redirect")
        # the target returns 302 to evil.example; proxy re-checks scope and blocks it
        assert status != 200
        assert proxy.stats["redirect_blocked"] >= 1
    finally:
        proxy.stop()


def test_proxy_unsupported_protocol_refused():
    proxy = EgressProxy(allow=[], request_quota=0)
    action, reason = proxy.decide(UNSUPPORTED, "target.example", 21)
    assert action == "refuse_protocol"
    assert proxy.stats["unsupported"] == 1


def test_proxy_forwards_target_query_verbatim(fixture):
    """Target requests must be forwarded as-is, including the query string (M1收尾)."""
    host, port = fixture
    proxy = EgressProxy(allow=[f"http://{host}:{port}"], request_quota=5)
    pp = proxy.start()
    try:
        status, body = _proxied_get(pp, host, port, "/echo?key=v1&x=%20s")
        assert status == 200
        assert b"/echo?key=v1&x=%20s" in body, body
    finally:
        proxy.stop()


def test_extract_gateway_hosts_no_credentials(tmp_path):
    """Model-gateway host discovery reads host names only, never credentials."""
    from cairn.server.research_egress import extract_gateway_hosts
    op = tmp_path / "op"; op.mkdir()
    (op / ".claude.json").write_text('{"apiBaseUrl":"https://gw.example","primaryApiKey":"sk-supersecret"}', encoding="utf-8")
    (op / ".claude").mkdir();
    (op / ".claude" / "settings.json").write_text('{"baseURL":"https://api.example/v1"}', encoding="utf-8")
    hosts = extract_gateway_hosts(op)
    assert "gw.example" in hosts
    assert "api.example" in hosts
    assert not any("sk-" in h for h in hosts)  # never leaks the api key as a host
    assert extract_gateway_hosts(tmp_path / "missing") == []


def test_proxy_target_requests_tracked(fixture):
    host, port = fixture
    proxy = EgressProxy(allow=[f"http://{host}:{port}"], request_quota=3)
    pp = proxy.start()
    try:
        _proxied_get(pp, host, port)
        _proxied_get(pp, host, port)
        assert proxy.quota.requests == 2
        assert proxy.stats["requests"] == 2
    finally:
        proxy.stop()

def test_ld_preload_forces_direct_connect_through_proxy(tmp_path):
    """#5 bypass-hardening: a direct socket.connect() to a NON-loopback host must be
    forced through the enforcement proxy so it cannot reach the target directly, while
    loopback stays usable. Proven with the proxy target unreachable: the direct connect
    FAILS (ConnectionRefusedError) instead of reaching the host; and a loopback fixture
    still connects. Skipped if no C compiler is available."""
    import os
    import subprocess as sp
    from cairn.server.research_egress import build_egress_preload
    so = build_egress_preload(tmp_path)
    if so is None:
        pytest.skip("no C compiler available to build the egress interceptor")
    srv, host, port = _start_fixture()
    try:
        dead = _grab_free_port()
        # (A) direct connect to a NON-loopback host is forced through the proxy:
        # with the proxy unreachable the connect cannot reach the host and fails.
        code = (
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('10.255.255.1', 80), timeout=3)\n"
            "    print('LEAK')\n"
            "except Exception as e:\n"
            "    print(type(e).__name__)\n"
        )
        env = dict(os.environ); env["LD_PRELOAD"] = so
        env["CAIRN_EGRESS_PROXY"] = f"127.0.0.1:{dead}"
        r = sp.run([sys.executable, "-c", code], capture_output=True, text=True,
                   env=env, timeout=20)
        assert "LEAK" not in r.stdout, (r.stdout, r.stderr)
        assert "ConnectionRefusedError" in r.stdout, (r.stdout, r.stderr)
        # (B) loopback is NOT intercepted, so the proxy itself and local fixtures
        # remain reachable even with the interceptor loaded (no ConnectionRefused).
        env2 = dict(os.environ); env2["LD_PRELOAD"] = so
        env2["CAIRN_EGRESS_PROXY"] = f"127.0.0.1:{dead}"
        code2 = (
            "import socket\n"
            f"s=socket.create_connection(('127.0.0.1', {port}), timeout=3)\n"
            "print('LOOPBACK_OK')\n"
        )
        r2 = sp.run([sys.executable, "-c", code2], capture_output=True, text=True,
                    env=env2, timeout=20)
        assert "LOOPBACK_OK" in r2.stdout, (r2.stdout, r2.stderr)
    finally:
        srv.shutdown(); srv.server_close()


def _grab_free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p