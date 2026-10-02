import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairn.server import db, research_services as service
from cairn.server.research_sandbox import build_sandbox
from cairn.server.research_worker import ResearchWorker
from cairn.server.routers.research import router
from cairn.server.routers.research_runtime_status import router as runtime_router
from cairn.server.routers.projects import router as projects_router


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "research.db")
    app = FastAPI()
    app.include_router(router)
    app.include_router(runtime_router)
    app.include_router(projects_router)
    with TestClient(app) as app_client:
        yield app_client


def create(client, **kwargs):
    body = {
        "title": "Worker test research",
        "objective": "Review authorized target",
        "url": "http://127.0.0.1:4100",
        "authorization_confirmed": True,
        "budget": {
            "minutes": 45,
            "requests": 50,
            "max_cost_usd": 0.10,
            "max_steps": 10,
        },
    }
    body.update(kwargs)
    response = client.post("/api/research/sessions", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_runtime_reflects_live_worker(client):
    if shutil.which("claude") is None:
        pytest.skip("research runtime availability requires the claude binary")
    with db.get_conn() as conn:
        assert service.read_worker_runtime(conn) is None
    runtime = client.get("/api/research/runtime").json()
    assert runtime["available"] is False
    with db.get_conn() as conn:
        service.worker_heartbeat(conn, "worker-1", "test", state="idle")
    runtime = client.get("/api/research/runtime").json()
    assert runtime["available"] is True
    assert runtime["worker"]["state"] == "idle"
    with db.get_conn() as conn:
        service.worker_heartbeat(conn, "worker-1", "test", state="working", current_session_id="s-1")
    runtime = client.get("/api/research/runtime").json()
    assert runtime["worker"]["state"] == "working"
    assert runtime["worker"]["current_session_id"] == "s-1"


def _make_fake_claude(tmp_path, stdout_text: str):
    """Install a fake ``claude`` that cats an outer-envelope JSON file, matching the
    real CLI's shape (result + total_cost_usd) without any shell-quoting hazards."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    envelope = fake_bin / "envelope.json"
    envelope.write_text(stdout_text, encoding="utf-8")
    claude = fake_bin / "claude"
    claude.write_text(
        "#!/bin/sh\n"
        f"cat {envelope}\n",
        encoding="utf-8",
    )
    claude.chmod(0o755)
    return fake_bin


def _outer_envelope(research_payload: dict, cost_usd: float) -> str:
    return json.dumps(
        {
            "is_error": False,
            "result": json.dumps(research_payload),
            "total_cost_usd": cost_usd,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
    )


def test_worker_end_to_end_with_outer_envelope(tmp_path, monkeypatch):
    """Fix #1 + #2: the worker must parse the REAL outer claude envelope
    (result/total_cost_usd), not assume a bare research object, and must write
    back the true provider cost/elapsed rather than leave budget untouched."""
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI()
    app.include_router(router)
    app.include_router(projects_router)
    with TestClient(app) as app_client:
        session = create(app_client, title="Envelope e2e")
        sid = session["id"]

        research_payload = {
            "summary": "observed request/response under budget",
            "evidence": [
                {"kind": "http", "title": "req A", "content": "base vs sample"},
                {"kind": "source", "title": "code line", "content": "check() at x.py:12", "metadata": {}},
            ],
            "findings": [
                {"title": "no confirmed vuln", "description": "sample in scope returned expected",
                 "status": "pending"}
            ],
            "next_direction": "continue review", "phase": 1, "terminal": True, "awaiting_input": False,
        }
        fake_bin = _make_fake_claude(
            tmp_path, _outer_envelope(research_payload, cost_usd=0.0042)
        )

        result = _spawn_worker(research_db, tmp_path / "ws", fake_bin)
        assert result.returncode == 0, result.stderr

        with db.get_conn() as conn:
            detail = service.get_session(conn, sid)
        assert detail["status"] == "completed"  # terminal=true
        # Two evidence entries: one http, one source.
        http_evidence = [e for e in detail["evidence"] if e["kind"] == "http"]
        assert len(http_evidence) == 1
        assert len(detail["findings"]) == 1
        # Budget write-back is truthful: cost reflects what claude reported and
        # request count counts at least the http evidence recorded.
        assert detail["usage"]["cost_usd"] >= 0.0042, detail["usage"]
        # request count counts the pre-start reservation plus each recorded http
        # evidence of an actual target request.
        assert detail["usage"]["requests"] >= 1, detail["usage"]
        assert detail["usage"]["steps"] == 1
        with db.get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM research_reports WHERE session_id=?", (detail["id"],)).fetchone()
            assert row[0] == 1, "report should be created after terminal run"


def _make_code_audit_claude(tmp_path):
    """Install a fake ``claude`` that actually READS the authorized /repo inside the
    sandbox (proving the RO mount + content visibility for white-box mode) and emits an
    outer envelope whose source evidence carries the real file:line it discovered."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    claude = fake_bin / "claude"
    claude.write_text(
        '#!/usr/bin/env python3\n'
        'import json, pathlib\n'
        'p = pathlib.Path("/repo/app.py")\n'
        'target = "def admin_resource"\n'
        'line = 0; saw = False\n'
        'if p.is_file():\n'
        '    saw = True\n'
        '    try:\n'
        '        for i, ln in enumerate(p.read_text().splitlines(), 1):\n'
        '            if target in ln:\n'
        '                line = i; break\n'
        '    except Exception:\n'
        '        pass\n'
        'c = ("app.py:%d %s" % (line, target)) if line else "app.py:? not found"\n'
        'payload = {\n'
        '  "summary": "白盒审计：从 /repo 识读到入口 admin_resource，评估权限边界。",\n'
        '  "evidence": [{"kind":"source","title":"入口函数定义","content": c, '
        '    "metadata":{"file":"app.py","line":line,"saw_repo":saw}}],\n'
        '  "findings": [{"title":"admin_resource 缺少调用者角色校验",\n'
        '    "description": ("app.py:%d 入口未校验调用者角色即触及资源" % line),"status":"pending"}],\n'
        '  "next_direction":"检查鉴权中间件与调用链","phase":1,"terminal":True,"awaiting_input":False\n'
        '}\n'
        'out = {"is_error":False,"result":json.dumps(payload, ensure_ascii=False),'
        ' "total_cost_usd":0.002,"usage":{"input_tokens":10,"output_tokens":5}}\n'
        'print(json.dumps(out, ensure_ascii=False))\n',
        encoding="utf-8",
    )
    claude.chmod(0o755)
    return fake_bin


def test_worker_code_audit_end_to_end(tmp_path, monkeypatch):
    """M2 white-box slice: a repo-only (code) research session runs through the worker,
    the sandbox RO-binds /repo so the step can actually read the authorized code, the
    source evidence carries a real file:line, and an audit report is committed. No
    real model gateway involved (deterministic, fixture-based). Code-only runs need no
    egress proxy, so no target HTTP is counted beyond the gate reservation."""
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI()
    app.include_router(router)
    app.include_router(projects_router)
    proj = tmp_path / "proj"; proj.mkdir()
    # entry endpoint + a permission gap an auditor would cite by file:line (line 4)
    (proj / "app.py").write_text("import os\n\n@route('/admin')\ndef admin_resource(req):\n    return fetch(req)\n", encoding="utf-8")
    (proj / "README.md").write_text("# demo audit target\n", encoding="utf-8")
    with TestClient(app) as app_client:
        session = create(app_client, url=None, repo=str(proj), title="Whitebox audit e2e",
                         objective="审计 admin_resource 的权限边界")
        sid = session["id"]
        assert session["mode"] == "code"
        fake_bin = _make_code_audit_claude(tmp_path)
        result = _spawn_worker(research_db, tmp_path / "ws", fake_bin)
        assert result.returncode == 0, result.stderr
        with db.get_conn() as conn:
            detail = service.get_session(conn, sid)
    assert detail["status"] == "completed"
    src = [e for e in detail["evidence"] if e["kind"] == "source"]
    assert len(src) == 1, detail["evidence"]
    # the source evidence reflects what the step actually read from the RO-mounted /repo
    assert src[0]["metadata"].get("saw_repo") is True
    assert "app.py:4" in src[0]["content"], src[0]["content"]
    assert len(detail["findings"]) == 1
    # audit report is committed and carries the repo + the code-position source evidence
    with db.get_conn() as conn:
        row = conn.execute("SELECT markdown FROM research_reports WHERE session_id=? ORDER BY created_at DESC", (sid,)).fetchone()
    assert row is not None
    md = row["markdown"]
    assert str(proj) in md and "admin_resource" in md and "app.py:4" in md, md
    # code-only: no target HTTP requests counted beyond the pre-start gate reservation
    assert detail["usage"]["requests"] == 1
    assert detail["usage"]["steps"] == 1


def _make_combined_claude(tmp_path, port):
    """Install a fake ``claude`` for a COMBINED (url+repo) run: it reads /repo to locate
    the route (source evidence), then makes a REAL HTTP request to the running
    environment (http/request evidence + baseline), and links both to one finding. This
    proves the M3 code<->route<->request mapping loop deterministically."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    claude = fake_bin / "claude"
    script = (
        '#!/usr/bin/env python3\n'
        'import json, pathlib, subprocess, os\n'
        'p = pathlib.Path("/repo/main.py")\n'
        'line = 0\n'
        'if p.is_file():\n'
        '    for i, ln in enumerate(p.read_text().splitlines(), 1):\n'
        '        if "def get_data" in ln:\n'
        '            line = i; break\n'
        'resp = ""\n'
        'try:\n'
        '    out = subprocess.check_output(["curl", "-s", "-m", "8", '
        '       "http://127.0.0.1:__PORT__/data?id=1"], env=dict(os.environ), timeout=10)\n'
        '    resp = out.decode("utf-8", "replace").strip()\n'
        'except Exception as e:\n'
        '    resp = "ERR:" + str(e)\n'
        'payload = {\n'
        '  "summary": "联动：从 /repo 定位 get_data 路由，并动态请求运行环境验证。",\n'
        '  "evidence": [\n'
        '    {"kind": "source", "title": "路由实现", '
        '      "content": "main.py:%d def get_data -> /data" % line, '
        '      "metadata": {"file": "main.py", "line": line}},\n'
        '    {"kind": "http", "title": "动态基线请求", '
        '      "content": "GET /data?id=1 -> %s" % resp, '
        '      "metadata": {"url": "http://127.0.0.1:__PORT__/data?id=1"}}\n'
        '  ],\n'
        '  "findings": [{"title": "get_data 未校验调用者身份", '
        '    "description": "main.py:%d 入口直接返回资源未校验身份；动态请求 /data?id=1 得到基线响应。" % line, '
        '    "status": "pending"}],\n'
        '  "next_direction": "对照修复版本判定", "phase": 1, "terminal": True, "awaiting_input": False\n'
        '}\n'
        'out = {"is_error": False, "result": json.dumps(payload, ensure_ascii=False), '
        ' "total_cost_usd": 0.003, "usage": {"input_tokens": 10, "output_tokens": 5}}\n'
        'print(json.dumps(out, ensure_ascii=False))\n'
    ).replace("__PORT__", str(port))
    claude.write_text(script, encoding="utf-8")
    claude.chmod(0o755)
    return fake_bin


def test_worker_combined_audit_maps_code_to_request(tmp_path, monkeypatch):
    """M3 combined slice: a session with BOTH a running environment URL and a code repo
    runs through the worker; the sandbox gets /repo (RO) and the step both reads the
    code to locate the route AND makes a real HTTP request to the running environment;
    the committed evidence carries the code<->route<->request mapping and a linked
    finding, and the report contains both code and request evidence."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _H(BaseHTTPRequestHandler):
        # minimal running-environment fixture on the host loopback
        def do_GET(self):
            if self.path.startswith("/data"):
                body = b'{"id":"1","owner":"admin"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404); self.end_headers()
        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    try:
        monkeypatch.setattr(db, "_db_path", None)
        research_db = tmp_path / "research.db"
        db.configure(research_db)
        app = FastAPI()
        app.include_router(router)
        app.include_router(projects_router)
        proj = tmp_path / "proj"; proj.mkdir()
        (proj / "main.py").write_text("import os\n\n@app_route(\"/data\")\ndef get_data(req):\n    return \"resource:\" + str(req.get('id'))\n", encoding="utf-8")
        target = f"http://127.0.0.1:{port}/"
        with TestClient(app) as app_client:
            session = create(app_client, url=target, repo=str(proj), title="Combined audit e2e",
                             objective="联动验证 get_data 的权限")
            sid = session["id"]
            assert session["mode"] == "combined"
            fake_bin = _make_combined_claude(tmp_path, port)
            result = _spawn_worker(research_db, tmp_path / "ws", fake_bin)
            assert result.returncode == 0, result.stderr
            with db.get_conn() as conn:
                detail = service.get_session(conn, sid)
        assert detail["status"] == "completed", detail.get("latest_error")
        kinds = {e["kind"] for e in detail["evidence"]}
        assert kinds >= {"source", "http"}, detail["evidence"]
        src = next(e for e in detail["evidence"] if e["kind"] == "source")
        req = next(e for e in detail["evidence"] if e["kind"] == "http")
        # code<->request mapping: source names the route line, request is the baseline
        assert "main.py:4" in src["content"], src
        assert "/data?id=1" in req["content"], req
        assert "owner" in req["content"], req  # the real dynamic response the step fetched
        assert len(detail["findings"]) == 1
        # the dynamic request is accounted (gate reservation + the http baseline evidence)
        assert detail["usage"]["requests"] >= 2, detail["usage"]
        with db.get_conn() as conn:
            row = conn.execute("SELECT markdown FROM research_reports WHERE session_id=? ORDER BY created_at DESC", (sid,)).fetchone()
        assert row is not None
        md = row["markdown"]
        assert "main.py:4" in md and "/data?id=1" in md and "owner" in md, md
    finally:
        srv.shutdown()


def _spawn_worker(research_db, workspace_root, fake_bin):
    src_root = Path(__file__).parent.parent / "src"
    code = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "from cairn.server.research_worker import ResearchWorker\n"
        "import os\n"
        "os.environ['CAIRN_CLAUDE_BIN']=%r\n"
        "w=ResearchWorker(db_path=%r, workspace_root=%r, interval_seconds=2, worker_id='test-worker')\n"
        "w.run(once=True)\n"
    ) % (
        str(src_root),
        str(fake_bin / "claude"),
        str(research_db),
        str(workspace_root),
    )
    env = dict(os.environ)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env,
        cwd=str(Path(__file__).parent.parent.parent), timeout=120,
    )


