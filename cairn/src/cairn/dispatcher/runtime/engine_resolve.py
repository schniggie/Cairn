from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# worker type -> bare agent binary name
BINARY = {"claudecode": "claude", "codex": "codex", "gemini": "gemini", "opencode": "opencode", "pi": "pi"}
# host dependency tools the local engine needs (for project-knowledge queries)
TOOLS = ("graphify", "codegraph")
# binaries we will rewrite argv[0] for when seen bare (direct-argv drivers)
DIRECT_BINARIES = set(BINARY.values())


@dataclass(slots=True)
class Resolved:
    path: str
    launcher: str  # "direct" | "cmd" | "powershell"
    source: str    # "override" | "path"


def engines_config_path() -> Path:
    override = os.environ.get("CAIRN_HOME")
    base = Path(override).expanduser() if override else Path.home() / ".cairn"
    return base / "engines.json"


def load_overrides() -> dict:
    try:
        return json.loads(engines_config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_overrides(data: dict) -> None:
    path = engines_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def set_override(worker_type: str, path: str, launcher: str) -> None:
    data = load_overrides()
    data[worker_type] = {"path": path, "launcher": launcher}
    _write_overrides(data)


def remove_override(worker_type: str) -> None:
    data = load_overrides()
    if worker_type in data:
        del data[worker_type]
        _write_overrides(data)


# Back-compat private aliases
_engines_config_path = engines_config_path
_load_overrides = load_overrides


def _augmented_dirs() -> list[str]:
    dirs: list[str] = []
    try:
        out = subprocess.run(["npm", "config", "get", "prefix"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            prefix = out.stdout.strip()
            dirs.append(prefix)                       # Windows: shims live here
            dirs.append(str(Path(prefix) / "bin"))    # unix
    except (OSError, subprocess.SubprocessError):
        pass
    home = Path.home()
    dirs += ["/opt/homebrew/bin", "/usr/local/bin", str(home / ".local" / "bin")]
    nvm_bin = os.environ.get("NVM_BIN")
    if nvm_bin:
        dirs.append(nvm_bin)
    return [d for d in dict.fromkeys(dirs) if d and os.path.isdir(d)]


def augmented_path(base_path: str) -> str:
    extra = _augmented_dirs()
    parts = [p for p in [base_path] if p] + extra
    return os.pathsep.join(parts)


def _launcher_for(path: str) -> str:
    lower = path.lower()
    if lower.endswith((".cmd", ".bat")):
        return "cmd"
    if lower.endswith(".ps1"):
        return "powershell"
    return "direct"


def _windows_candidates(name: str) -> list[str]:
    return [f"{name}.cmd", f"{name}.exe", f"{name}.bat", f"{name}.ps1", name]


def resolve_engine(worker_type: str) -> Resolved | None:
    binary = BINARY.get(worker_type, worker_type)
    overrides = load_overrides()
    ov = overrides.get(worker_type) or overrides.get(binary)
    if isinstance(ov, dict) and ov.get("path"):
        path = ov["path"]
        return Resolved(path=path, launcher=ov.get("launcher") or _launcher_for(path), source="override")
    search = augmented_path(os.environ.get("PATH", ""))
    if os.name == "nt":
        for cand in _windows_candidates(binary):
            found = shutil.which(cand, path=search)
            if found:
                return Resolved(path=found, launcher=_launcher_for(found), source="path")
        return None
    found = shutil.which(binary, path=search)
    return Resolved(path=found, launcher="direct", source="path") if found else None


def launch_argv(resolved: Resolved, args: list[str]) -> list[str]:
    if resolved.launcher == "cmd":
        return ["cmd", "/c", resolved.path, *args]
    if resolved.launcher == "powershell":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", resolved.path, *args]
    return [resolved.path, *args]


def probe_engine(worker_type: str) -> dict:
    resolved = resolve_engine(worker_type)
    if resolved is None:
        return {"launchable": False, "path": None, "version": None, "source": None}
    version, launchable = None, False
    try:
        out = subprocess.run(launch_argv(resolved, ["--version"]),
                             capture_output=True, text=True, timeout=10)
        launchable = out.returncode == 0
        text = (out.stdout or out.stderr or "").strip()
        version = text.splitlines()[0] if text else None
    except (OSError, subprocess.SubprocessError):
        launchable = False
    return {"launchable": launchable, "path": resolved.path, "version": version, "source": resolved.source}


def probe_tool(name: str) -> dict:
    """Probe a bare host CLI tool (graphify / codegraph). Mirrors probe_engine
    but without override/BINARY mapping."""
    search = augmented_path(os.environ.get("PATH", ""))
    found = None
    if os.name == "nt":
        for cand in _windows_candidates(name):
            found = shutil.which(cand, path=search)
            if found:
                break
    else:
        found = shutil.which(name, path=search)
    if not found:
        return {"launchable": False, "path": None, "version": None}
    version, launchable = None, False
    try:
        argv = launch_argv(Resolved(path=found, launcher=_launcher_for(found), source="path"), ["--version"])
        out = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        launchable = out.returncode == 0
        text = (out.stdout or out.stderr or "").strip()
        version = text.splitlines()[0] if text else None
    except (OSError, subprocess.SubprocessError):
        launchable = False
    return {"launchable": launchable, "path": found, "version": version}
