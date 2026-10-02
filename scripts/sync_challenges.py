#!/usr/bin/env python3
"""监测平台新题并自动建 Cairn project(供 watchdog 定期调用)。

逻辑:
  1. 拉 exercise-list,找 isOpen 且未解 且未建过(projects_map 里没有)的题
  2. 每道新题:查详情 → 附件题直接建;环境题先 build-env 拿靶机再建
  3. origin 包含:题目描述 + 附件URL + flag格式 + 靶机地址
  4. 更新 projects_map(合并,保留旧映射)

用法:
  python sync_challenges.py              # 检查一次,建新题
  python sync_challenges.py --max-new 2  # 一次最多建 2 道(避免环境超限)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(HERE, "competition.yaml")
MAP_FILE = os.path.join(HERE, "projects_map.json")
CAIRN_SERVER = os.environ.get("CAIRN_SERVER", "http://127.0.0.1:8000")
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"


def _load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        print(f"缺少 {CONFIG_PATH}", file=sys.stderr)
        raise SystemExit(1)
    import yaml  # type: ignore
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_map() -> dict:
    if not os.path.exists(MAP_FILE):
        return {}
    try:
        with open(MAP_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_map(mapping: dict) -> None:
    with open(MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)


def _platform(cfg: dict, method: str, path: str, payload: dict | None = None, params: dict | None = None, retries: int = 4):
    comp = cfg["competition"]
    url = comp["host"] + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode() if payload else None
    headers = {"X-Agent-AccessKey": comp["access_key"], "User-Agent": BROWSER_UA}
    if payload:
        headers["Content-Type"] = "application/json"
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise


def _build_env(cfg: dict, eid: int) -> str | None:
    """启动环境并轮询靶机,返回 'IP:端口' 或 None(失败/超时)。"""
    try:
        _platform(cfg, "POST", cfg["competition"]["fetch"]["build_env_url"], {"exerciseId": eid})
    except Exception as exc:
        print(f"  [WARN] build-env {eid} 失败: {exc}", file=sys.stderr)
        return None
    for _ in range(16):
        time.sleep(5)
        try:
            d = _platform(cfg, "GET", cfg["competition"]["fetch"]["detail_url"], params={"exerciseId": eid})["data"]
        except Exception:
            continue
        ep = d.get("endpoints") or []
        if ep and not d.get("isNeedCheck") and ep[0].get("proxyIps"):
            target = ep[0].get("exposeIps") or []
            if target:
                return target[0]
    return None


def _create_cairn_project(title: str, origin: str, difficulty: str | None = None) -> str | None:
    payload = {"title": title, "origin": origin, "goal": "解题并获取 flag", "bootstrap_enabled": True}
    if difficulty:
        payload["difficulty"] = difficulty
    req = urllib.request.Request(
        f"{CAIRN_SERVER}/projects",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))["project"]["id"]
    except Exception as exc:
        print(f"  [WARN] 建 project 失败: {exc}", file=sys.stderr)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="监测并建新题")
    parser.add_argument("--max-new", type=int, default=2, help="一次最多建几道新题")
    args = parser.parse_args()

    cfg = _load_config()
    mapping = _load_map()
    comp = cfg["competition"]
    built_challenges = set(mapping.values())

    list_resp = _platform(cfg, "GET", comp["fetch"]["list_url"])
    new_count = 0

    for cat in list_resp.get("data", []):
        for item in cat.get("corpus", []):
            if new_count >= args.max_new:
                break
            cid = str(item["id"])
            if not item.get("isOpen"):
                continue
            if item.get("hasSolved"):
                continue
            if cid in built_challenges:
                continue  # 已建过

            # 新题,查详情
            try:
                detail = _platform(cfg, "GET", comp["fetch"]["detail_url"], params={"exerciseId": item["id"]})["data"]
            except Exception as exc:
                print(f"[WARN] 拉详情 {cid} 失败: {exc}", file=sys.stderr)
                continue
            time.sleep(0.5)

            # 构造 origin
            desc = detail.get("description", "") or ""
            parts = [f"题目:{desc}"] if desc else []
            att = detail.get("attachment") or {}
            att_url = att.get("url") or ""
            if not att_url:
                files = att.get("files") or []
                if files:
                    att_url = files[0].get("url", "")
            if att_url:
                parts.append(f"附件下载: {att_url}")
            # 环境题:build-env 拿靶机
            target = None
            if detail.get("isNeedInit"):
                target = _build_env(cfg, item["id"])
            if target:
                parts.append(f"靶机: {target}")
            parts.append("flag 格式为 DASCTF{...},请解出并验证 flag")
            origin = "\n".join(parts)

            pid = _create_cairn_project(detail.get("name", cid), origin, detail.get("difficulty"))
            if pid:
                mapping[pid] = cid
                built_challenges.add(cid)
                new_count += 1
                print(f"[新建] {cid} {detail.get('name')} -> {pid} (难度={detail.get('difficulty')}, 靶机={'是' if target else '无'})")
            else:
                print(f"[FAIL] 建题失败 {cid} {detail.get('name')}")

    _save_map(mapping)
    print(f"sync 完成:本次新建 {new_count} 道,当前映射 {len(mapping)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