def test_budget_writes_back_true_cost_when_at_limit(tmp_path, monkeypatch):
    """When claude returns a cost higher than the budget (real world: budget is a
    soft ceiling), the worker must record the TRUE cost, not silently drop it and
    then let a later resume pretend budget is still full."""
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as app_client:
        session = create(
            app_client,
            budget={"minutes": 45, "requests": 50, "max_cost_usd": 0.01, "max_steps": 10},
            title="over-budget truth",
        )
        sid = session["id"]
        payload = {
            "summary": "spent more than approved, must be recorded",
            "evidence": [], "findings": [],
            "next_direction": "", "phase": 1, "terminal": True, "awaiting_input": False,
        }
        fake_bin = _make_fake_claude(tmp_path, _outer_envelope(payload, cost_usd=0.05))
        result = _spawn_worker(research_db, tmp_path / "ws", fake_bin)
        assert result.returncode == 0, result.stderr
        with db.get_conn() as conn:
            detail = service.get_session(conn, sid)
        assert detail["status"] in ("completed", "failed")
        assert detail["usage"]["cost_usd"] >= 0.05, detail["usage"]


def _make_sandbox_sleeper(tmp_path, workspace_host: Path):
    """A fake claude that runs a long-lived script visible to the test: it appends
    a timestamp to the host-visible workspace every 0.3s. Located inside ``bin`` so
    it is reachable in the bwrap sandbox, and writes to /workspace which is bound
    back to the host path the test can watch."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    sleeper = fake_bin / "sleeper.py"
    sleeper.write_text(
        "import time\n"
        "while True:\n"
        "    open('/workspace/beat.txt','a').write(str(time.time())+'\\n')\n"
        "    time.sleep(0.3)\n",
        encoding="utf-8",
    )
    claude = fake_bin / "claude"
    claude.write_text(
        "#!/bin/sh\nexec /usr/bin/python3 %s\n" % (sleeper),
        encoding="utf-8",
    )
    claude.chmod(0o755)
    return fake_bin


def _beat_age(workspace_host: Path) -> float | None:
    import os as _os
    beat = workspace_host / "beat.txt"
    if not beat.exists():
        return None
    return time.time() - beat.stat().st_mtime


def test_pause_stops_live_subprocess(tmp_path, monkeypatch):
    """Fix #4 (pause arm): a real long-running subprocess is terminated when the
    user requests pause — the worker stops the subprocess group, not just writes
    a dB flag. Verified by the sandboxed process stopping its workspace heartbeat."""
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as app_client:
        session = create(app_client, title="Pause real subprocess")
        sid = session["id"]
        workspace_host = tmp_path / "ws" / sid
        fake_bin = _make_sandbox_sleeper(tmp_path, workspace_host)
        monkeypatch.setenv("CAIRN_CLAUDE_BIN", str(fake_bin / "claude"))
        monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))

        with db.get_conn() as conn:
            service.claim_session(conn, "w-pause", lease_seconds=60)
        worker = ResearchWorker(
            db_path=research_db, workspace_root=tmp_path / "ws", worker_id="w-pause",
            lease_seconds=60, interval_seconds=1,
        )

        error = {}
        def target():
            try:
                worker.run_session(sid)
            except Exception as exc:  # pragma: no cover
                error["exc"] = repr(exc)
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        deadline = time.time() + 40
        while time.time() < deadline and _beat_age(workspace_host) is None:
            time.sleep(0.2)
        assert _beat_age(workspace_host) is not None, "sleeper never started"

        before = _beat_age(workspace_host)
        with db.get_conn() as conn:
            conn.execute("UPDATE research_sessions SET status='pause_requested' WHERE id=?", (sid,))
            conn.commit()

        thread.join(timeout=45)
        assert not thread.is_alive(), "worker thread did not return after pause"
        if error:
            raise AssertionError(error["exc"])
        with db.get_conn() as conn:
            status = service.get_session(conn, sid)["status"]
        assert status == "paused", status
        # Heartbeat must stop: the sandboxed subprocess is dead, not leaked.
        deadline = time.time() + 20
        while time.time() < deadline:
            age = _beat_age(workspace_host)
            if age is not None and age > 2.0:
                break
            time.sleep(0.2)
        assert _beat_age(workspace_host) is None or _beat_age(workspace_host) > 1.5, (
            "sandboxed subprocess still writing after pause"
        )


def test_lease_loss_stops_subprocess(tmp_path, monkeypatch):
    """Fix #4 (ownership arm): if the lease is lost (or the session stops being
    owned/running), the worker terminates the subprocess instead of letting a
    task it no longer owns keep running."""
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as app_client:
        session = create(app_client, title="Lease loss stop")
        sid = session["id"]
        workspace_host = tmp_path / "ws" / sid
        fake_bin = _make_sandbox_sleeper(tmp_path, workspace_host)
        monkeypatch.setenv("CAIRN_CLAUDE_BIN", str(fake_bin / "claude"))
        monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))

        with db.get_conn() as conn:
            service.claim_session(conn, "w-owner", lease_seconds=60)
        worker = ResearchWorker(
            db_path=research_db, workspace_root=tmp_path / "ws", worker_id="w-owner",
            lease_seconds=60, interval_seconds=1,
        )

        error = {}
        def target():
            try:
                worker.run_session(sid)
            except Exception as exc:  # pragma: no cover
                error["exc"] = repr(exc)
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        deadline = time.time() + 40
        while time.time() < deadline and _beat_age(workspace_host) is None:
            time.sleep(0.2)
        assert _beat_age(workspace_host) is not None, "sleeper never started"

        # Steal the lease: another worker takes ownership now.
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE research_sessions SET lease_owner='other-worker', lease_expires_at=datetime('now','+60 seconds') WHERE id=?",
                (sid,),
            )
            conn.commit()

        thread.join(timeout=45)
        assert not thread.is_alive(), "worker thread did not return after lease loss"
        if error:
            raise AssertionError(error["exc"])
        with db.get_conn() as conn:
            status = service.get_session(conn, sid)["status"]
        # The worker that lost ownership must NOT claim the result (finish_run is
        # owner-guarded), so never completed by a non-owner.
        assert status != "completed", status
        deadline = time.time() + 20
        while time.time() < deadline:
            age = _beat_age(workspace_host)
            if age is not None and age > 2.0:
                break
            time.sleep(0.2)
        assert _beat_age(workspace_host) is None or _beat_age(workspace_host) > 1.5, (
            "sandboxed subprocess leaked after lease loss"
        )

def _run_once(research_db, workspace_root, fake_bin):
    """Spawn a worker subprocess running one tick (real bwrap + subprocess)."""
    src_root = Path(__file__).parent.parent / "src"
    code = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "from cairn.server.research_worker import ResearchWorker\n"
        "from cairn.server import db\n"
        "from pathlib import Path\n"
        "import os\n"
        "os.environ['CAIRN_CLAUDE_BIN']=%r\n"
        "db.configure(Path(%r))\n"
        "logging_basic=1\n"
        "w=ResearchWorker(db_path=Path(%r), workspace_root=Path(%r), worker_id='tw')\n"
        "w.tick()\n"
    ) % (str(src_root), str(fake_bin / "claude"), str(research_db), str(research_db), str(workspace_root))
    env = dict(os.environ)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["CAIRN_CLAUDE_BIN"] = str(fake_bin / "claude")
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env,
        cwd=str(Path(__file__).parent.parent.parent), timeout=120,
    )


def _sandbox_probe(argv_probe):
    """Run a shell probe inside the bwrap sandbox (no model) and return stdout."""
    workspace = Path("/tmp/cairn-sbx-probe")
    import tempfile
    workspace = Path(tempfile.mkdtemp(prefix="cairn-sbx-"))
    cmd = build_sandbox(
        workspace=workspace, repo=None, argv=["-c", argv_probe], claude_bin="sh",
    )
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return r


def test_sandbox_home_exists_with_seeded_config(tmp_path):
    """Fix #1: the sandbox HOME must exist (inside the mounted workspace) and the
    operator's Claude settings are staged READ-ONLY at CLAUDE_CONFIG_DIR (outside the
    writable home), not copied into the writable HOME where the model could edit them."""
    import tempfile
    ws = tmp_path / "ws"; ws.mkdir()
    op_home = tmp_path / "op-home"; op_home.mkdir()
    (op_home / ".claude.json").write_text('{"primaryApiKey":"secret-k","model":"hypothetical-1"}', encoding="utf-8")
    (op_home / ".claude").mkdir()
    (op_home / ".claude" / "settings.json").write_text('{"apiKeyHelper":"op","baseURL":"https://gw.example"}', encoding="utf-8")
    cmd = build_sandbox(
        workspace=ws, repo=None,
        argv=["-c", "echo HOME=$HOME; test -d $HOME && echo HOME_OK; echo CDIR=$CLAUDE_CONFIG_DIR; grep -q hypothetical-1 $CLAUDE_CONFIG_DIR/.claude.json && echo CONFIG_OK; grep -q baseURL $CLAUDE_CONFIG_DIR/settings.json && echo SETTINGS_OK; touch $CLAUDE_CONFIG_DIR/.claude.json 2>/dev/null && echo WRITABLE || echo READONLY; echo HOME_LOCAL=$(ls $HOME/.claude.json 2>/dev/null || echo absent)"],
        claude_bin="sh", claude_config_src=op_home, config_root=tmp_path / "private",
    )
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "HOME=/workspace/.sandbox-home" in out, out
    assert "HOME_OK" in out, out
    assert "CDIR=/claude-config" in out, out
    assert "CONFIG_OK" in out and "SETTINGS_OK" in out, out
    assert "READONLY" in out, out
    assert "HOME_LOCAL=absent" in out  # config is NOT in the writable home


def test_sandbox_does_not_expose_host_home(tmp_path):
    """Fix #1 regression guard: operator's host home is never bound; only its seeded
    Claude settings are copied into the sandbox home."""
    ws = tmp_path / "ws"; ws.mkdir()
    cmd = build_sandbox(
        workspace=ws, repo=None, claude_bin="sh",
        argv=["-c", "echo HOME=$HOME; ls /home 2>&1 | head -1"],
    )
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    # /home is not mounted: listing it inside the sandbox is empty (no host user dirs).
    assert "/home/kali" not in r.stdout, r.stdout


def test_no_start_when_request_budget_exhausted(tmp_path, monkeypatch):
    """Fix #2: when the request budget is exhausted (no remaining requests), the worker
    refuses to launch another research step instead of running anyway."""
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as app_client:
        session = create(app_client, budget={"minutes": 45, "requests": 1, "max_cost_usd": 0.10, "max_steps": 10})
        sid = session["id"]
        with db.get_conn() as conn:
            # Marked running + owned by the worker so it proceeds past claim.
            service.claim_session(conn, "w-req", lease_seconds=60)
            # Fully consume the single request so no request budget remains.
            db_usage = json.loads(conn.execute("SELECT usage_json FROM research_sessions WHERE id=?", (sid,)).fetchone()[0])
            db_usage["requests"] = 1
            conn.execute("UPDATE research_sessions SET usage_json=? WHERE id=?", (json.dumps(db_usage), sid))
            conn.commit()
        sleeper = tmp_path / "bin"; sleeper.mkdir()
        claude = sleeper / "claude"
        claude.write_text("#!/bin/sh\ncat "+str(tmp_path/"never.txt")+" 2>/dev/null || echo nofile\n", encoding="utf-8")
        claude.chmod(0o755)
        monkeypatch.setenv("CAIRN_CLAUDE_BIN", str(claude))
        monkeypatch.setenv("PATH", str(sleeper) + os.pathsep + os.environ.get("PATH", ""))
        w = ResearchWorker(db_path=research_db, workspace_root=tmp_path / "ws", worker_id="w-req")
        w.run_session(sid)
        with db.get_conn() as conn:
            detail = service.get_session(conn, sid)
        # With no request budget left, the run must not launch a claude subprocess
        # (the guard/reserve rejects it) and must record no evidence.
        assert detail["status"] == "failed", detail["status"]
        assert detail["evidence"] == []
        assert (detail["usage"] or {}).get("cost_usd", 0) == 0, detail["usage"]


def test_cost_recorded_on_abnormal_exit(tmp_path, monkeypatch):
    """Fix #3: even when the run fails (nonzero exit from the child), any provider
    cost reported in the outer envelope is still written back so the remaining budget
    is truthful."""
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as app_client:
        session = create(app_client)
        sid = session["id"]
        # fake claude that exits nonzero but still emits a cost-bearing envelope
        fake_bin = tmp_path / "bin"; fake_bin.mkdir()
        envelope = fake_bin / "envelope.json"
        envelope.write_text(json.dumps({"is_error": False, "total_cost_usd": 0.0099, "result": "not-needed"}), encoding="utf-8")
        claude = fake_bin / "claude"
        claude.write_text(
            "#!/bin/sh\ncat "+str(envelope)+"\nexit 3\n", encoding="utf-8")
        claude.chmod(0o755)
        monkeypatch.setenv("CAIRN_CLAUDE_BIN", str(claude))
        monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
        with db.get_conn() as conn:
            service.claim_session(conn, "w-cost", lease_seconds=60)
        w = ResearchWorker(db_path=research_db, workspace_root=tmp_path / "ws", worker_id="w-cost")
        w.run_session(sid)
        with db.get_conn() as conn:
            detail = service.get_session(conn, sid)
        assert detail["status"] == "failed", detail["status"]
        # elapsed and cost are recorded even though the run failed.
        assert detail["usage"]["elapsed_seconds"] >= 0
        assert detail["usage"]["cost_usd"] >= 0.0099, detail["usage"]


def test_stale_worker_cannot_write_results(tmp_path, monkeypatch):
    """Fix #4: if the lease moves away after the subprocess finishes but before the
    worker writes evidence, the ownership-guarded record functions refuse the writes;
    a stale worker must not claim evidence/findings into the re-claimed session."""
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as app_client:
        session = create(app_client)
        sid = session["id"]
        with db.get_conn() as conn:
            service.claim_session(conn, "w-orig", lease_seconds=60)
        w = ResearchWorker(db_path=research_db, workspace_root=tmp_path / "ws", worker_id="w-orig")
        payload = {
            "summary": "s", "findings": [],
            "evidence": [{"kind": "http", "title": "r", "content": "c"}],
            "next_direction": "", "phase": 1, "terminal": True, "awaiting_input": False,
        }
        # Move ownership to another worker before writes.
        with db.get_conn() as conn:
            conn.execute("UPDATE research_sessions SET lease_owner='other', lease_expires_at=datetime('now','+60 seconds') WHERE id=?", (sid,))
            conn.commit()
        owned, _ = w._apply_payload(sid, None, payload)
        assert owned is False, owned
        with db.get_conn() as conn:
            detail = service.get_session(conn, sid)
        assert len(detail["evidence"]) == 0, detail["evidence"]


def test_malformed_output_is_failed_not_completed(tmp_path, monkeypatch):
    """Fix #5: a run that returns unparseable / non-object output must be marked
    failed, never auto-completed with a fabricated terminal=True report."""
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as app_client:
        session = create(app_client)
        sid = session["id"]
        fake_bin = tmp_path / "bin"; fake_bin.mkdir()
        # outer envelope with a result that is NOT valid JSON (plain error text)
        envelope = fake_bin / "env2.json"
        envelope.write_text(json.dumps({"is_error": False, "result": "could not reach target: connection refused"}), encoding="utf-8")
        claude = fake_bin / "claude"
        claude.write_text("#!/bin/sh\ncat "+str(envelope)+"\n", encoding="utf-8")
        claude.chmod(0o755)
        monkeypatch.setenv("CAIRN_CLAUDE_BIN", str(claude))
        monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
        with db.get_conn() as conn:
            service.claim_session(conn, "w-mal", lease_seconds=60)
        w = ResearchWorker(db_path=research_db, workspace_root=tmp_path / "ws", worker_id="w-mal")
        w.run_session(sid)
        with db.get_conn() as conn:
            detail = service.get_session(conn, sid)
        assert detail["status"] == "failed", detail["status"]
        with db.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM research_reports WHERE session_id=?", (sid,)).fetchone()[0]
        assert n == 0, "no completion report for malformed output"

# ---------------------------------------------------------------------------
# Reliability hardening round: strict protocol, run-batch ledger, single
# transactional commit, cost-pending hold, and config-outside-workspace.
# ---------------------------------------------------------------------------


def test_validate_payload_strict_protocol():
    from cairn.server.research_services import validate_payload

    def base(**over):
        p = {"summary": "done", "evidence": [], "findings": [],
             "next_direction": "", "phase": 1, "terminal": True,
             "awaiting_input": False}
        p.update(over)
        return validate_payload(p)

    # real booleans required: JSON-string "false" is a protocol error
    ok, reason, _ = base(terminal="false")
    assert ok is False and "terminal" in reason
    ok, reason, _ = base(terminal=1)
    assert ok is False
    ok, reason, _ = base(terminal=None)
    assert ok is False
    ok, reason, _ = base(awaiting_input="false")
    assert ok is False
    # conflict (both true) is a protocol error
    ok, reason, _ = base(terminal=True, awaiting_input=True)
    assert ok is False and "冲突" in reason
    # three-state: not completed
    ok, _, norm = base(terminal=False, awaiting_input=False)
    assert ok and norm["terminal"] is False and norm["awaiting_input"] is False
    # waiting_input
    ok, _, norm = base(terminal=False, awaiting_input=True)
    assert ok and norm["awaiting_input"] is True
    # terminal + no findings requires a legitimate summary explanation
    ok, reason, _ = base(terminal=True, summary="")
    assert ok is False and "说明" in reason
    ok, _, _ = base(terminal=True, summary="request in scope completed as expected")
    assert ok is True
    # structure: evidence/findings must be lists of dicts
    ok, _, _ = base(evidence={})
    assert ok is False
    ok, _, _ = base(findings="nope")
    assert ok is False
    ok, reason, _ = base(evidence=[{"kind": "http", "title": "r", "content": "c"}],
                         findings=[{"title": 5, "description": "d"}])
    assert ok is False
    # non-object payload
    ok, _, _ = validate_payload(["not", "an", "object"])
    assert ok is False
    ok, _, _ = validate_payload(None)
    assert ok is False
    # valid with findings needs no summary
    ok, _, _ = validate_payload({"summary": "", "evidence": [], "phase": 0,
                                 "findings": [{"title": "f", "description": "d"}],
                                 "terminal": True, "awaiting_input": False})
    assert ok is True


def test_claim_opens_independent_run_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as app_client:
        session = create(app_client)
        sid = session["id"]
        with db.get_conn() as conn:
            claimed = service.claim_session(conn, "w-run", lease_seconds=60)
            rid = claimed["current_run_id"]
            assert rid is not None
            row = conn.execute(
                "SELECT worker_id,cost_status,settled FROM research_run_accounts WHERE run_id=? AND session_id=?",
                (rid, sid)).fetchone()
            assert row["worker_id"] == "w-run"
            # a run ledger is independent and starts unsettled
            assert row["settled"] == 0


def test_settle_is_idempotent_no_double_charge(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as app_client:
        session = create(app_client)
        sid = session["id"]
        with db.get_conn() as conn:
            claimed = service.claim_session(conn, "w-st", lease_seconds=60)
            rid = claimed["current_run_id"]
            service.settle_run(conn, sid, rid, "w-st", final_cost=0.02, elapsed_seconds=5)
            # repeated settlement must not double-charge
            service.settle_run(conn, sid, rid, "w-st", final_cost=0.02, elapsed_seconds=5)
            detail = service.get_session(conn, sid)
            assert detail["usage"]["cost_usd"] == 0.02
            assert detail["usage"]["elapsed_seconds"] == 5


def test_pending_cost_blocks_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as app_client:
        session = create(app_client)
        sid = session["id"]
        with db.get_conn() as conn:
            claimed = service.claim_session(conn, "w-pc", lease_seconds=60)
            rid = claimed["current_run_id"]
            # a completed step with no final cost => 费用待核对, budget not released
            service.settle_run(conn, sid, rid, "w-pc", elapsed_seconds=3, expect_final_cost=True)
            row = conn.execute(
                "SELECT cost_pending_check FROM research_sessions WHERE id=?", (sid,)
            ).fetchone()
            assert row["cost_pending_check"] == 1
            # finish the run (lease gone) so resume is meaningful
            service.finish_run(conn, sid, "w-pc", "paused")
        from fastapi import HTTPException
        with db.get_conn() as conn:
            try:
                service.resume_session(conn, sid)
            except HTTPException as exc:
                assert exc.status_code == 409
            else:
                raise AssertionError("resume should be blocked while cost pending")
        # crediting the outstanding cost clears the hold and unblocks resume
        with db.get_conn() as conn:
            service.resolve_pending_cost(conn, sid, 0.01)
            service.resume_session(conn, sid)  # must not raise
            assert service.get_session(conn, sid)["status"] == "queued"


def test_late_pause_preempts_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as app_client:
        session = create(app_client, title="pause preempt")
        sid = session["id"]
        payload = {"summary": "late result", "findings": [], "evidence": [],
                   "next_direction": "", "phase": 1, "terminal": True,
                   "awaiting_input": False}
        with db.get_conn() as conn:
            service.claim_session(conn, "w-pause2", lease_seconds=60)
            conn.execute(
                "UPDATE research_sessions SET status='pause_requested' WHERE id=?", (sid,))
            conn.commit()
        with db.get_conn() as conn:
            res = service.commit_run_results(
                conn, sid, None, "w-pause2", payload)
            assert res["ok"] is False and res["reason"] == "paused"
            n = conn.execute("SELECT COUNT(*) FROM research_reports WHERE session_id=?", (sid,)).fetchone()[0]
            assert n == 0, "a paused run must not produce a completion report"
            assert service.get_session(conn, sid)["status"] == "pause_requested"


def test_protocol_error_commits_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as app_client:
        session = create(app_client)
        sid = session["id"]
        with db.get_conn() as conn:
            service.claim_session(conn, "w-perr", lease_seconds=60)
        bad = {"terminal": "false", "awaiting_input": False, "findings": [],
               "evidence": [{"kind": "http", "title": "r", "content": "c"}],
               "summary": "x"}
        with db.get_conn() as conn:
            res = service.commit_run_results(conn, sid, None, "w-perr", bad)
            assert res["ok"] is False and res["reason"] == "protocol_error"
            detail = service.get_session(conn, sid)
            assert len(detail["evidence"]) == 0
            n = conn.execute("SELECT COUNT(*) FROM research_reports WHERE session_id=?", (sid,)).fetchone()[0]
            assert n == 0


def test_empty_results_valid_commit(tmp_path, monkeypatch):
    """A terminal run may commit with zero findings/evidence as long as it carries a
    legitimate result explanation (no-findings completion)."""
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as app_client:
        session = create(app_client)
        sid = session["id"]
        payload = {"summary": "target returned expected result; no vulnerability",
                   "findings": [], "evidence": [],
                   "next_direction": "", "phase": 3, "terminal": True,
                   "awaiting_input": False}
        with db.get_conn() as conn:
            service.claim_session(conn, "w-empty", lease_seconds=60)
            res = service.commit_run_results(conn, sid, None, "w-empty", payload)
            assert res["ok"] is True and res["status"] == "completed" and res["report_id"]
            detail = service.get_session(conn, sid)
            assert detail["status"] == "completed"
            n = conn.execute("SELECT COUNT(*) FROM research_reports WHERE session_id=?", (sid,)).fetchone()[0]
            assert n == 1


def test_old_worker_files_consumption_but_not_results(tmp_path, monkeypatch):
    """A worker that lost the lease may still file THIS batch's real consumption, but
    it must not write any research results into the re-claimed session."""
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as app_client:
        session = create(app_client)
        sid = session["id"]
        with db.get_conn() as conn:
            claimed = service.claim_session(conn, "w-old", lease_seconds=60)
            rid = claimed["current_run_id"]
            # ownership transfers to a new worker
            conn.execute("UPDATE research_sessions SET lease_owner='w-new', lease_expires_at=datetime('now','+60 seconds') WHERE id=?", (sid,))
            conn.commit()
            # old worker can still settle THIS batch (idempotent, owner not required)
            out = service.settle_run(conn, sid, rid, "w-old", final_cost=0.03, elapsed_seconds=7)
            assert out["cost_status"] == "recorded"
            detail = service.get_session(conn, sid)
            assert detail["usage"]["cost_usd"] == 0.03
            # ...but cannot write results
            pay = {"summary": "stale", "findings": [], "evidence": [{"kind": "http", "title": "r", "content": "c"}],
                   "next_direction": "", "phase": 1, "terminal": True, "awaiting_input": False}
            res = service.commit_run_results(conn, sid, rid, "w-old", pay)
            assert res["ok"] is False and res["reason"] == "ownership_lost"
            assert len(service.get_session(conn, sid)["evidence"]) == 0

def test_sandbox_config_outside_workspace_readonly(tmp_path):
    """Fix #1: the execution config is staged OUTSIDE the session workspace and mounted
    READ-ONLY, so a model can never edit the settings that govern its own run."""
    import shutil as _sh
    if _sh.which("bwrap") is None:
        pytest.skip("bwrap required")
    ws = tmp_path / "ws"; ws.mkdir()
    op_home = tmp_path / "op-home"; op_home.mkdir()
    (op_home / ".claude.json").write_text('{"model":"h-r"}', encoding="utf-8")
    cmd = build_sandbox(
        workspace=ws, repo=None, claude_bin="sh", claude_config_src=op_home,
        config_root=tmp_path / "private",
        argv=["-c",
              "echo CONFIG_DIR=$CLAUDE_CONFIG_DIR; "
              "grep -q h-r $CLAUDE_CONFIG_DIR/.claude.json && echo RO_READ_OK; "
              "echo HOME=$HOME; "
              "touch $CLAUDE_CONFIG_DIR/.claude.json 2>/dev/null && echo WRITABLE || echo READONLY; "
              "echo WS_PUB=$(ls $HOME/.claude.json 2>/dev/null || echo absent)"],
    )
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "CONFIG_DIR=/claude-config" in r.stdout, r.stdout
    assert "RO_READ_OK" in r.stdout, r.stdout
    assert "READONLY" in r.stdout, r.stdout
    assert "WS_PUB=absent" in r.stdout  # config is NOT copied into the writable sandbox home
    # the private config copy lives outside the workspace on the host
    assert (tmp_path / "private" / "claude-config" / ".claude.json").is_file()


def test_sandbox_config_rejects_symlink(tmp_path):
    """Fix #1: a symlinked config source must be refused, never silently followed."""
    import shutil as _sh
    if _sh.which("bwrap") is None:
        pytest.skip("bwrap required")
    ws = tmp_path / "ws"; ws.mkdir()
    op_home = tmp_path / "op-home"; op_home.mkdir()
    secret = tmp_path / "secret.key"; secret.write_text("hunter2", encoding="utf-8")
    if hasattr(__import__("os"), "symlink"):
        try:
            (op_home / ".claude.json").symlink_to(secret)
        except OSError:
            pytest.skip("symlink unavailable")
    with pytest.raises(RuntimeError):
        build_sandbox(workspace=ws, repo=None, claude_bin="sh",
                      claude_config_src=op_home, config_root=tmp_path / "private",
                      argv=["-c", "echo x"])


