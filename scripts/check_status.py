#!/usr/bin/env python3
"""巡检所有 project 的状态,标出疑似异常的题。

用法:
  python check_status.py              # 打印全部 + 异常标注
  python check_status.py --json       # 只输出原始 JSON(供程序消费)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

SERVER = os.environ.get("CAIRN_SERVER", "http://127.0.0.1:8000")


def _get(path: str):
    req = urllib.request.Request(f"{SERVER}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode("utf-8", errors="replace")}


def _flag(project) -> str:
    """判断一个 project 是否疑似异常,返回原因或空串。"""
    reasons = []
    if project["status"] != "active":
        return ""
    w = project.get("working_intent_count", 0)
    u = project.get("unclaimed_intent_count", 0)
    if w == 0 and u == 0:
        reasons.append("无任务无新意图(疑似卡住)")
    elif w == 0 and u > 0:
        reasons.append("有意图但无人认领(worker 忙或挂)")
    return " | ".join(reasons)


def main() -> int:
    parser = argparse.ArgumentParser(description="巡检 Cairn project 状态")
    parser.add_argument("--json", action="store_true", help="只输出原始 JSON")
    args = parser.parse_args()

    status, body = _get("/projects")
    if status != 200:
        print(f"请求失败 status={status} body={body}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0

    if not body:
        print("当前没有 project。")
        return 0

    print(f"{'id':<12} {'status':<10} {'fact':<5} {'intent':<7} {'work':<5} {'unclaim':<7} {'hint':<5} 异常")
    print("-" * 80)
    for p in body:
        anomaly = _flag(p)
        line = (
            f"{p['id']:<12} {p['status']:<10} {p['fact_count']:<5} "
            f"{p['intent_count']:<7} {p['working_intent_count']:<5} "
            f"{p['unclaimed_intent_count']:<7} {p['hint_count']:<5}"
        )
        if anomaly:
            print(f"{line} [!] {anomaly}")
        else:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
