#!/usr/bin/env python3
"""从安恒平台拉取赛题列表+详情,输出题目 JSON(供 create_projects.py 消费)。

用法:
  python fetch_challenges.py                    # 打印到 stdout
  python fetch_challenges.py --output challenges.json

输出格式(每个元素):
  {challenge_id, title, description, difficulty, score, isNeedInit, attachment_url}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(HERE, "competition.yaml")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        print(f"缺少配置文件 {CONFIG_PATH},先 cp 并填写。", file=sys.stderr)
        raise SystemExit(1)
    try:
        import yaml  # type: ignore

        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except ImportError:
        print("未安装 pyyaml,请用 `uv run --project cairn python scripts/fetch_challenges.py`", file=sys.stderr)
        raise


def _get_json(cfg: dict, path: str, params: dict | None = None, retries: int = 4):
    host = cfg["competition"]["host"]
    ak = cfg["competition"]["access_key"]
    url = host + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"X-Agent-AccessKey": ak, "User-Agent": BROWSER_UA}
    )
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 平台有限流(429),退避重试
            if exc.code == 429 and attempt < retries:
                wait = 2 * attempt
                print(f"[429] 限流,{wait}s 后重试 ({path}?{params})", file=sys.stderr)
                time.sleep(wait)
                continue
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description="拉取赛题列表+详情")
    parser.add_argument("--output", help="写题目 JSON 到文件(默认打印到 stdout)")
    args = parser.parse_args()

    import urllib.parse  # noqa: WPS433 (local import to keep module load light)

    cfg = _load_config()
    comp = cfg["competition"]

    # 1. 题目列表(按分类分组)
    list_resp = _get_json(cfg, comp["fetch"]["list_url"])
    categories = list_resp["data"]
    detail_path = comp["fetch"]["detail_url"]

    challenges = []
    for cat in categories:
        for item in cat.get("corpus", []):
            eid = item["id"]
            if not item.get("isOpen"):
                continue
            # 2. 题目详情(请求间留间隔,避免触发限流)
            try:
                detail = _get_json(cfg, detail_path, {"exerciseId": eid})["data"]
            except Exception as exc:
                print(f"[WARN] 拉详情失败 exerciseId={eid}: {exc}", file=sys.stderr)
                continue
            time.sleep(0.5)
            attachment_url = ""
            att = detail.get("attachment") or {}
            # 实际返回的是 attachment.url;文档示例是 attachment.files[].url,两种都兼容
            attachment_url = att.get("url") or ""
            if not attachment_url:
                files = att.get("files") or []
                if files:
                    attachment_url = files[0].get("url", "")
            challenges.append(
                {
                    "challenge_id": str(eid),
                    "title": detail.get("name", item.get("name", str(eid))),
                    "description": detail.get("description", "") or "",
                    "difficulty": detail.get("difficulty", ""),
                    "score": detail.get("score", ""),
                    "isNeedInit": bool(detail.get("isNeedInit", False)),
                    "attachment_url": attachment_url,
                }
            )

    text = json.dumps(challenges, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"已写入 {args.output}({len(challenges)} 道题)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())