def test_sandbox_config_missing_raises(tmp_path):
    """Fix #1: no usable Claude config must fail loudly, not silently run empty."""
    import shutil as _sh
    if _sh.which("bwrap") is None:
        pytest.skip("bwrap required")
    ws = tmp_path / "ws"; ws.mkdir()
    empty_home = tmp_path / "empty-home"; empty_home.mkdir()  # no config files
    with pytest.raises(RuntimeError):
        build_sandbox(workspace=ws, repo=None, claude_bin="sh",
                      claude_config_src=empty_home, config_root=tmp_path / "private",
                      argv=["-c", "echo x"])


def test_old_batch_cannot_overwrite_new_results(tmp_path, monkeypatch):
    """M1收尾: an old run batch (worker that has since lost the lease) must not be able
    to overwrite results already committed by a NEWER batch on the same session."""
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as app_client:
        session = create(app_client, title="batch isolation")
        sid = session["id"]
        with db.get_conn() as conn:
            claimed1 = service.claim_session(conn, "w-old", lease_seconds=60)
            rid1 = claimed1["current_run_id"]
        # old worker's step ends non-terminal; it releases via pause so the session can
        # be re-queued and re-claimed as a NEW batch by a new worker.
        with db.get_conn() as conn:
            service.finish_run(conn, sid, "w-old", "paused")
            service.resume_session(conn, sid)
            claimed2 = service.claim_session(conn, "w-new", lease_seconds=60)
            rid2 = claimed2["current_run_id"]
            assert rid2 != rid1
            new_payload = {"summary": "new round done", "findings": [{"title": "new finding", "description": "d", "status": "pending"}],
                           "evidence": [], "next_direction": "", "phase": 2, "terminal": True, "awaiting_input": False}
            res2 = service.commit_run_results(conn, sid, rid2, "w-new", new_payload)
            assert res2["ok"] is True
            # stale old worker (wrong batch + no longer owner) tries to commit its old results
            old_payload = {"summary": "stale round", "findings": [{"title": "stale finding", "description": "x", "status": "pending"}],
                           "evidence": [], "next_direction": "", "phase": 1, "terminal": True, "awaiting_input": False}
            res1 = service.commit_run_results(conn, sid, rid1, "w-old", old_payload)
            assert res1["ok"] is False and res1["reason"] == "ownership_lost"
            detail = service.get_session(conn, sid)
            titles = [f["title"] for f in detail["findings"]]
            assert "new finding" in titles and "stale finding" not in titles


