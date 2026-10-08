#!/usr/bin/env python3
"""从某道题的导出里提取 flag。

用法:
  python extract_flag.py --project proj_001
  python extract_flag.py --project proj_001 --regex 'ctf\\{[^}]+\\}'   # 自定义 flag 格式

默认从 export?format=yaml 里正则匹配 flag{...}。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

SERVER = os.environ.get("CAIRN_SERVER", "http://127.0.0.1:8000")
DEFAULT_REGEX = r"(?:DASCTF|flag)\{[^}]+\}"


def _load_flag_regex() -> str:
    """优先从 scripts/competition.yaml 读 flag_regex。"""
    cfg = os.path.join(os.path.dirname(__file__), "competition.yaml")
    if not os.path.exists(cfg):
        return DEFAULT_REGEX
    try:
        import yaml  # type: ignore

        with open(cfg, encoding="utf-8") as f:
            return yaml.safe_load(f).get("flag_regex") or DEFAULT_REGEX
    except Exception:
        return DEFAULT_REGEX


def _get_text(path: str) -> str:
    req = urllib.request.Request(f"{SERVER}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}"


def main() -> int:
    parser = argparse.ArgumentParser(description="从 project 导出里提取 flag")
    parser.add_argument("--project", required=True, help="project id")
    parser.add_argument("--regex", default=None, help="flag 正则(默认读 competition.yaml)")
    args = parser.parse_args()
    regex = args.regex or _load_flag_regex()

    text = _get_text(f"/projects/{args.project}/export?format=yaml")
    flags = sorted(set(re.findall(regex, text)))
    if not flags:
        print("未找到 flag。可改用 --regex 指定格式,或直接看导出:")
        print("---")
        print(text)
        return 1
    for f in flags:
        print(f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
