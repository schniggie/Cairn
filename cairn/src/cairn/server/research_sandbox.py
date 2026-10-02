"""Execution boundary for research: a real bwrap filesystem sandbox.

A per-session workspace and the authorized repository are the only writable/read
paths the model sees. The process runs in its own user/PID/network/cgroup namespace
with all Linux capabilities dropped and no access to the operator's home or secrets.
Outbound network is kept usable so black-box requests can reach the authorized target
AND the model can reach its own gateway, but nothing else about the host is exposed.

If bwrap is unavailable the worker refuses to run tool-enabled research instead of
silently falling back to an uncontained process on the operator's host.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

LOG = logging.getLogger(__name__)

# System read-only bind roots the model needs to run the CLI (its dynamic loader,
# libs, config). We never bind /home, /root, /run, or host /tmp; those namespaces
# stay private so the operator's files are out of reach.
_SYSTEM_RO_ROOTS = ("/usr", "/etc")

# usrmerge is default on modern Debian/Ubuntu-derived systems: /bin,/sbin,/lib,/lib64
# are symlinks into /usr. Recreate those top-level symlinks inside the sandbox so the
# loader and tools resolve their canonical paths.
_USRMERGE_SYMLINKS = {
    "usr/bin": "/bin",
    "usr/sbin": "/sbin",
    "usr/lib": "/lib",
    "usr/lib64": "/lib64",
}

# Namespace + capability flags. We keep network shared (``--share-net``) so the model
# can reach its gateway and the authorized target; everything else is unshared.
_BWRAP_COMMON = (
    "--die-with-parent",
    "--new-session",
    "--unshare-all",
    "--share-net",
    "--cap-drop",
    "ALL",
)


def bwrap_available() -> bool:
    return shutil.which("bwrap") is not None


def _resolve_ro_root(value: str) -> Path | None:
    path = Path(value)
    if path.is_dir():
        return path
    return None


def _seed_claude_config(src_home: Path | None, dest_home: Path) -> None:
    """Copy the operator's Claude settings into the sandbox HOME so the CLI inherits
    the same API endpoint / auth / model selection it would use on the host, without
    binding the operator's whole home into the sandbox.

    Only the plain JSON settings files Claude reads from ``$HOME`` are copied, not the
    host home tree, history, or credentials store. Files are copied into the writable
    sandbox home (under the session workspace). If nothing is found, the sandbox still
    gets a writable empty HOME (the CLI may pick up further settings via env)."""
    if src_home is None or not src_home.is_dir():
        return
    candidates = [
        src_home / ".claude.json",
        src_home / ".claude" / "settings.json",
        src_home / ".claude" / "settings.local.json",
    ]
    for src in candidates:
        if not src.is_file():
            continue
        rel = src.relative_to(src_home)
        dst = dest_home / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            dst.chmod(0o600)
            LOG.debug("seeded claude config %s -> %s", src, dst)
        except OSError as exc:
            LOG.warning("could not seed claude config %s: %s", src, exc)


def _config_candidates(src_home: Path) -> list[Path]:
    # settings.local.json is deliberately NOT seeded: it typically holds host-specific
    # permission allow-lists (absolute host paths / OS-specific gates) that make no
    # sense inside the sandbox and would block the research model's general Bash/curl.
    # Auth + provider selection live in settings.json and are still seeded.
    return [
        src_home / ".claude.json",
        src_home / ".claude" / "settings.json",
    ]


def _prepare_private_config(src_home: Path | None, config_root: Path) -> Path:
    """Stage the operator's Claude settings into a PRIVATE directory OUTSIDE the
    session workspace, then return its path (the caller RO-binds it into the sandbox).

    The model must never be able to edit its own execution config, so this directory
    is never mounted writable. Source paths that are symbolic links or contain path
    traversal are refused, and a missing-totally-empty config raises (we never run a
    tool-enabled research step silently with empty credentials/settings).
    """
    config_root = Path(config_root)
    dest = config_root / "claude-config"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    if src_home is None or not src_home.is_dir():
        raise RuntimeError("缺少 Claude Code 用户配置目录，无法继承执行配置")
    src_home = src_home.resolve()
    seeded = False
    for src in _config_candidates(src_home):
        if not src.exists():
            continue
        # Refuse symlinks (attack vector: a link out of the private config tree).
        if src.is_symlink():
            raise RuntimeError(f"Claude 配置文件禁止为符号链接：{src}")
        if not src.is_file():
            continue
        try:
            rel = src.relative_to(src_home)
        except ValueError as exc:
            raise RuntimeError(f"Claude 配置文件路径非法：{src}") from exc
        if any(part == ".." or os.path.isabs(part) for part in rel.parts):
            raise RuntimeError(f"Claude 配置文件路径越界：{src}")
        # Flatten the known Claude files into the config-dir ROOT so the CLI finds them:
        # it reads user settings from $CLAUDE_CONFIG_DIR/settings.json (the default
        # config dir IS ~/.claude) and the global state file from $HOME/.claude.json.
        # Mirroring ~/.claude/ under CLAUDE_CONFIG_DIR would hide settings.json one
        # level down from where the CLI reads it.
        if src.name == ".claude.json":
            dst = dest / ".claude.json"
        else:
            dst = dest / src.name  # settings.json
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        dst.chmod(0o600)
        seeded = True
        LOG.debug("staged claude config %s -> %s", src, dst)
    if not seeded:
        raise RuntimeError("未找到任何 Claude Code 配置文件；拒绝在空配置下执行研究步骤")
    return dest



def build_sandbox(
    *,
    workspace: Path,
    repo: str | None,
    argv: list[str],
    claude_bin: str = "claude",
    claude_config_src: Path | None = None,
    config_root: Path | None = None,
) -> list[str]:
    """Prepend a bwrap invocation that confines ``argv`` to workspace + repo.

    ``workspace`` is mounted writable at ``/workspace``; the authorized ``repo`` is
    mounted read-only at ``/repo``. The CLI binary is resolved to an absolute path and
    its parent bound so a locally installed CLI can be used. The operator's Claude
    settings are staged into a PRIVATE directory (``config_root``, outside the
    workspace) and mounted READ-ONLY at ``/claude-config`` with ``CLAUDE_CONFIG_DIR``
    pointed at it, so the model can never edit the settings that govern its own
    execution. A separate writable cache/tmp area lives inside the workspace. Raises
    if bwrap is unavailable, the workspace is unusable, or required config is missing.
    """
    if shutil.which("bwrap") is None:
        raise RuntimeError(
            "研究执行需要 bubblewrap 隔离；请安装 bwrap（sudo apt install bubblewrap）后重试"
        )
    workspace = Path(workspace)
    if not workspace.is_dir():
        workspace.mkdir(parents=True, exist_ok=True)
    workspace = workspace.resolve()
    workspace.chmod(0o700)

    resolved_bin = shutil.which(claude_bin) if claude_bin != "claude" else shutil.which("claude")
    if resolved_bin is None:
        raise RuntimeError(f"未找到可执行的 {claude_bin}")
    claude_path = Path(resolved_bin)  # keep the symlink path (do not resolve to target)

    bind_dirs: set[str] = set()
    command: list[str] = ["bwrap", *_BWRAP_COMMON]
    for root in _SYSTEM_RO_ROOTS:
        resolved = _resolve_ro_root(root)
        if resolved is not None and not resolved.is_symlink():
            command += ["--ro-bind", str(resolved), str(resolved)]
            bind_dirs.add(str(resolved))
    for target, dest in _USRMERGE_SYMLINKS.items():
        command += ["--symlink", target, dest]
    command += ["--proc", "/proc", "--dev", "/dev"]
    command += ["--tmpfs", "/tmp"]
    command += ["--bind", str(workspace), "/workspace"]
    if repo:
        repo_path = Path(repo)
        if not repo_path.is_dir():
            raise RuntimeError(f"授权代码目录不存在或不是目录：{repo}")
        repo_path = repo_path.resolve()
        if repo_path.is_symlink():
            raise RuntimeError(f"授权代码目录禁止为符号链接：{repo}")
        command += ["--ro-bind", str(repo_path), "/repo"]

    # Make the CLI binary and its resolved target reachable (a symlinked launcher).
    claude_real = claude_path.resolve()
    for bind in (claude_path.parent, claude_real.parent):
        if str(bind) not in bind_dirs:
            command += ["--ro-bind", str(bind), str(bind)]
            bind_dirs.add(str(bind))

    home = workspace / ".sandbox-home"
    home.mkdir(parents=True, exist_ok=True)
    # Separate writable cache/tmp/session areas INSIDE the workspace; the private
    # config is read-only, so anything the CLI may want to write goes here instead.
    cache = workspace / ".sandbox-cache"
    cache.mkdir(parents=True, exist_ok=True)
    sandbox_home = Path("/workspace/.sandbox-home")
    sandbox_cache = Path("/workspace/.sandbox-cache")
    command += [
        "--setenv",
        "HOME",
        str(sandbox_home),
        "--setenv",
        "PATH",
        "/usr/local/bin:/usr/bin:/bin",
        "--setenv",
        "XDG_CACHE_HOME",
        str(sandbox_cache / ".cache"),
        "--setenv",
        "XDG_CONFIG_HOME",
        str(sandbox_cache / ".config"),
        "--setenv",
        "XDG_DATA_HOME",
        str(sandbox_cache / ".data"),
        "--chdir",
        "/workspace",
    ]

    # Stage private config OUTSIDE the workspace. The config DIR the CLI reads is made
    # WRITABLE (claude creates runtime dirs like ``session-env`` in it), but the actual
    # settings files (.claude.json, settings.json) are individually RO-bound on top so
    # the model can never edit the settings that govern its own execution (item ①).
    if claude_config_src is not None:
        if config_root is None:
            config_root = workspace.parent / ".cairn-sandbox-private"
        private_config = _prepare_private_config(claude_config_src, config_root)
        # writable runtime base for the config dir, OUTSIDE the workspace
        runtime = config_root / "claude-config-runtime"
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        command += ["--bind", str(runtime), "/claude-config"]
        for name in (".claude.json", "settings.json"):
            src = private_config / name
            if src.is_file():
                command += ["--ro-bind", str(src), f"/claude-config/{name}"]
        command += ["--setenv", "CLAUDE_CONFIG_DIR", "/claude-config"]

    full_argv = [claude_bin] + list(argv)
    command += ["--", *full_argv]
    return command


def run_sandboxed(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` (a full bwrap prefixed command) and return its result."""
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    return result