def test_egress_env_fails_loud_when_enforcement_unavailable(monkeypatch, tmp_path):
    """M1收尾: when outbound enforcement cannot be built (no C compiler -> no LD_PRELOAD
    interceptor), a real-target run must FAIL loudly instead of silently running with
    uncontrolled egress."""
    import cairn.server.research_egress as eg
    monkeypatch.setattr(eg, "build_egress_preload", lambda *a, **k: None)
    monkeypatch.delenv("CAIRN_CLAUDE_BIN", raising=False)
    w = ResearchWorker(db_path=tmp_path / "r.db", workspace_root=tmp_path / "ws", worker_id="w-eg")
    session = {"url": "http://target.example:80", "authorization": {}}
    with pytest.raises(RuntimeError) as ei:
        w._egress_environment(session, 10)
    assert "出站" in str(ei.value) or "拒绝" in str(ei.value)


def test_egress_env_establishes_enforcement_and_model_gateway(monkeypatch, tmp_path):
    """M1收尾: a real-target run gets a scoped egress proxy + LD_PRELOAD; the model
    gateway is classified as model traffic (allowed, not quota), the target is allowed,
    and any other host is refused (no silent allow)."""
    import cairn.server.research_egress as eg
    def _fake_build(out_dir=None):
        d = Path(out_dir) if out_dir else Path("/tmp")
        d.mkdir(parents=True, exist_ok=True)
        (d / "libcairn_egress.so").write_bytes(b"ELF")
        return str(d / "libcairn_egress.so")
    monkeypatch.setattr(eg, "build_egress_preload", _fake_build)
    monkeypatch.delenv("CAIRN_CLAUDE_BIN", raising=False)
    w = ResearchWorker(db_path=tmp_path / "r.db", workspace_root=tmp_path / "ws", worker_id="w-eg2")
    session = {"url": "http://target.example:80", "authorization": {}}
    config_root = tmp_path / "private"
    proxy, env = w._egress_environment(session, 10, config_root=config_root)
    try:
        # interceptor is compiled into the private claude-config dir and referenced by
        # its SANDBOX path (the dir is RO-bound at /claude-config) — a host /tmp path
        # would be invisible inside bwrap. LD_PRELOAD is injected INSIDE by bwrap
        # --setenv, not in the outer env (outer LD_PRELOAD breaks bwrap's user-ns helper).
        assert w._egress_preload_path == "/claude-config/libcairn_egress.so"
        assert (config_root / "claude-config-runtime" / "libcairn_egress.so").is_file()
        assert "LD_PRELOAD" not in env
        assert "CAIRN_EGRESS_PROXY" in env
        # model gateway is admitted as model traffic
        a, _ = proxy.decide(eg.HTTPS, "api.anthropic.com", 443)
        assert a == "proxy_model"
        # the authorized target is admitted and consumes quota
        a, _ = proxy.decide(eg.HTTP, "target.example", 80)
        assert a == "proxy_target"
        # anything else is refused (no silent allow)
        a, _ = proxy.decide(eg.HTTP, "evil.example", 80)
        assert a == "refuse_scope"
    finally:
        proxy.stop()


