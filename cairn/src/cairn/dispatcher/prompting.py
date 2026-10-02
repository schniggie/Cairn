from __future__ import annotations

import json
from importlib import resources
from typing import Any


def load_prompt(group: str, name: str) -> str:
    return resources.files("cairn.dispatcher.prompts").joinpath(group).joinpath(name).read_text(encoding="utf-8")


def render_prompt(template: str, replacements: dict[str, str]) -> str:
    text = template
    for key, value in replacements.items():
        text = text.replace("{" + key + "}", value)
    return text


def format_fact_ids(fact_ids: list[str]) -> str:
    return format_json_block(fact_ids)


def format_open_intents(intents: list[dict[str, Any]]) -> str:
    return format_json_block(intents)


def format_hints(hints: list[dict[str, Any]]) -> str:
    return format_json_block(hints)


def format_json_block(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def format_skills(skills) -> str:
    if not skills:
        return ""
    lines = [
        "## Available Skills (prefer these)",
        "You have these skills installed at .claude/skills/<name>/SKILL.md. When a task matches "
        "a skill, READ its SKILL.md and follow it; prefer these skills over ad-hoc approaches.",
        "",
    ]
    for skill in skills:
        description = (skill.description or "").strip()
        lines.append(f"- {skill.name}: {description}  (.claude/skills/{skill.name}/SKILL.md)")
    return "\n".join(lines)


_PK_USAGE = {
    "src-repo": "source code: read / grep `./project/src-repo`",
    "codegraph-out": "code graph: query with the `codegraph` CLI over `./project/codegraph-out`",
    "graphify-out": "domain knowledge graph: run `graphify query` over `./project/graphify-out`",
    "scan-out": "static scan findings: read `./project/scan-out`",
    "docs-out": "product docs: read `./project/docs-out`",
}
_PK_ORDER = ["src-repo", "docs-out", "graphify-out", "scan-out", "codegraph-out"]


def format_project_knowledge(project_root, present_subdirs) -> str:
    if not project_root or not present_subdirs:
        return ""
    present = set(present_subdirs)
    items = [_PK_USAGE[name] for name in _PK_ORDER if name in present and name in _PK_USAGE]
    if not items:
        return ""
    lines = [
        "## Project Knowledge (prior analysis, read-only at ./project)",
        "Reuse these prior results to gain context. If a query tool is missing, read the files directly.",
        "",
    ]
    lines.extend(f"- {item}" for item in items)
    return "\n".join(lines)
