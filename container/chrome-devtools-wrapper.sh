#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Cairn 对 chrome-devtools CLI 的包装入口。
#
# 镜像里 `chrome-devtools` 就是本脚本，真实 CLI 保留为 `chrome-devtools-real`。
# 这样做不是风格偏好，而是因为「靠 agent 自觉传 flag」在这套参数上不成立：
#
# 1. daemon 的启动参数只能由 `start` 的 flag 传入。除 usage statistics 外，
#    上游没有给任何一项提供 env 等价物（已对 1.9.0 发布产物逐一实证）。
# 2. 工具命令在 daemon 未运行时，会用「工具命令自己的 argv」自动拉起 daemon
#    （build/src/bin/chrome-devtools.js），也就是全套默认值。而默认值在本场景
#    下每一项都不可接受：
#      usageStatistics = true    → 工具调用信息上报 Google
#      performanceCrux = true    → performance trace 的 URL 回传 Google CrUX
#      channel         = stable  → 不会用镜像里的 Chromium，而是去找 Google Chrome
#      isolated        = true    → 临时 profile，浏览器一关 Cookie 就没了
# 3. 所以把参数收敛到本脚本，agent 侧不需要记住任何 flag，也无从绕过。
#
# 隔离：socket 与 pid 的路径由 XDG_RUNTIME_DIR 决定，由 dispatch 配置按 Worker
# 注入；浏览器 profile 同样按 Worker 分目录，否则多个 Worker 会抢同一个
# user-data-dir（Chrome 的 SingletonLock 会让后来者起不来）。
#
# 背景与验证清单：reports/Cairn 浏览器自动化改进方案-评审与落地建议.md
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REAL="${CAIRN_CDT_REAL_CLI:-/usr/local/bin/chrome-devtools-real}"

# ── 每个 Worker 独立的运行时目录（daemon socket + pid）──────────────────────
# 由 dispatch.yaml 的 workers[].env.XDG_RUNTIME_DIR 注入；未注入时退化为共享默认值，
# 那只适合单 Worker 环境。目录权限必须是 0700 且属主为当前用户，否则 daemon 会以
# "Possible tampering" 拒绝启动。
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/cairn-cdt/default}"
mkdir -p -m 700 "$XDG_RUNTIME_DIR" 2>/dev/null || true

# ── 浏览器 profile 也按 Worker 隔离 ────────────────────────────────────────
WORKER_ID="$(basename "$XDG_RUNTIME_DIR")"
PROFILE_DIR="${CAIRN_CDT_PROFILE_DIR:-$HOME/.cache/cairn-cdt/$WORKER_ID/chrome-profile}"
mkdir -p "$(dirname "$PROFILE_DIR")" 2>/dev/null || true

# ── 定位浏览器可执行文件 ───────────────────────────────────────────────────
# 上游官方只支持 Chrome / Chrome for Testing，镜像里装的是 Playwright 版
# Chromium，属于「may work, not guaranteed」。用 CAIRN_CDT_CHROME 可指向
# Chrome for Testing，不需要改镜像。
resolve_chrome() {
    if [ -n "${CAIRN_CDT_CHROME:-}" ]; then
        printf '%s' "$CAIRN_CDT_CHROME"
        return 0
    fi
    local candidate found=""
    for candidate in "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux/chrome \
                     "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux64/chrome; do
        # 按「存在」而非「可执行」判定：若文件在但权限位不对，让 Chrome 启动时的
        # 报错直接暴露问题，而不是在这里静默丢掉 flag、退化成去找 Google Chrome。
        [ -f "$candidate" ] && found="$candidate"
    done
    if [ -z "$found" ]; then
        # 不静默退化：否则上游会自行补 --channel=stable 去找 Google Chrome，
        # 报错点与真实原因（本机没有可用浏览器）脱节，很难排查。
        echo "cairn-cdt: 未找到 Playwright Chromium，请设置 CAIRN_CDT_CHROME 指向 Chrome for Testing" >&2
        return 0
    fi
    printf '%s' "$found"
    return 0
}