# ---------------------------------------------------------------------------
# M4: failure classification + bounded recovery
# ---------------------------------------------------------------------------

def test_failure_classifier_categories():
    from cairn.server.research_services import classify_failure
    assert classify_failure(returncode=0)["category"] == "ok"
    t = classify_failure(returncode=None, timed_out=True)
    assert t["category"] == "timeout" and t["recoverable"] is True
    b = classify_failure(returncode=1, detail='{"is_error":true,"api_error_status":402,"subtype":"error_max_budget_usd"}')
    assert b["category"] == "budget_exhausted" and b["recoverable"] is False
    p = classify_failure(returncode=1, detail="...api_error_status 429...")
    assert p["category"] == "provider_error" and p["recoverable"] is True
    p5 = classify_failure(returncode=1, detail="api_error_status: 503")
    assert p5["category"] == "provider_error" and p5["recoverable"] is True
    perm = classify_failure(returncode=1, detail="permission_denied: Bash requires approval")
    assert perm["category"] == "permission_error" and perm["recoverable"] is False
    e = classify_failure(returncode=7, detail="boom")
    assert e["category"] == "execution_error" and e["recoverable"] is False
    ab = classify_failure(returncode=1, cancelled=True)
    assert ab["category"] == "aborted" and ab["recoverable"] is False


