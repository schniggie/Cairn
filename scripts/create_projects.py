#!/usr/bin/env python3
"""批量建 Cairn project(一道赛题 = 一个 project)。

输入:一个 JSON 文件,每道题一个对象:
  [{"challenge_id": "1", "title": "xxx", "description": "题目描述", "hint": "可选方向引导"}]

用法:
  python create_projects.py --input challenges.json
  python create_projects.py --input challenges.json --goal "获取 flag 并提交"

建完后把 project_id -> challenge_id 的映射写到 scripts/projects_map.json,
方便后续提取 flag 后知道该提交到哪道题。
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
MAP_FILE = os.path.join(os.path.dirname(__file__), "projects_map.json")


def _post(path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{SERVER}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, {"error": body}


def main() -> int:
    parser = argparse.ArgumentParser(description="批量建 Cairn project")
    parser.add_argument("--input", required=True, help="题目 JSON 文件路径")
    parser.add_argument("--goal", default="解题并获取 flag", help="project 的 goal 描述")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        challenges = json.load(f)

    # 合并已有映射(不覆盖,避免丢失旧 project 的 challenge 对应关系)
    mapping: dict[str, str] = {}
    if os.path.exists(MAP_FILE):
        try:
            with open(MAP_FILE, encoding="utf-8") as f:
                mapping = json.load(f)
        except Exception:
            mapping = {}

    for ch in challenges:
        cid = str(ch.get("challenge_id", ch.get("title", "")))
        title = ch.get("title", cid)
        origin = ch.get("description", "")
        hint = ch.get("hint")

        payload = {
            "title": title,
            "origin": origin,
            "goal": args.goal,
            "bootstrap_enabled": True,
        }
        if ch.get("difficulty"):
            payload["difficulty"] = ch["difficulty"]
        if hint:
            payload["hints"] = [{"content": hint, "creator": "commander"}]

        status, body = _post("/projects", payload)
        if status == 201:
            pid = body["project"]["id"]
            mapping[pid] = cid
            print(f"[OK] {cid} -> {pid} (title={title!r})")
        else:
            print(f"[FAIL] {cid} status={status} body={body}", file=sys.stderr)

    with open(MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)
    print(f"\n映射已写入 {MAP_FILE}: {len(mapping)} 道题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