# 调用方显式给过的选项不再重复注入 —— yargs 对重复 flag 的解析行为不值得依赖。
_opt_given() {
    local name="$1"
    shift
    local arg
    for arg in "$@"; do
        case "$arg" in
            "--$name" | "--$name="* | "--no-$name") return 0 ;;
        esac
    done
    return 1
}

# 构建 daemon 启动 flag。$1 是接收结果的数组名，其余是调用方的参数。
build_start_flags() {
    local -n out="$1"
    shift

    local chrome
    chrome="$(resolve_chrome)"

    # 关掉 performance trace 的 URL 外发（R1）。没有 env 等价物，只能靠 flag。
    if ! _opt_given performance-crux "$@"; then
        out+=(--no-performance-crux)
    fi

    # 显式指定浏览器。不给的话上游会自动补 --channel=stable，行为完全不同（R3）。
    # 调用方若自己指定了来源（browserUrl / wsEndpoint / channel / autoConnect），
    # 就尊重调用方的选择。
    if ! _opt_given executablePath "$@" &&
        ! _opt_given browserUrl "$@" &&
        ! _opt_given wsEndpoint "$@" &&
        ! _opt_given channel "$@" &&
        ! _opt_given autoConnect "$@" &&
        [ -n "$chrome" ]; then
        out+=("--executablePath=$chrome")
    fi

    # 是否「附着到已有浏览器」。上游把 userDataDir 与 browserUrl / wsEndpoint / isolated
    # 都声明为互斥（config/browser-options.js），附着模式下 profile 本来也没有意义，
    # 所以这两种情况一律不注入。
    local attaches=0
    if _opt_given browserUrl "$@" ||
        _opt_given wsEndpoint "$@" ||
        _opt_given autoConnect "$@"; then
        attaches=1
    fi

    # 持久 profile，让 daemon 重启后登录态还在。默认给，除非调用方自己指定了
    # isolated / userDataDir，或者处在上面两种不适用的情况。
    if [ "${CAIRN_CDT_ISOLATED:-0}" = "1" ] || [ "$attaches" = "1" ]; then
        : # 不给 userDataDir
    elif ! _opt_given userDataDir "$@" && ! _opt_given isolated "$@"; then
        out+=("--userDataDir=$PROFILE_DIR")
    fi

    # 工具面收窄是 opt-in：CLI 模式默认比 MCP 模式多带 13 个 heap snapshot 工具和
    # 5 个扩展工具。是否需要要靠实测 token 与调用次数决定，不预设默认值。
    if [ "${CAIRN_CDT_NARROW:-0}" = "1" ]; then
        out+=(--no-memory-debugging --no-category-extensions)
    fi

    # 自签名 / 过期证书的授权目标
    if [ "${CAIRN_CDT_INSECURE:-0}" = "1" ]; then
        out+=(--acceptInsecureCerts)
    fi

    # 追加 Chrome 参数。容器内缺少 user namespace 时 Chrome 起不来，可设
    # CAIRN_CDT_CHROME_ARGS="--no-sandbox" 兜底 —— 但这是降低隔离强度的手段，
    # 只在确有必要时开启。
    if [ -n "${CAIRN_CDT_CHROME_ARGS:-}" ]; then
        local extra
        for extra in $CAIRN_CDT_CHROME_ARGS; do
            out+=("--chromeArg=$extra")
        done
    fi
}

daemon_running() {
    # status 无论 daemon 在不在都返回 0，只能看输出，不能看退出码。
    "$REAL" status 2>/dev/null | grep -q 'daemon is running\.'
}

_main() {
    local cmd="${1:-}"
    case "$cmd" in
        "" | -h | --help | -v | --version)
            exec "$REAL" "$@"
            ;;
        status | stop)
            # 只读 / 幂等，原样放行
            exec "$REAL" "$@"
            ;;
        start)
            shift
            local flags=()
            build_start_flags flags "$@"
            exec "$REAL" start "${flags[@]}" "$@"
            ;;
        *)
            # 工具命令：daemon 不在时先按 Cairn 的参数把它拉起来，否则它会用
            # 默认值自行启动，遥测与浏览器来源都会跑偏。
            if ! daemon_running; then
                local flags=()
                build_start_flags flags
                "$REAL" start "${flags[@]}" >/dev/null
            fi
            exec "$REAL" "$@"
            ;;
    esac
}

_main "$@"