def test_fail_session_bounded_recovery_cap(tmp_path):
    """M4: recoverable transient failures are auto-re-queued up to the retry cap;
    the next failure flips to failed and retry_count never exceeds the cap."""
    from cairn.server.research_services import fail_session
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from cairn.server.routers.research import router
    _prev = db._db_path
    db._db_path = None
    db.configure(tmp_path / "r.db")
    app = FastAPI(); app.include_router(router)
    try:
        with TestClient(app) as cli:
            created = create(cli)   # fixture: posts a research session
        sid = created["id"]
        # First two recoverable failures -> requeued; cap is 2.
        for attempt in (1, 2):
            with db.get_conn() as conn:
                service.claim_session(conn, "w-m4", lease_seconds=60)
                ok = fail_session(conn, sid, "w-m4", category="provider_error",
                                  recoverable=True, reason="429", max_retries=2)
                assert ok is True
            with db.get_conn() as conn:
                s = service.get_session(conn, sid)
            assert s["status"] == "queued", (attempt, s["status"])
            assert s["retry_count"] == attempt
            assert s["latest_failure"]["category"] == "provider_error"
        # Third failure (retry_count already == cap) -> failed, no attempt overflow.
        with db.get_conn() as conn:
            service.claim_session(conn, "w-m4", lease_seconds=60)
            fail_session(conn, sid, "w-m4", category="provider_error",
                         recoverable=True, reason="429", max_retries=2)
        with db.get_conn() as conn:
            s = service.get_session(conn, sid)
        assert s["status"] == "failed"
        assert s["retry_count"] == 2
        # A permanent failure is never re-queued (stays failed).
        with db.get_conn() as conn:
            conn.execute("UPDATE research_sessions SET status='queued', lease_owner=NULL WHERE id=?", (sid,))
            service.claim_session(conn, "w-m4b", lease_seconds=60)
            fail_session(conn, sid, "w-m4b", category="budget_exhausted",
                         recoverable=False, reason="budget exhausted", max_retries=2)
        with db.get_conn() as conn:
            s = service.get_session(conn, sid)
        assert s["status"] == "failed"
        assert s["latest_failure"]["category"] == "budget_exhausted"
        assert s["latest_failure"]["recoverable"] is False
    finally:
        db._db_path = _prev


def test_worker_provider_error_requeues_bounded(tmp_path, monkeypatch):
    """M4 worker integration: a real worker whose model run dies on a transient
    429-class error records a classified failure and leaves the session queued for
    bounded auto-recovery (next tick retries), not failed."""
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "research.db"
    db.configure(research_db)
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as app_client:
        session = create(app_client); sid = session["id"]
    fake_bin = tmp_path / "bin"; fake_bin.mkdir()
    envelope = fake_bin / "envelope.json"
    envelope.write_text(json.dumps({
        "is_error": True, "api_error_status": 429,
        "type": "result", "result": "",
    }), encoding="utf-8")
    claude = fake_bin / "claude"
    claude.write_text("#!/bin/sh\ncat " + str(envelope) + "\nexit 1\n", encoding="utf-8")
    claude.chmod(0o755)
    monkeypatch.setenv("CAIRN_CLAUDE_BIN", str(claude))
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
    with db.get_conn() as conn:
        service.claim_session(conn, "w-m4i", lease_seconds=60)
    w = ResearchWorker(db_path=research_db, workspace_root=tmp_path / "ws", worker_id="w-m4i")
    w.run_session(sid)
    with db.get_conn() as conn:
        detail = service.get_session(conn, sid)
    # Transient provider error -> recoverable -> re-queued, one attempt recorded.
    assert detail["status"] == "queued", detail["status"]
    assert detail["retry_count"] == 1
    assert detail["latest_failure"]["category"] == "provider_error"
    assert detail["latest_failure"]["recoverable"] is True


# ---------------------------------------------------------------------------
# M4: cost governance (remaining-budget telemetry + top-up→resume loop)
# ---------------------------------------------------------------------------

