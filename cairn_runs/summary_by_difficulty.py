#!/usr/bin/env python3
"""Summarize xbow benchmark results by difficulty level.

Environment variables:
    VALIDATION_BENCHMARKS_DIR  (optional) Path to validation-benchmarks/benchmarks,
                                default auto-detects relative to Cairn repo root
"""
import json
import os
import sys
from pathlib import Path


def _find_benchmarks_dir() -> Path:
    env = os.environ.get("VALIDATION_BENCHMARKS_DIR")
    if env:
        return Path(env)
    cairn_root = Path(__file__).parent.parent  # cairn_runs/ -> Cairn/
    sibling = cairn_root.parent / "validation-benchmarks" / "benchmarks"
    if sibling.exists():
        return sibling
    sys.exit("ERROR: Cannot find validation-benchmarks/benchmarks.\n"
             "  Set VALIDATION_BENCHMARKS_DIR or place it alongside the Cairn/ directory.")


BENCHMARKS_DIR = _find_benchmarks_dir()
RESULTS_DIR = Path(__file__).parent.parent / ".cairn_runs" / "results"

results = {}
for tsv in sorted(RESULTS_DIR.glob("batch_*.tsv")):
    for line in tsv.read_text().strip().split("\n")[1:]:
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) < 6:
            continue
        bench = cols[0]
        new_entry = {"status": cols[1], "passed": cols[2] == "True"}
        if bench in results:
            old = results[bench]
            if old["passed"]:
                continue
            if old["status"] in ("completed", "active") and new_entry["status"] == "start_failed":
                continue
        results[bench] = new_entry

by_difficulty = {}
for i in range(1, 105):
    bid = f"XBEN-{i:03d}-24"
    meta_path = BENCHMARKS_DIR / bid / "benchmark.json"
    if not meta_path.exists():
        continue
    meta = json.loads(meta_path.read_text())
    diff = str(meta.get("level", "unknown"))
    tags = meta.get("tags", [])
    if diff not in by_difficulty:
        by_difficulty[diff] = {"total": 0, "pass": 0, "fail": 0, "timeout": 0, "other": 0, "items": []}
    by_difficulty[diff]["total"] += 1
    r = results.get(bid)
    if r is None:
        by_difficulty[diff]["other"] += 1
        by_difficulty[diff]["items"].append((bid, "NOT_RUN"))
    elif r["passed"]:
        by_difficulty[diff]["pass"] += 1
    elif r["status"] in ("active", "timeout"):
        by_difficulty[diff]["timeout"] += 1
        by_difficulty[diff]["items"].append((bid, "TIMEOUT"))
    else:
        by_difficulty[diff]["fail"] += 1
        by_difficulty[diff]["items"].append((bid, r["status"]))

print("=" * 60)
print("  按难度分类的做题情况 (Cairn + qwen3.7-max)")
print("=" * 60)

for diff in sorted(by_difficulty.keys()):
    d = by_difficulty[diff]
    rate = d["pass"] / max(1, d["total"]) * 100
    p, t, to, f, o = d["pass"], d["total"], d["timeout"], d["fail"], d["other"]
    print(f"\n  [Level {diff}] {p}/{t} PASS ({rate:.0f}%)")
    print(f"    通过: {p} | 超时: {to} | 失败: {f} | 未跑: {o}")
    if d["items"]:
        print("    未通过:")
        for bid, status in d["items"]:
            print(f"      {bid}: {status}")

total_pass = sum(d["pass"] for d in by_difficulty.values())
total_all = sum(d["total"] for d in by_difficulty.values())
print(f"\n{'=' * 60}")
print(f"  总计: {total_pass}/{total_all} PASS ({total_pass/max(1,total_all)*100:.1f}%)")
print("=" * 60)
