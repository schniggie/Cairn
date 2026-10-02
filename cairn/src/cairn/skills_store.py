from __future__ import annotations

import io
import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

import yaml

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_REGISTRY = ".registry.json"


@dataclass(slots=True)
class SkillMeta:
    name: str
    description: str
    enabled: bool
    path: str


def _cairn_home() -> Path:
    override = os.environ.get("CAIRN_HOME")
    return Path(override).expanduser() if override else Path.home() / ".cairn"


def skills_root() -> Path:
    return _cairn_home() / "skills"


def _validate_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"invalid skill name: {name!r}")
    return name


def _registry_path() -> Path:
    return skills_root() / _REGISTRY


def _load_registry() -> dict:
    try:
        return json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_registry(data: dict) -> None:
    root = skills_root()
    root.mkdir(parents=True, exist_ok=True)
    tmp = _registry_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, _registry_path())


def seed_if_empty(repo_skills_dir: Path) -> None:
    root = skills_root()
    if root.exists() and any(p.is_dir() for p in root.iterdir()):
        return
    if not Path(repo_skills_dir).is_dir():
        return
    root.mkdir(parents=True, exist_ok=True)
    for child in Path(repo_skills_dir).iterdir():
        if child.is_dir() and (child / "SKILL.md").is_file():
            shutil.copytree(child, root / child.name, dirs_exist_ok=True)


def _parse_description(skill_md_path: Path) -> str:
    try:
        text = skill_md_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return ""
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return ""
    desc = meta.get("description", "") if isinstance(meta, dict) else ""
    return str(desc) if desc else ""


def list_skills() -> list[SkillMeta]:
    root = skills_root()
    if not root.is_dir():
        return []
    reg = _load_registry()
    out: list[SkillMeta] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue
        enabled = bool(reg.get(child.name, {}).get("enabled", True))
        out.append(SkillMeta(name=child.name, description=_parse_description(skill_md),
                             enabled=enabled, path=str(child)))
    return out


def _skill_dir(name: str) -> Path:
    return skills_root() / _validate_name(name)


def read_skill_md(name: str) -> str:
    path = _skill_dir(name) / "SKILL.md"
    if not path.is_file():
        raise FileNotFoundError(name)
    return path.read_text(encoding="utf-8")


def write_skill_md(name: str, content: str) -> None:
    d = _skill_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")


def create_skill(name: str, skill_md: str) -> None:
    d = _skill_dir(name)
    if d.exists():
        raise ValueError(f"skill already exists: {name}")
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(skill_md, encoding="utf-8")


def delete_skill(name: str) -> None:
    shutil.rmtree(_skill_dir(name), ignore_errors=True)
    reg = _load_registry()
    if name in reg:
        del reg[name]
        _save_registry(reg)


def set_enabled(name: str, enabled: bool) -> None:
    _validate_name(name)
    reg = _load_registry()
    reg.setdefault(name, {})["enabled"] = bool(enabled)
    _save_registry(reg)


def import_zip(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if not n.startswith("/") and ".." not in n]
        tops = {n.split("/", 1)[0] for n in names if "/" in n}
        if len(tops) != 1:
            raise ValueError("zip must contain exactly one top-level skill directory")
        skill_name = _validate_name(next(iter(tops)))
        if not any(n == f"{skill_name}/SKILL.md" for n in names):
            raise ValueError("zip skill directory must contain SKILL.md")
        dest = skills_root() / skill_name
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        for n in names:
            if n.endswith("/"):
                continue
            rel = n.split("/", 1)[1] if "/" in n else n
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(n))
    return skill_name


def enabled_skill_dirs() -> list[Path]:
    return [Path(m.path) for m in list_skills() if m.enabled]