def test_settle_emits_budget_warning(tmp_path):
    """M4: after settlement drops remaining cost budget below a threshold, a
    budget_warn event is emitted once so the runway is visible before the hard gate."""
    from cairn.server.routers.research import router
    _prev = db._db_path
    db._db_path = None
    db.configure(tmp_path / "gw.db")
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as cli:
        created = create(cli); sid = created["id"]
    with db.get_conn() as conn:
        b = json.loads(conn.execute("SELECT budget_json FROM research_sessions WHERE id=?", (sid,)).fetchone()[0])
        b["max_cost_usd"] = 1.0
        conn.execute("UPDATE research_sessions SET budget_json=? WHERE id=?", (json.dumps(b), sid))
        conn.commit()
        s = service.claim_session(conn, "w-gov", lease_seconds=60)
        run_id = s["current_run_id"]
    # Settling a $0.60 cost leaves 40% remaining -> crosses the 50% threshold.
    with db.get_conn() as conn:
        service.settle_run(conn, sid, run_id, "w-gov", requests=1, elapsed_seconds=30, final_cost=0.60)
    with db.get_conn() as conn:
        detail = service.get_session(conn, sid)
    warns = [e for e in detail["events"] if e["kind"] == "budget_warn"]
    assert warns, detail["events"]
    assert warns[0]["detail"]["remaining"]["cost_pct"] <= 50.0
    assert warns[0]["detail"]["remaining"]["cost_usd"] <= 0.41
    db._db_path = _prev


def test_budget_topup_resume_recovery_loop(tmp_path):
    """M4: a budget-exhausted failed session refuses resume with a 409; after an
    explicit, confirmed budget top-up it can resume again (governance recovery loop)."""
    from cairn.server.routers.research import router
    _prev = db._db_path
    db._db_path = None
    db.configure(tmp_path / "tp.db")
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as cli:
        created = create(cli); sid = created["id"]
        # Simulate a prior run that burned the whole approved budget.
        with db.get_conn() as conn:
            conn.execute("UPDATE research_sessions SET usage_json=?, status='failed' WHERE id=?",
                         (json.dumps({"cost_usd": 1.0, "requests": 1, "steps": 1, "elapsed_seconds": 60}), sid))
            conn.commit()
        # Resume must be refused while budget is exhausted.
        r = cli.post(f"/api/research/sessions/{sid}/resume")
        assert r.status_code == 409, r.text
        # Top-up the budget (increase requires explicit confirmation).
        r = cli.patch(f"/api/research/sessions/{sid}/budget",
                      json={"budget": {"max_cost_usd": 2.0}, "authorization_confirmed": True})
        assert r.status_code == 200, r.text
        r = cli.post(f"/api/research/sessions/{sid}/resume")
        assert r.status_code == 200, r.text
        detail = r.json()
        assert detail["status"] == "queued", detail["status"]
        assert detail["usage"]["cost_usd"] == 1.0  # spent runway preserved, not reset
        assert any(e["kind"] == "budget" for e in detail["events"])  # top-up seen
    db._db_path = _prev


# ---------------------------------------------------------------------------
# M4: experience feedback (cross-session learning)
# ---------------------------------------------------------------------------

def test_distill_experiences_from_confirmed_findings(tmp_path):
    """M4: distilling a session writes one experience per confirmed finding (with a
    stable lesson row for a recorded failure) keyed by target scope and is idempotent;
    the source session is excluded from its own scope listing."""
    from cairn.server.routers.research import router
    _prev = db._db_path
    db._db_path = None
    db.configure(tmp_path / "exp.db")
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as cli:
        a = create(cli); sid = a["id"]
    scope = service.session_scope({"repo": "/project/app"})
    with db.get_conn() as conn:
        conn.execute("UPDATE research_sessions SET repo=? WHERE id=?", ("/project/app", sid))
        ev = service.record_evidence(conn, sid, "http", "GET /data", '{"id":1,"owner":"admin"}')
        _ = service.record_finding(conn, sid, "未授权读取", "状态被公开且可无密码访问", status="confirmed", evidence_ids=[ev["id"]])
        conn.execute("UPDATE research_sessions SET latest_failure_json=? WHERE id=?",
                     (json.dumps({"category": "budget_exhausted", "recoverable": False,
                                  "reason": "费用预算耗尽", "attempt": 1}), sid))
        conn.commit()
        created = service.distill_experiences(conn, sid)
        again = service.distill_experiences(conn, sid)  # idempotent
    assert len(created) == 2, created  # one finding + one lesson
    assert again == []  # no duplicates on a second distill
    with db.get_conn() as conn:
        rows = service.list_experiences_for_scope(conn, scope, session_id="other", limit=20)
    assert len(rows) == 2
    kinds = {r["kind"] for r in rows}
    assert kinds == {"finding", "lesson"}
    assert any(r["source_session_id"] == sid and "未授权读取" in r["content"] for r in rows)
    # Source session is excluded from its own listing.
    with db.get_conn() as conn:
        own = service.list_experiences_for_scope(conn, scope, session_id=sid, limit=20)
    assert own == []
    db._db_path = _prev


