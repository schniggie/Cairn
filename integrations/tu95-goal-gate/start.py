#!/usr/bin/env python3
"""Cairn 项目统一控制器。

这是整个项目的单一入口：用它启动、管理、维护 Cairn，不需要再记忆一长串
`uv run --project cairn cairn ...` 命令，也不依赖 screen。纯标准库实现，
可直接 `python3 start.py` 运行。

用法::

    python3 start.py                 # 终端交互时进入控制面板；非交互等同 `up`
    python3 start.py panel           # 交互式控制面板（启动/停止/重启/日志/自检）
    python3 start.py up              # 前台启动 server+dispatcher（Ctrl+C 停止）
    python3 start.py up -d           # 后台启动，写 pidfile + 日志
    python3 start.py status          # 查看运行状态与健康检查
    python3 start.py logs -f         # 跟随查看后台日志
    python3 start.py restart -d      # 后台重启
    python3 start.py stop            # 停止后台实例
    python3 start.py doctor          # 环境自检（uv / 配置 / worker CLI）
    python3 start.py test            # 跑回归测试
    python3 start.py --help          # 查看全部命令

设计约定（便于长期不动）：
  * 所有实际功能都委托给已验证的 `cairn` CLI（通过 `uv run`），本文件只做编排，
    不重复实现服务端逻辑，因此 CLI 演进时这里通常无需改动。
  * 命令通过 @command 注册表登记，新增功能 = 写一个函数并加一行装饰器，见文件末尾
    “扩展方式”。
  * 后台实例用 workspace 下的 pidfile 管理，与 db.py 的 CAIRN_WORKSPACE 保持一致。
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

# ── 路径与默认值 ────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent
CAIRN_PROJECT = "cairn"  # uv 子项目目录（相对 REPO_ROOT）
DEFAULT_CONFIG = REPO_ROOT / "dispatch.yaml"
EXAMPLE_CONFIG = REPO_ROOT / "dispatch.example.yaml"
WORKSPACE = Path(os.environ.get("CAIRN_WORKSPACE") or (REPO_ROOT / "workspace"))
PID_FILE = WORKSPACE / "cairn.pid"
LOG_FILE = WORKSPACE / "cairn.log"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000

# 命令注册表：name -> (handler, help)。handler 接收剩余的命令行参数列表。
COMMANDS: dict[str, tuple[Callable[[list[str]], int], str]] = {}


def command(name: str, help: str) -> Callable[[Callable[[list[str]], int]], Callable[[list[str]], int]]:
    def deco(fn: Callable[[list[str]], int]) -> Callable[[list[str]], int]:
        COMMANDS[name] = (fn, help)
        return fn

    return deco


# ── 通用工具 ────────────────────────────────────────────────────────────────
def _uv() -> str:
    path = shutil.which("uv")
    if not path:
        sys.exit(
            "未找到 uv。请先安装：curl -LsSf https://astral.sh/uv/install.sh | sh\n"
            "参考 https://docs.astral.sh/uv/getting-started/installation/"
        )
    return path


def _cairn_cmd(*args: str) -> list[str]:
    return [_uv(), "run", "--project", CAIRN_PROJECT, "cairn", *args]


def _run(cmd: list[str], **kwargs) -> int:
    """在仓库根目录同步执行命令，继承标准输入输出。"""
    kwargs.setdefault("cwd", str(REPO_ROOT))
    return subprocess.call(cmd, **kwargs)


def _health_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "::") else host


def _probe_health(host: str, port: int, timeout: float = 1.0) -> bool:
    """探测 /projects 是否可达（<500 即视为存活）。"""
    url = f"http://{_health_host(host)}:{port}/projects"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (urllib.error.URLError, OSError):
        return False


def _read_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _running_pid() -> int | None:
    pid = _read_pid()
    if pid is not None and _pid_alive(pid):
        return pid
    return None


def ensure_workspace() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)


def ensure_config(config: Path) -> None:
    """dispatch.yaml 不存在时从 example 复制，保证 `cairn launch` 能启动。"""
    if config.exists():
        return
    if config == DEFAULT_CONFIG and EXAMPLE_CONFIG.exists():
        shutil.copyfile(EXAMPLE_CONFIG, config)
        print(f"已从 {EXAMPLE_CONFIG.name} 生成 {config.name}；请在 Web UI 或直接编辑该文件配置 worker。")
        return
    sys.exit(f"配置文件不存在：{config}（可先运行 `python3 start.py config` 生成）")


def _common_run_parser(prog: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"start.py {prog}", add_help=True)
    p.add_argument("--host", default=DEFAULT_HOST, help=f"绑定地址（默认 {DEFAULT_HOST}）")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"绑定端口（默认 {DEFAULT_PORT}）")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="dispatcher 配置路径")
    return p


# ── 后台进程管理 ────────────────────────────────────────────────────────────
def _spawn_background(host: str, port: int, config: Path) -> int:
    running = _running_pid()
    if running is not None:
        print(f"已有后台实例在运行 pid={running}；如需重启用 `python3 start.py restart -d`。")
        return 1
    ensure_workspace()
    ensure_config(config)
    cmd = _cairn_cmd("launch", "--config", str(config), "--host", host, "--port", str(port))
    log = open(LOG_FILE, "ab")
    # 独立会话 → 子进程成为进程组组长，停止时可整组发信号优雅收尾。
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))
    print(f"后台启动中 pid={proc.pid} 日志={LOG_FILE}")
    # 等待健康，最长约 25s；期间进程若退出则报错。
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"启动失败，进程已退出 returncode={proc.returncode}；查看日志：python3 start.py logs")
            _clear_pidfile()
            return 1
        if _probe_health(host, port):
            print(f"Cairn 已就绪：http://{_health_host(host)}:{port}")
            return 0
        time.sleep(0.4)
    print("等待健康超时，进程仍在运行；请查看 `python3 start.py logs` 排查。")
    return 1


def _clear_pidfile() -> None:
    try:
        PID_FILE.unlink()
    except OSError:
        pass


def _signal_group(pid: int, sig: int) -> bool:
    try:
        os.killpg(os.getpgid(pid), sig)
    except ProcessLookupError:
        return False
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            return False
    return True


def _stop_background(grace: float = 12.0) -> int:
    pid = _running_pid()
    if pid is None:
        print("没有正在运行的后台实例。")
        _clear_pidfile()
        return 0
    print(f"正在停止 pid={pid} ...")
    # 先发 SIGINT 触发 KeyboardInterrupt，让 launch 的 finally 优雅收尾（等任务结束、关服务）。
    _signal_group(pid, signal.SIGINT)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            _clear_pidfile()
            print("已停止。")
            return 0
        time.sleep(0.3)
    # 优雅超时 → SIGTERM → 最后 SIGKILL。
    _signal_group(pid, signal.SIGTERM)
    time.sleep(2)
    if _pid_alive(pid):
        _signal_group(pid, signal.SIGKILL)
    _clear_pidfile()
    print("已强制停止。")
    return 0


# ── 命令 ────────────────────────────────────────────────────────────────────
@command("up", "启动 server+dispatcher（默认前台；-d 后台）")
def cmd_up(argv: list[str]) -> int:
    p = _common_run_parser("up")
    p.add_argument("-d", "--detach", action="store_true", help="后台运行并写 pidfile/日志")
    args = p.parse_args(argv)
    if args.detach:
        return _spawn_background(args.host, args.port, args.config)
    ensure_workspace()
    ensure_config(args.config)
    # 前台直接把控制权交给 cairn launch，Ctrl+C 可优雅停止。
    return _run(_cairn_cmd("launch", "--config", str(args.config), "--host", args.host, "--port", str(args.port)))


@command("stop", "停止后台实例（优雅 SIGINT → SIGTERM → SIGKILL）")
def cmd_stop(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="start.py stop").parse_args(argv)
    return _stop_background()


@command("restart", "重启：先停后台，再启动（-d 后台，默认前台）")
def cmd_restart(argv: list[str]) -> int:
    p = _common_run_parser("restart")
    p.add_argument("-d", "--detach", action="store_true", help="后台运行")
    args = p.parse_args(argv)
    _stop_background()
    if args.detach:
        return _spawn_background(args.host, args.port, args.config)
    ensure_workspace()
    ensure_config(args.config)
    return _run(_cairn_cmd("launch", "--config", str(args.config), "--host", args.host, "--port", str(args.port)))


@command("status", "查看后台进程与服务健康状态")
def cmd_status(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="start.py status")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = p.parse_args(argv)
    pid = _running_pid()
    if pid is not None:
        print(f"后台进程：运行中 pid={pid}")
    elif PID_FILE.exists():
        print("后台进程：pidfile 存在但进程已退出（陈旧）")
    else:
        print("后台进程：未运行（或以前台方式运行）")
    healthy = _probe_health(args.host, args.port)
    url = f"http://{_health_host(args.host)}:{args.port}"
    print(f"服务健康：{'可达' if healthy else '不可达'} {url}")
    print(f"工作目录：{WORKSPACE}")
    print(f"数据库：  {WORKSPACE / 'cairn.db'}")
    return 0 if healthy else 1


@command("logs", "查看后台日志（-f 跟随，-n 行数）")
def cmd_logs(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="start.py logs")
    p.add_argument("-f", "--follow", action="store_true", help="持续跟随输出")
    p.add_argument("-n", "--lines", type=int, default=200, help="显示末尾行数（默认 200）")
    args = p.parse_args(argv)
    if not LOG_FILE.exists():
        print(f"暂无日志文件：{LOG_FILE}（后台启动后才会生成）")
        return 1
    cmd = ["tail"]
    if args.follow:
        cmd.append("-f")
    cmd += ["-n", str(args.lines), str(LOG_FILE)]
    try:
        return _run(cmd)
    except KeyboardInterrupt:
        return 0


@command("serve", "只启动 API/Web 服务（不带 dispatcher，前台）")
def cmd_serve(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="start.py serve")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = p.parse_args(argv)
    ensure_workspace()
    return _run(_cairn_cmd("serve", "--host", args.host, "--port", str(args.port)))


@command("dispatch", "只启动 dispatcher（需服务已在运行，前台）")
def cmd_dispatch(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="start.py dispatch")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = p.parse_args(argv)
    ensure_workspace()
    ensure_config(args.config)
    return _run(_cairn_cmd("dispatch", "--config", str(args.config)))


@command("healthcheck", "只跑 worker 启动健康检查后退出")
def cmd_healthcheck(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="start.py healthcheck")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = p.parse_args(argv)
    ensure_config(args.config)
    return _run(_cairn_cmd("dispatch", "--config", str(args.config), "--startup-healthcheck-only"))


@command("test", "运行回归测试（其余参数透传给 pytest）")
def cmd_test(argv: list[str]) -> int:
    return _run([_uv(), "run", "--project", CAIRN_PROJECT, "--group", "dev", "pytest", *argv])


@command("config", "若缺失则从 example 生成 dispatch.yaml，并打印关键路径")
def cmd_config(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="start.py config").parse_args(argv)
    ensure_config(DEFAULT_CONFIG)
    print(f"配置文件：  {DEFAULT_CONFIG}")
    print(f"模型目录：  {DEFAULT_CONFIG.with_name(DEFAULT_CONFIG.stem + '.models.yaml')}")
    print(f"工作目录：  {WORKSPACE}")
    return 0


@command("doctor", "环境自检：uv / 配置 / worker CLI / 服务可达性")
def cmd_doctor(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="start.py doctor")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = p.parse_args(argv)
    ok = True

    def check(label: str, passed: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and passed
        print(f"[{'OK ' if passed else 'XX '}] {label}: {detail}")

    uv_path = shutil.which("uv")
    check("uv", bool(uv_path), uv_path or "未安装（必需）")
    check("Python", sys.version_info >= (3, 12), sys.version.split()[0] + "（需 >=3.12）")
    check("配置 dispatch.yaml", DEFAULT_CONFIG.exists(), str(DEFAULT_CONFIG) if DEFAULT_CONFIG.exists() else "缺失，运行 `python3 start.py config` 生成")
    codex = shutil.which("codex")
    claude = shutil.which("claude")
    check("worker CLI", bool(codex or claude), f"codex={codex or '-'} claude={claude or '-'}（至少一个）")
    try:
        ensure_workspace()
        writable = os.access(WORKSPACE, os.W_OK)
    except OSError:
        writable = False
    check("工作目录可写", writable, str(WORKSPACE))
    check("服务可达", _probe_health(args.host, args.port), f"http://{_health_host(args.host)}:{args.port}（未启动则正常为不可达）")
    return 0 if ok else 1


@command("clean", "清理 workspace 运行产物（默认保留 DB；--all 连 DB 一并删）")
def cmd_clean(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="start.py clean")
    p.add_argument("--all", action="store_true", help="连同数据库整体清空 workspace")
    p.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    args = p.parse_args(argv)
    if _running_pid() is not None:
        print("检测到后台实例仍在运行，请先 `python3 start.py stop`。")
        return 1
    if not WORKSPACE.exists():
        print("workspace 不存在，无需清理。")
        return 0
    if args.all:
        targets = [WORKSPACE]
        desc = f"整个 workspace（含数据库）：{WORKSPACE}"
    else:
        targets = [WORKSPACE / "prompts", WORKSPACE / "pi", LOG_FILE]
        desc = "运行产物 prompts/ pi/ cairn.log（保留 cairn.db）"
    if not args.yes:
        reply = input(f"将删除 {desc}\n确认？[y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("已取消。")
            return 0
    for t in targets:
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
        elif t.exists():
            t.unlink()
    print("清理完成。")
    return 0


# ── 交互式面板 ──────────────────────────────────────────────────────────────
@command("panel", "交互式控制面板：启动/停止/重启/状态/日志/自检")
def cmd_panel(argv: list[str]) -> int:
    p = _common_run_parser("panel")
    args = p.parse_args(argv)
    url = f"http://{_health_host(args.host)}:{args.port}"

    def do_restart() -> int:
        _stop_background()
        return _spawn_background(args.host, args.port, args.config)

    def do_open() -> int:
        import webbrowser

        webbrowser.open(url)
        print(f"已在浏览器打开 {url}")
        return 0

    # (按键, 说明, 动作)。动作均针对后台守护实例，面板本身不阻塞。
    menu: list[tuple[str, str, Callable[[], int]]] = [
        ("1", "启动（后台守护）", lambda: _spawn_background(args.host, args.port, args.config)),
        ("2", "停止", lambda: _stop_background()),
        ("3", "重启（后台守护）", do_restart),
        ("4", "状态详情", lambda: cmd_status(["--host", args.host, "--port", str(args.port)])),
        ("5", "查看日志（末尾 60 行）", lambda: cmd_logs(["-n", "60"])),
        ("6", "跟随日志（Ctrl+C 返回面板）", lambda: cmd_logs(["-f"])),
        ("7", "环境自检 doctor", lambda: cmd_doctor(["--host", args.host, "--port", str(args.port)])),
        ("o", "浏览器打开 Web UI", do_open),
    ]
    print("Cairn 控制面板（q 退出；退出不影响后台进程）")
    while True:
        pid = _running_pid()
        healthy = _probe_health(args.host, args.port)
        proc_desc = f"运行中 pid={pid}" if pid is not None else "未运行"
        net_desc = f"{'可达' if healthy else '不可达'} {url}"
        print(f"\n── 守护进程：{proc_desc} ｜ 服务：{net_desc} " + "─" * 12)
        for key, label, _ in menu:
            print(f"  {key}  {label}")
        print("  q  退出面板")
        try:
            choice = input("cairn> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice in ("q", "quit", "exit"):
            return 0
        if not choice:
            continue
        for key, _, action in menu:
            if choice == key:
                try:
                    action()
                except KeyboardInterrupt:
                    # 子操作被 Ctrl+C 打断时回到面板，而不是整个退出
                    print()
                break
        else:
            print(f"无效选择：{choice}（输入左侧按键，或 q 退出）")


# ── 入口 ────────────────────────────────────────────────────────────────────
def _print_help() -> None:
    print("Cairn 项目控制器 —— 用法：python3 start.py <命令> [选项]\n")
    print("命令：")
    width = max(len(n) for n in COMMANDS)
    for name, (_, help_text) in COMMANDS.items():
        print(f"  {name.ljust(width)}  {help_text}")
    print("\n终端里不带命令进入交互面板（panel）；非交互环境等同 `up`（前台启动）。每个命令支持 --help 查看专属选项。")


def main(argv: list[str]) -> int:
    if not argv:
        # 终端里裸跑进入交互面板；非交互环境（nohup/管道/服务托管）保持原前台启动语义
        if sys.stdin.isatty() and sys.stdout.isatty():
            return cmd_panel([])
        return cmd_up([])
    first = argv[0]
    if first in ("-h", "--help", "help"):
        _print_help()
        return 0
    entry = COMMANDS.get(first)
    if entry is None:
        print(f"未知命令：{first}\n")
        _print_help()
        return 2
    handler, _ = entry
    return handler(argv[1:])


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit(130)


# ══════════════════════════════════════════════════════════════════════════
# 扩展方式（写给未来的维护者）
# --------------------------------------------------------------------------
# 新增一个功能命令，只需两步，无需改动其它任何地方：
#
#   1) 写一个函数，签名为 (argv: list[str]) -> int，自己用 argparse 解析专属参数；
#   2) 加一行装饰器 @command("命令名", "一句话帮助")。
#
# 例如给项目加一个“导出某项目图谱”的命令：
#
#   @command("export", "导出项目图谱 YAML：export <project_id>")
#   def cmd_export(argv):
#       p = argparse.ArgumentParser(prog="start.py export")
#       p.add_argument("project_id")
#       p.add_argument("--host", default=DEFAULT_HOST)
#       p.add_argument("--port", type=int, default=DEFAULT_PORT)
#       args = p.parse_args(argv)
#       url = f"http://{_health_host(args.host)}:{args.port}/projects/{args.project_id}/export?format=yaml"
#       with urllib.request.urlopen(url) as r:
#           sys.stdout.write(r.read().decode())
#       return 0
#
# 它会自动出现在 `python3 start.py --help` 里，也自动支持 `python3 start.py export --help`。
# 原则：真正的业务逻辑放在 cairn 包/CLI 里，这里只做“启动与编排”，保持 start.py 稳定。
# ══════════════════════════════════════════════════════════════════════════
