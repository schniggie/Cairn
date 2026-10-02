#!/usr/bin/env python3
"""清理一道题:停 project -> 删 worker 容器 -> 删 DB 记录。

⚠️ 顺序很重要:直接 DELETE 会泄漏容器(dispatcher 不会清理孤儿容器)。

用法:
  python cleanup_project.py --project proj_001
  python cleanup_project.py --project proj_001 --skip-docker   # 宿主机不装 docker 时跳过容器清理
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

SERVER = os.environ.get("CAIRN_SERVER", "http://127.0.0.1:8000")
CONTAINER_PREFIX = "cairn-dispatch-"


def _request(method: str, path: str, payload: dict | None = None) -> int:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{SERVER}{path}",
        data=data,
        headers={"Content-Type": "application/json"} if payload else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> int:
    parser = argparse.ArgumentParser(description="清理一道题的 project 和容器")
    parser.add_argument("--project", required=True, help="project id")
    parser.add_argument("--skip-docker", action="store_true", help="跳过 docker rm")
    args = parser.parse_args()

    pid = args.project
    container = f"{CONTAINER_PREFIX}{pid}"

    # 1. 停 project(让 dispatcher cancel 运行中任务)
    s = _request("PUT", f"/projects/{pid}/status", {"status": "stopped"})
    print(f"[1/3] 停 project: status={s}")
    if s not in (200, 409):  # 409 = 已完成/已停,也算 ok
        print(f"停 project 异常 status={s}", file=sys.stderr)

    # 2. 删容器
    if not args.skip_docker:
        try:
            r = subprocess.run(
                ["docker", "rm", "-f", container],
                capture_output=True,
                text=True,
                timeout=30,
            )
            print(f"[2/3] 删容器 {container}: rc={r.returncode}")
            if r.returncode != 0:
                print(f"  docker 输出: {r.stderr.strip()}", file=sys.stderr)
        except FileNotFoundError:
            print("[2/3] 本机未找到 docker,跳过容器清理", file=sys.stderr)
    else:
        print("[2/3] --skip-docker,跳过容器清理")

    # 3. 删 DB 记录
    s = _request("DELETE", f"/projects/{pid}")
    print(f"[3/3] 删 DB 记录: status={s}")

    return 0 if s in (204, 404) else 1


if __name__ == "__main__":
    raise SystemExit(main())