def test_experience_injected_into_same_scope_prompt(tmp_path, monkeypatch):
    """M4: a later session on the SAME target material gets prior findings+lesson
    injected into its prompt with provenance; a different scope does not."""
    from cairn.server.routers.research import router
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "exp2.db"
    db.configure(research_db)
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as cli:
        s1 = create(cli); sid1 = s1["id"]
        s2 = create(cli); sid2 = s2["id"]
    # Seed an experience on scope repo:/a from "another" session (sid1 targets it).
    with db.get_conn() as conn:
        conn.execute("UPDATE research_sessions SET repo=? WHERE id=?", ("/proj/a", sid1))
        conn.execute("UPDATE research_sessions SET repo=? WHERE id=?", ("/proj/a", sid2))
        conn.execute(
            "INSERT INTO research_experiences (id,source_session_id,scope_key,kind,content,ref,created_at) VALUES (?,?,?,?,?,?,?)",
            ("exp-seed", sid1, "repo:/proj/a", "finding", "公开只读接口 /data 无需鉴权即可读取他人数据", "finding f1", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
    # Build the run-time prompt for session 2 (same scope).
    with db.get_conn() as conn:
        detail = service.get_session(conn, sid2)
    w = ResearchWorker(db_path=research_db, workspace_root=tmp_path / "ws", worker_id="w-exp")
    prompt = w._build_prompt(detail, detail["budget"], detail["usage"], detail["authorization"], remaining_seconds=120)
    assert "公开只读接口 /data 无需鉴权即可读取他人数据" in prompt
    assert "[finding]" in prompt
    assert sid1 in prompt and "来源会话" in prompt
    assert "跨会话经验" in prompt
    # Different scope must not see it.
    with db.get_conn() as conn:
        conn.execute("UPDATE research_sessions SET repo=? WHERE id=?", ("/proj/b", sid2))
        detail2 = service.get_session(conn, sid2)
    prompt2 = w._build_prompt(detail2, detail2["budget"], detail2["usage"], detail2["authorization"], remaining_seconds=120)
    assert "公开只读接口 /data 无需鉴权即可读取他人数据" not in prompt2


# ---------------------------------------------------------------------------
# M4: change triggering (fingerprint baseline -> re-audit re-queue)
# ---------------------------------------------------------------------------

def _make_repo(tmp_path, secret="ok"):
    r = tmp_path / "app"
    r.mkdir()
    (r / "main.py").write_text(f"SECRET='{secret}'\n@app.route('/data')\ndef d(): ...\n", encoding="utf-8")
    (r / "conf").mkdir()
    (r / "conf" / "cfg.yaml").write_text(f"mode: {secret}\n", encoding="utf-8")
    return str(r)


def test_repo_fingerprint_changes_with_content(tmp_path):
    """M4: repo fingerprint is a stable hash of the tree that changes when a file's
    content (or presence) changes, and ignores .git plus ordering."""
    from cairn.server.research_services import fingerprint_repo
    r = tmp_path / "app"; r.mkdir()
    (r / "a.txt").write_text("one", encoding="utf-8")
    (r / "sub").mkdir(); (r / "sub" / "b.txt").write_text("two", encoding="utf-8")
    f1 = fingerprint_repo(str(r))
    f2 = fingerprint_repo(str(r))          # identical -> same hash
    assert f1 == f2
    (r / "a.txt").write_text("ONE!", encoding="utf-8")
    f3 = fingerprint_repo(str(r))          # content change -> new hash
    assert f3 != f1
    # a .git dir does not affect the hash
    (r / ".git").mkdir(); (r / ".git" / "objects").write_text("junk", encoding="utf-8")
    assert fingerprint_repo(str(r)) == f3


def test_recheck_baseline_and_change_requeues(tmp_path):
    """M4 end-to-end: recheck stores a baseline; after the repo content changes it
    reports changed and re-queues a completed session for re-audit (new run batch,
    same history/budget), guarded so only completed sessions re-audit."""
    from cairn.server.routers import research as _rr
    repo = _make_repo(tmp_path)
    _prev = db._db_path
    db._db_path = None
    db.configure(tmp_path / "ch.db")
    app = FastAPI(); app.include_router(_rr.router)
    with TestClient(app) as cli:
        created = create(cli, repo=repo, url=None); sid = created["id"]
        # First recheck: no baseline yet -> recorded, unchanged.
        r = cli.post(f"/api/research/sessions/{sid}/recheck")
        assert r.status_code == 200, r.text
        first = r.json()
        assert first["changed"] is False and first["requeued"] is False
        # Unchanged on immediate repeat.
        r = cli.post(f"/api/research/sessions/{sid}/recheck")
        assert r.json()["changed"] is False
        # Not completed -> even a change must not re-audit.
        _mutate_repo(tmp_path)
        r = cli.post(f"/api/research/sessions/{sid}/recheck")
        assert r.json()["changed"] is True
        assert r.json()["requeued"] is False  # session is queued, not completed
        # Now mark completed and recheck a fresh change -> re-audit re-queued.
        with db.get_conn() as conn:
            conn.execute("UPDATE research_sessions SET status='completed' WHERE id=?", (sid,))
            conn.commit()
        _mutate_repo(tmp_path, extra=True)
        r = cli.post(f"/api/research/sessions/{sid}/recheck")
        out = r.json()
        assert out["changed"] is True and out["requeued"] is True, out
        with db.get_conn() as conn:
            detail = service.get_session(conn, sid)
        assert detail["status"] == "queued"
        assert detail["phase"] == 1
        assert any(e["kind"] == "change" for e in detail["events"])
        assert "复测" in detail["events"][-1]["description"]
    db._db_path = _prev


def _mutate_repo(tmp_path, extra=False):
    (tmp_path / "app" / "main.py").write_text("SECRET='changed'\n@app.route('/admin')\ndef a(): ...\n", encoding="utf-8")
    if extra:
        (tmp_path / "app" / "extra.txt").write_text("new", encoding="utf-8")


# ---------------------------------------------------------------------------
# M1 close-out: auto-finalize a paused step when budget/steps are exhausted
# ---------------------------------------------------------------------------

def _make_nonterminal_claude(tmp_path):
    fake_bin = tmp_path / "bin2"
    fake_bin.mkdir(exist_ok=True)
    claude = fake_bin / "claude"
    claude.write_text(
        '#!/usr/bin/env python3\n'
        'import json\n'
        'payload = {\n'
        '  "summary": "本轮完成部分探索，还需继续",\n'
        '  "evidence": [{"kind":"note","title":"notes","content":"partial progress"}],\n'
        '  "findings": [],\n'
        '  "next_direction": "继续探测", "phase": 1, "terminal": False, "awaiting_input": False\n'
        '}\n'
        'out = {"is_error": False, "result": json.dumps(payload), "total_cost_usd": 0.002}\n'
        'print(json.dumps(out))\n',
        encoding="utf-8",
    )
    claude.chmod(0o755)
    return fake_bin


def test_paused_step_autofinalizes_on_steps_cap(tmp_path, monkeypatch):
    """M1 close-out: a NON-terminal step that lands in `paused` is auto-closed into
    `completed` with a report the moment the max_steps/budget can't fund another step
    (instead of parking forever awaiting a manual resume)."""
    from cairn.server.routers.research import router as _r
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "ac.db"
    db.configure(research_db)
    app = FastAPI(); app.include_router(_r)
    with TestClient(app) as cli:
        sid = create(cli, budget={"minutes": 45, "requests": 50, "max_cost_usd": 1.0, "max_steps": 1})["id"]
    fake_bin = _make_nonterminal_claude(tmp_path)
    claude = fake_bin / "claude"
    monkeypatch.setenv("CAIRN_CLAUDE_BIN", str(claude))
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
    with db.get_conn() as conn:
        service.claim_session(conn, "w-ac", lease_seconds=60)
    w = ResearchWorker(db_path=research_db, workspace_root=tmp_path / "ws", worker_id="w-ac")
    w.run_session(sid)
    with db.get_conn() as conn:
        detail = service.get_session(conn, sid)
        rep = conn.execute("SELECT id FROM research_reports WHERE session_id=? ORDER BY created_at DESC", (sid,)).fetchone()
    assert detail["status"] == "completed", detail["status"]
    assert detail["phase"] == 3
    assert rep is not None  # auto report committed by the close-out
    auto = [e for e in detail["events"] if e["kind"] == "completed" and "自动收束" in e["title"]]
    assert auto, [e for e in detail["events"]]  # honest close-out event


def test_paused_step_stays_paused_with_budget(tmp_path, monkeypatch):
    """M1 close-out (negative): with budget/steps remaining, a non-terminal step stays
    `paused` (round boundary) and is NOT auto-finalized."""
    monkeypatch.setattr(db, "_db_path", None)
    research_db = tmp_path / "ac2.db"
    db.configure(research_db)
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as cli:
        sid = create(cli, budget={"minutes": 45, "requests": 50, "max_cost_usd": 5.0, "max_steps": 10})["id"]
    fake_bin = _make_nonterminal_claude(tmp_path)
    claude = fake_bin / "claude"
    monkeypatch.setenv("CAIRN_CLAUDE_BIN", str(claude))
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
    with db.get_conn() as conn:
        service.claim_session(conn, "w-ac2", lease_seconds=60)
    w = ResearchWorker(db_path=research_db, workspace_root=tmp_path / "ws", worker_id="w-ac2")
    w.run_session(sid)
    with db.get_conn() as conn:
        detail = service.get_session(conn, sid)
    assert detail["status"] == "paused", detail["status"]
    assert not any("自动收束" in e["title"] for e in detail["events"])


# ---------------------------------------------------------------------------
# Pluggable agent driver (M-era: research works with non-claude agents)
# ---------------------------------------------------------------------------

def test_research_driver_pluggable(tmp_path, monkeypatch):
    """The research worker resolves a driver from the shared dispatcher registry and
    reuses each driver's own argv builder + envelope extraction: claudecode is the
    default and keeps its research-tuned args and Claude config seeding; codex/mock
    reuse their existing build_execute and do NOT seed Claude config or use Claude
    budget/permission flags."""
    import cairn.server.research_worker as rw_mod
    seen = {}
    def fake_sandbox(workspace, repo, argv, **kw):
        seen["claude_config_src"] = kw.get("claude_config_src")
        return ["bwrap", "rec", *argv]
    monkeypatch.setattr(rw_mod, "build_sandbox", fake_sandbox)
    monkeypatch.setenv("HOME", "/tmp/fakehome")
    monkeypatch.delenv("CAIRN_CLAUDE_BIN", raising=False)
    monkeypatch.delenv("CAIRN_RESEARCH_DRIVER", raising=False)

    # default -> claudecode
    w = ResearchWorker(db_path=tmp_path / "x.db", worker_id="w-drv")
    assert w._driver.type_name == "claudecode"
    argv = w._driver_argv("hi", remaining_cost=0.5, session_id="s1")
    assert "bypassPermissions" in argv and "--max-budget-usd" in argv
    assert w._driver.local_binary() == "claude"
    w._build_sandboxed_argv(tmp_path / "ws", {"repo": None}, argv, claude_bin="claude")
    assert seen["claude_config_src"] is not None  # Claude config seeded for claude

    # codex via env -> reuses CodexDriver(local=True).build_execute, no claude bits
    monkeypatch.setenv("CAIRN_RESEARCH_DRIVER", "codex")
    w2 = ResearchWorker(db_path=tmp_path / "x.db", worker_id="w-drv2")
    assert w2._driver.type_name == "codex"
    a2 = w2._driver_argv("hi", remaining_cost=0.5, session_id="s2")
    assert a2[0] == "codex" and "bypassPermissions" not in a2
    assert w2._driver.local_binary() == "codex"
    w2._build_sandboxed_argv(tmp_path / "ws2", {"repo": None}, a2, claude_bin="codex")
    assert seen["claude_config_src"] is None  # no Claude config for other agents

    # mock resolves and reuses its python3 argv builder
    monkeypatch.setenv("CAIRN_RESEARCH_DRIVER", "mock")
    w3 = ResearchWorker(db_path=tmp_path / "x.db", worker_id="w-drv3")
    assert w3._driver.type_name == "mock"
    a3 = w3._driver_argv("hi", remaining_cost=1.0, session_id="s3")
    assert a3[0] == "python3"

    # explicit driver param overrides env default
    monkeypatch.setenv("CAIRN_RESEARCH_DRIVER", "claudecode")
    w4 = ResearchWorker(db_path=tmp_path / "x.db", worker_id="w-drv4", driver="pi")
    assert w4._driver.type_name == "pi"
    monkeypatch.delenv("CAIRN_RESEARCH_DRIVER", raising=False)
