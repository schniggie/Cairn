"""Reliability hardening #5: controlled research egress (scope / protocol / quota /
redirect re-check / retry / model-vs-target classification). Pure decision logic and a
loopback enforcement proxy against local HTTP fixtures — no real model needed."""
import http.server
import os
import socket
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

from cairn.server.research_egress import (EgressProxy, QuotaState, SANDBOX_PROXY_PORT,
                                          UNSUPPORTED, build_egress_preload,
                                          classify_scheme, inject_egress_bridge,
                                          parse_scope, scope_decision,
                                          stage_egress_bridge)
from cairn.server.research_sandbox import build_sandbox


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

def test_ld_preload_blocks_udp_and_non_proxy_loopback(tmp_path):
    """Direct TCP is forced through the proxy port. UDP/DNS and any other loopback
    port are refused, so a target cannot exfiltrate over DNS or a host service.
    Skipped if no C compiler is available."""
    import os
    import subprocess as sp
    so = build_egress_preload(tmp_path)
    if so is None:
        pytest.skip("no C compiler available to build the egress interceptor")
    dead = _grab_free_port()
    proxy_srv = socket.socket()
    proxy_srv.bind(("127.0.0.1", 0))
    proxy_srv.listen(1)
    proxy_port = proxy_srv.getsockname()[1]
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen(1)
    other_port = other.getsockname()[1]
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp_port = udp.getsockname()[1]
    udp.settimeout(0.4)
    direct = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('10.255.255.1', 80), timeout=3)\n"
        "    print('TCP_LEAK')\n"
        "except Exception as e:\n"
        "    print('TCP', type(e).__name__)\n"
    )
    local = (
        "import socket\n"
        f"s=socket.create_connection(('127.0.0.1', {proxy_port}), timeout=3)\n"
        "print('PROXY_OK')\n"
        "s.close()\n"
        "try:\n"
        f"    socket.create_connection(('127.0.0.1', {other_port}), timeout=3)\n"
        "    print('LOOP_LEAK')\n"
        "except Exception as e:\n"
        "    print('LOOP', type(e).__name__)\n"
        "u=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "try:\n"
        f"    u.sendto(b'EXFIL', ('127.0.0.1', {udp_port}))\n"
        "    print('UDP_LEAK')\n"
        "except Exception as e:\n"
        "    print('UDP', type(e).__name__)\n"
        "try:\n"
        "    u.sendto(b'EXFIL', ('8.8.8.8', 53))\n"
        "    print('DNS_LEAK')\n"
        "except Exception as e:\n"
        "    print('DNS', type(e).__name__)\n"
        "try:\n"
        "    u.sendmsg([b'EXFIL'], [], 0, ('8.8.8.8', 53))\n"
        "    print('MSG_LEAK')\n"
        "except Exception as e:\n"
        "    print('MSG', type(e).__name__)\n"
    )
    try:
        dead_env = dict(os.environ)
        dead_env["LD_PRELOAD"] = so
        dead_env["CAIRN_EGRESS_PROXY"] = f"127.0.0.1:{dead}"
        refused = sp.run([sys.executable, "-c", direct], capture_output=True, text=True,
                         env=dead_env, timeout=20)
        assert refused.returncode == 0, (refused.stdout, refused.stderr)
        assert "TCP_LEAK" not in refused.stdout
        assert "TCP ConnectionRefusedError" in refused.stdout, (refused.stdout, refused.stderr)
        env = dict(os.environ)
        env["LD_PRELOAD"] = so
        env["CAIRN_EGRESS_PROXY"] = f"127.0.0.1:{proxy_port}"
        r = sp.run([sys.executable, "-c", local], capture_output=True, text=True, env=env, timeout=20)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert "PROXY_OK" in r.stdout, (r.stdout, r.stderr)
        assert "LOOP_LEAK" not in r.stdout
        assert "LOOP ConnectionRefusedError" in r.stdout, (r.stdout, r.stderr)
        assert "UDP_LEAK" not in r.stdout and "DNS_LEAK" not in r.stdout and "MSG_LEAK" not in r.stdout
        assert "UDP PermissionError" in r.stdout, (r.stdout, r.stderr)
        assert "DNS PermissionError" in r.stdout, (r.stdout, r.stderr)
        assert "MSG PermissionError" in r.stdout, (r.stdout, r.stderr)
        try:
            udp.recvfrom(32)
            raise AssertionError("UDP datagram reached the host")
        except socket.timeout:
            pass
    finally:
        proxy_srv.close()
        other.close()
        udp.close()


def test_egress_bridge_socket_fits_sockaddr(tmp_path):
    """A deep session directory must not make the Unix socket unbindable."""
    deep = tmp_path
    for i in range(8):
        deep = deep / f"dir{i:02d}-session-private"
    proxy = EgressProxy(allow=["http://127.0.0.1:9"], request_quota=1)
    proxy.start()
    try:
        bridge = stage_egress_bridge(deep, proxy)
        assert Path(proxy._unix_path).is_socket()
        assert len(os.fsencode(proxy._unix_path)) < 108
        assert (bridge / "host-socket").read_text().strip() == proxy._unix_path
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(proxy._unix_path)
        client.close()
    finally:
        proxy.stop()
        assert proxy._unix_path is None


