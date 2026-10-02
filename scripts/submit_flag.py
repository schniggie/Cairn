#!/usr/bin/env python3
"""把 flag 提交到安恒平台。

用法:
  python submit_flag.py --challenge 10661 --flag 'flag{...}'
返回 0 表示提交成功(isCorrect=true)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
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
        print("未安装 pyyaml,请用 `uv run --project cairn python scripts/submit_flag.py`", file=sys.stderr)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="提交 flag")
    parser.add_argument("--challenge", required=True, help="赛题 exerciseId")
    parser.add_argument("--flag", required=True, help="flag 内容")
    args = parser.parse_args()

    cfg = _load_config()
    comp = cfg["competition"]
    submit = comp["submit"]
    url = comp["host"] + submit["url"]

    # 平台只认 flag 内容,不认 DASCTF{} / flag{} 外壳,提交前剥掉
    flag = args.flag
    shell = re.match(r"^(?:DASCTF|flag)\{([^}]*)\}$", flag)
    if shell:
        flag = shell.group(1)

    # 用 json.dumps 构造 body,避免 str.format 与 JSON 的 {} 冲突
    payload = {"exerciseId": int(args.challenge), "flag": flag}
    body = json.dumps(payload)

    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Agent-AccessKey": comp["access_key"],
            "User-Agent": BROWSER_UA,
        },
        method=submit.get("method", "POST"),
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        print(f"提交失败(status={exc.code}): {raw[:300]}")
        return 1

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print(f"响应非 JSON: {raw[:300]}")
        return 1

    is_correct = parsed.get("data", {}).get(submit.get("success_field", "isCorrect"))
    if parsed.get("code") == "00000" and is_correct:
        print(f"✅ flag 提交成功: {args.flag}")
        return 0
    print(f"提交未通过: code={parsed.get('code')} message={parsed.get('message')} data={parsed.get('data')}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())