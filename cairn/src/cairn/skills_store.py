from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_REGISTRY = ".registry.json"
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_ZIP_ENTRIES = 256
MAX_MEMBER_BYTES = 1 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
_READ_CHUNK = 64 * 1024


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
    if not name or name.startswith(".") or not _NAME_RE.match(name):
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
        if not child.is_dir() or child.name.startswith("."):
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
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("zip archive exceeds the upload size limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("zip archive is not valid") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise ValueError("zip archive has too many entries")
        _reject_zip_expansion(infos)
        names = [_zip_member_name(info) for info in infos]
        tops = {n.split("/", 1)[0] for n in names if "/" in n}
        if len(tops) != 1:
            raise ValueError("zip must contain exactly one top-level skill directory")
        skill_name = _validate_name(next(iter(tops)))
        if not any(n == f"{skill_name}/SKILL.md" for n in names):
            raise ValueError("zip skill directory must contain SKILL.md")
        root = skills_root()
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink():
            raise ValueError("skills directory must not be a symlink")
        dest = root / skill_name
        staging = Path(tempfile.mkdtemp(prefix=".upload-", dir=root))
        try:
            total = 0
            for info, name in zip(infos, names, strict=True):
                if name.endswith("/") or info.is_dir():
                    continue
                rel = name.split("/", 1)[1]
                target = _member_target(staging, rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                total = _copy_member(archive, info, target, total)
            if dest.exists() or dest.is_symlink():
                shutil.rmtree(dest)
            staging.rename(dest)
            staging = None
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
    return skill_name


def _zip_member_name(info: zipfile.ZipInfo) -> str:
    name = info.filename.replace("\\", "/")
    if name.startswith("/") or "\x00" in name:
        raise ValueError("zip member path escapes the skill directory")
    parts = PurePosixPath(name).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("zip member path escapes the skill directory")
    return "/".join(parts) + ("/" if name.endswith("/") else "")


def _reject_zip_expansion(infos: list[zipfile.ZipInfo]) -> None:
    total = 0
    for info in infos:
        declared = info.file_size
        if declared < 0 or declared > MAX_MEMBER_BYTES:
            raise ValueError("zip member exceeds the expanded size limit")
        total += declared
        if total > MAX_TOTAL_BYTES:
            raise ValueError("zip archive exceeds the expanded size limit")
        compressed = info.compress_size
        if declared and (compressed <= 0 or declared / compressed > MAX_COMPRESSION_RATIO):
            raise ValueError("zip member exceeds the compression ratio limit")


def _member_target(staging: Path, rel: str) -> Path:
    parts = PurePosixPath(rel).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("zip member path escapes the skill directory")
    target = staging.joinpath(*parts)
    root = staging.resolve()
    parent = target.parent.resolve() if target.parent.exists() else root
    if parent != root and not parent.is_relative_to(root):
        raise ValueError("zip member path escapes the skill directory")
    return target


def _copy_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path, total: int) -> int:
    written = 0
    try:
        source = archive.open(info)
    except RuntimeError as exc:
        raise ValueError("zip member cannot be extracted") from exc
    with source, target.open("wb") as out:
        while True:
            chunk = source.read(_READ_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            total += len(chunk)
            if written > MAX_MEMBER_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError("zip member exceeds the expanded size limit")
            out.write(chunk)
    return total


def enabled_skill_dirs() -> list[Path]:
    return [Path(m.path) for m in list_skills() if m.enabled]