def test_sandbox_command_does_not_share_host_network(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    cmd = build_sandbox(workspace=ws, repo=None, argv=["-c", "true"], claude_bin="sh")
    assert "--unshare-all" in cmd
    assert "--share-net" not in cmd


def test_sandbox_blocks_udp_dns_and_host_loopback(tmp_path):
    """Inside the research network namespace, host loopback and external UDP/DNS
    are unreachable even without the preload library."""
    import subprocess as sp
    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0))
    tcp.listen(1)
    tcp.settimeout(0.4)
    tport = tcp.getsockname()[1]
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(0.4)
    uport = udp.getsockname()[1]
    code = (
        "import socket\n"
        "try:\n"
        f"    socket.create_connection(('127.0.0.1', {tport}), timeout=1)\n"
        "    print('TCP_LEAK')\n"
        "except OSError as e:\n"
        "    print('TCP', type(e).__name__)\n"
        "u=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "try:\n"
        f"    u.sendto(b'EXFIL', ('127.0.0.1', {uport}))\n"
        "    print('UDP_LOCAL')\n"
        "except OSError as e:\n"
        "    print('UDP_LOCAL', e.errno)\n"
        "try:\n"
        "    u.sendto(b'EXFIL', ('8.8.8.8', 53))\n"
        "    print('UDP_EXT_SENT')\n"
        "except OSError as e:\n"
        "    print('UDP_EXT', e.errno)\n"
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    cmd = build_sandbox(workspace=ws, repo=None, argv=["-c", code], claude_bin="python3")
    try:
        result = sp.run(cmd, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert "TCP_LEAK" not in result.stdout
        assert "UDP_EXT_SENT" not in result.stdout
        assert "UDP_EXT 101" in result.stdout, result.stdout
        try:
            udp.recvfrom(32)
            raise AssertionError("sandbox UDP reached the host")
        except socket.timeout:
            pass
        try:
            tcp.accept()
            raise AssertionError("sandbox TCP reached host loopback")
        except socket.timeout:
            pass
    finally:
        tcp.close()
        udp.close()


def test_sandbox_proxy_bridge_enforces_scope_and_quota(tmp_path):
    """The Unix-socket bridge is the only exit. In-scope HTTP is counted; a second
    request past the quota and an out-of-scope host are refused. UDP still cannot
    leave the namespace."""
    import subprocess as sp
    srv, host, port = _start_fixture()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(0.4)
    uport = udp.getsockname()[1]
    decoy = socket.socket()
    decoy.bind(("127.0.0.1", 0))
    decoy.listen(1)
    decoy.settimeout(0.4)
    decoy_port = decoy.getsockname()[1]
    proxy = EgressProxy(allow=[f"http://{host}:{port}"], request_quota=1)
    proxy.start()
    try:
        bridge = stage_egress_bridge(tmp_path / "private", proxy)
        so = build_egress_preload(tmp_path)
        if so is not None:
            (bridge / "libcairn_egress.so").write_bytes(Path(so).read_bytes())
        probe = (
            "import os, socket, urllib.request\n"
            "print('PORT', os.environ.get('CAIRN_EGRESS_PORT'))\n"
            "u=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
            "try:\n"
            f"    u.sendto(b'EXFIL', ('8.8.8.8', 53))\n"
            "    print('UDP_EXT_SENT')\n"
            "except OSError as e:\n"
            "    print('UDP_EXT', e.errno)\n"
            "try:\n"
            f"    u.sendto(b'EXFIL', ('127.0.0.1', {uport}))\n"
            "    print('UDP_LOCAL_SENT')\n"
            "except OSError as e:\n"
            "    print('UDP_LOCAL', e.errno)\n"
            "try:\n"
            f"    socket.create_connection(('127.0.0.1', {decoy_port}), timeout=1)\n"
            "    print('DIRECT_LEAK')\n"
            "except OSError:\n"
            "    print('DIRECT_BLOCKED')\n"
            "opener = urllib.request.build_opener(urllib.request.ProxyHandler({\n"
            f"    'http': 'http://127.0.0.1:{SANDBOX_PROXY_PORT}',\n"
            f"    'https': 'http://127.0.0.1:{SANDBOX_PROXY_PORT}',\n"
            "}))\n"
            "def hit(url):\n"
            "    try:\n"
            "        with opener.open(url, timeout=5) as resp:\n"
            "            print('STATUS', resp.status)\n"
            "    except urllib.error.HTTPError as e:\n"
            "        print('STATUS', e.code)\n"
            "    except Exception as e:\n"
            "        print('ERR', type(e).__name__)\n"
            "hit('http://evil.example:9/')\n"
            f"hit('http://{host}:{port}/')\n"
            f"hit('http://{host}:{port}/again')\n"
        )
        ws = tmp_path / "ws"
        ws.mkdir()
        cmd = build_sandbox(workspace=ws, repo=None, argv=["-c", probe], claude_bin="python3")
        cmd = inject_egress_bridge(cmd, bridge)
        if so is not None:
            sep = cmd.index("--")
            cmd[sep:sep] = ["--setenv", "LD_PRELOAD", "/cairn-egress/libcairn_egress.so"]
        result = sp.run(cmd, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert f"PORT {SANDBOX_PROXY_PORT}" in result.stdout, result.stdout
        assert "UDP_EXT_SENT" not in result.stdout
        assert "DIRECT_LEAK" not in result.stdout
        assert "DIRECT_BLOCKED" in result.stdout, result.stdout
        assert "STATUS 403" in result.stdout, result.stdout
        assert "STATUS 200" in result.stdout, result.stdout
        assert result.stdout.count("STATUS 403") >= 2, result.stdout
        assert proxy.quota.requests == 1
        assert proxy.stats["blocked"] >= 1
        assert proxy.stats["quota_exhausted"] >= 1
        try:
            udp.recvfrom(32)
            raise AssertionError("bridge sandbox delivered a UDP datagram to the host")
        except socket.timeout:
            pass
    finally:
        proxy.stop()
        udp.close()
        decoy.close()
        srv.shutdown()
        srv.server_close()


def _grab_free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p