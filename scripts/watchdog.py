#!/usr/bin/env python3
"""Cairn 常驻看门狗:确定性监控 + 自动干预。

它做的事情都是"规则化"的,不依赖 LLM 判断:
  1. 清理孤儿容器(名字 cairn-dispatch-* 但 DB 里没有对应 project)
  2. 检测 flag -> 提交 -> 提交成功则清理该题(停 + 删容器 + 删 DB)
  3. 卡住检测(active 且长时间无任务无新意图)-> 加一条 hint 撬动 reason 重新规划

用法:
  python watchdog.py                # 常驻循环
  python watchdog.py --once         # 只跑一轮(测试/调试用)
  python watchdog.py --interval 30  # 巡检间隔(秒)

配置读 scripts/competition.yaml(不存在则用默认值,flag 提交会跳过直到填好 API)。
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

SERVER = os.environ.get("CAIRN_SERVER", "http://127.0.0.1:8000")
HERE = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(HERE, "competition.yaml")
MAP_FILE = os.path.join(HERE, "projects_map.json")
CONTAINER_PREFIX = "cairn-dispatch-"

DEFAULT_FLAG_REGEX = r"flag\{[^}]+\}"
STUCK_HINT = "检测到该方向长时间无进展。请重新审视题目线索,换一个攻击面或思路,不要重复已经失败的尝试。"
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"


def _log(msg: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def _http(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload else {}
    req = urllib.request.Request(f"{SERVER}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, _parse(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, _parse(raw)


def _parse(raw: str):
    """JSON 则解析为对象,否则(如 export 的 YAML 文本)返回原始字符串。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        import yaml  # type: ignore
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _load_map() -> dict:
    if not os.path.exists(MAP_FILE):
        return {}
    try:
        with open(MAP_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _docker_names() -> set[str]:
    try:
        r = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return set()
        return {line.strip() for line in r.stdout.splitlines() if line.strip()}
    except Exception:
        return set()


def _docker_rm(name: str) -> bool:
    try:
        r = subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def _recover_platform_env(cfg: dict, cid: str) -> None:
    """回收平台靶机环境(recover-exercise-env),释放 3 台额度。"""
    comp = cfg.get("competition", {})
    host = comp.get("host", "")
    ak = comp.get("access_key", "")
    path = (comp.get("fetch", {}) or {}).get("recover_env_url", "")
    if not host or not ak or not path:
        return
    req = urllib.request.Request(
        host + path,
        data=json.dumps({"exerciseId": int(cid)}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Agent-AccessKey": ak, "User-Agent": _BROWSER_UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            _log(f"  已回收平台环境 exerciseId={cid} status={resp.status}")
    except Exception as exc:
        _log(f"  回收平台环境失败 {cid}: {exc}")


def _cleanup_project(pid: str, cfg: dict | None = None) -> None:
    """销毁一道题:回收平台环境 -> 停 project -> 删容器 -> 删 DB。"""
    if cfg:
        cid = _load_map().get(pid, "")
        if cid:
            _recover_platform_env(cfg, cid)
    s, _ = _http("PUT", f"/projects/{pid}/status", {"status": "stopped"})
    if s not in (200, 409):
        _log(f"  停 project {pid} 异常 status={s}")
    if _docker_rm(f"{CONTAINER_PREFIX}{pid}"):
        _log(f"  已删容器 {CONTAINER_PREFIX}{pid}")
    s, _ = _http("DELETE", f"/projects/{pid}")
    _log(f"  已删 DB 记录 {pid} (status={s})")


def _cleanup_orphans(known_ids: set[str]) -> None:
    names = _docker_names()
    orphans = [n for n in names if n.startswith(CONTAINER_PREFIX) and n[len(CONTAINER_PREFIX):] not in known_ids]
    for name in orphans:
        if _docker_rm(name):
            _log(f"[孤儿容器] 清理 {name}")
        else:
            _log(f"[孤儿容器] 清理失败 {name}")


def _handle_flags(cfg: dict, proj_map: dict, submitted: dict) -> None:
    """检查每个 active project 的导出,发现 flag 就提交,成功则清理。"""
    auto_submit = cfg.get("watchdog", {}).get("auto_submit", True)
    auto_cleanup = cfg.get("watchdog", {}).get("auto_cleanup", True)
    flag_regex = cfg.get("flag_regex", DEFAULT_FLAG_REGEX)
    submit_url = cfg.get("competition", {}).get("submit", {}).get("url", "")

    status, projects = _http("GET", "/projects")
    if status != 200 or not isinstance(projects, list):
        return

    for p in projects:
        pid = p["id"]
        # active 和 completed 都检测:completed 可能是 agent 解出 flag 后标记完成,但要提交才算数
        if p["status"] not in ("active", "completed"):
            continue
        _, export = _http("GET", f"/projects/{pid}/export?format=yaml")
        text = export if isinstance(export, str) else ""
        flags = set()
        # 只从 facts(agent 的结论)里提取 flag,排除 origin/goal/hints 里的占位符
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(text)
            for fact in (data or {}).get("facts", []):
                if fact.get("id") in ("origin", "goal"):
                    continue
                desc = fact.get("description", "") or ""
                flags.update(re.findall(flag_regex, desc))
        except Exception:
            flags = set(re.findall(flag_regex, text))
        # 过滤明显占位(如题目描述里的 DASCTF{...})
        flags = {f for f in flags if f not in ("DASCTF{...}", "flag{...}")}
        if not flags:
            continue
        cid = proj_map.get(pid, "")
        for flag in flags:
            if flag in submitted.get(pid, set()):
                continue
            submitted.setdefault(pid, set()).add(flag)
            _log(f"[flag] {pid} -> {flag} (challenge={cid or '未知'})")
            if not auto_submit or not cid or not submit_url or submit_url.startswith("TODO"):
                _log("  跳过提交(未配置安恒 API 或缺少 challenge 映射)")
                continue
            ok = _submit_flag(cfg, cid, flag)
            if ok and auto_cleanup:
                _log(f"[提交成功] 销毁 {pid}")
                _cleanup_project(pid, cfg)


def _submit_flag(cfg: dict, cid: str, flag: str) -> bool:
    comp = cfg.get("competition", {})
    submit = comp.get("submit", {})
    url = comp.get("host", "") + submit.get("url", "")
    method = submit.get("method", "POST")
    headers = submit.get("headers", {}) or {}

    # 平台只认 flag 内容,不认 DASCTF{} / flag{} 外壳,提交前剥掉
    shell = re.match(r"^(?:DASCTF|flag)\{([^}]*)\}$", flag)
    if shell:
        flag = shell.group(1)

    payload = {"exerciseId": int(cid), "flag": flag}
    body = json.dumps(payload)
    req = urllib.request.Request(
        url, data=body.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Agent-AccessKey": comp.get("access_key", ""),
            "User-Agent": _BROWSER_UA,
            **headers,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            _log(f"  提交响应 status={resp.status}: {raw[:200]}")
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        _log(f"  提交失败 status={exc.code}: {raw[:200]}")
        return False


def _handle_project_timeout(cfg: dict, timed_out: dict) -> None:
    """整道题超时止损:active 且运行超过 project_timeout(默认 30 分钟)仍没解出 → 自动 stop。

    停掉后 dispatcher 会 cancel 任务、停容器,释放 token 和资源,供其他题使用。
    """
    timeout = cfg.get("watchdog", {}).get("project_timeout", 1800)
    status, projects = _http("GET", "/projects")
    if status != 200 or not isinstance(projects, list):
        return

    now = time.time()
    for p in projects:
        pid = p["id"]
        if p["status"] != "active":
            continue
        created = p.get("created_at", "") or ""
        if not created:
            continue
        try:
            # created_at 是 UTC 时间,必须按 UTC 解析(calendar.timegm),否则时区会虚增运行时长
            created_ts = calendar.timegm(time.strptime(created.replace("Z", ""), "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue
        age = now - created_ts
        if age >= timeout and pid not in timed_out:
            timed_out[pid] = age
            _log(f"[超时止损] {pid} 运行 {int(age / 60)} 分钟超过 {int(timeout / 60)} 分钟,自动停止释放资源")
            s, _ = _http("PUT", f"/projects/{pid}/status", {"status": "stopped"})
            _log(f"  停止结果 status={s}")


def _handle_stuck(cfg: dict, stuck_since: dict) -> None:
    """卡住检测:active 且 work==0 且 unclaim==0 持续超时,则加 hint。"""
    timeout = cfg.get("watchdog", {}).get("stuck_timeout", 600)
    status, projects = _http("GET", "/projects")
    if status != 200 or not isinstance(projects, list):
        return

    now = time.time()
    for p in projects:
        pid = p["id"]
        if p["status"] != "active":
            stuck_since.pop(pid, None)
            continue
        if p.get("working_intent_count", 0) == 0 and p.get("unclaimed_intent_count", 0) == 0:
            since = stuck_since.setdefault(pid, now)
            if now - since >= timeout:
                _log(f"[卡住] {pid} 无进展超过 {int(timeout / 60)} 分钟,加 hint 撬动 reason")
                s, _ = _http("POST", f"/projects/{pid}/hints", {"content": STUCK_HINT, "creator": "watchdog"})
                _log(f"  加 hint 结果 status={s}")
                stuck_since[pid] = now  # 重置计时,避免反复加
        else:
            stuck_since.pop(pid, None)


def _sync_new_challenges() -> None:
    """定期调用 sync_challenges.py 监测平台新题并自动建 project。"""
    script = os.path.join(HERE, "sync_challenges.py")
    try:
        # 用 bytes 采集,手动 UTF-8 解码,避免 Windows GBK 解码子进程中文输出崩溃
        r = subprocess.run(
            [sys.executable, script, "--max-new", "3"],
            capture_output=True, timeout=180,
        )
        stdout = r.stdout.decode("utf-8", errors="replace")
        stderr = r.stderr.decode("utf-8", errors="replace")
        for line in stdout.strip().splitlines():
            if line.strip():
                _log(f"[sync] {line}")
        if r.returncode != 0 and stderr.strip():
            _log(f"[sync] 出错: {stderr.strip()[-200:]}")
    except Exception as exc:
        _log(f"[sync] 异常: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Cairn 常驻看门狗")
    parser.add_argument("--once", action="store_true", help="只跑一轮")
    parser.add_argument("--interval", type=int, default=None, help="巡检间隔(秒,默认读配置或 30)")
    args = parser.parse_args()

    cfg = _load_config()
    interval = args.interval or cfg.get("watchdog", {}).get("interval", 30)
    stuck_since: dict[str, float] = {}
    submitted: dict[str, set[str]] = {}
    timed_out: dict[str, float] = {}
    last_sync = 0.0
    sync_interval = cfg.get("watchdog", {}).get("sync_interval", 300)

    _log(f"看门狗启动 server={SERVER} interval={interval}s once={args.once}")
    while True:
        try:
            proj_map = _load_map()
            _, projects = _http("GET", "/projects")
            known_ids = {p["id"] for p in projects} if isinstance(projects, list) else set()

            _cleanup_orphans(known_ids)
            _handle_flags(cfg, proj_map, submitted)
            _handle_stuck(cfg, stuck_since)
            _handle_project_timeout(cfg, timed_out)
            # 定期监测平台新题并自动建 project
            if time.time() - last_sync >= sync_interval:
                _sync_new_challenges()
                last_sync = time.time()
        except Exception as exc:
            _log(f"巡检异常: {exc}")

        if args.once:
            break
        time.sleep(interval)

    _log("看门狗退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
