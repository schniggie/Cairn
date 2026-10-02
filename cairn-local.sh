#!/usr/bin/env bash
# Cairn local-mode helpers (server + dispatcher on host)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"
cd "$ROOT"
mkdir -p "$ROOT/datas/cairn" "$ROOT/datas/runs" "$ROOT/datas"

preflight() {
  # 1) uv must be available
  if ! command -v uv >/dev/null 2>&1; then
    echo "error: 'uv' not found in PATH." >&2
    echo "  install: https://docs.astral.sh/uv/   (curl -LsSf https://astral.sh/uv/install.sh | sh)" >&2
    exit 1
  fi
  # 2) dispatcher config must exist (it is gitignored, so a fresh checkout has none)
  if [ ! -f "$ROOT/dispatch.yaml" ]; then
    echo "error: $ROOT/dispatch.yaml not found." >&2
    echo "  create it first:" >&2
    echo "    cp dispatch.local.example.yaml dispatch.yaml   # local mode (workers run on this host)" >&2
    echo "    cp dispatch.example.yaml dispatch.yaml          # container mode" >&2
    echo "  then set runtime.prompt_group: \"zh-CN\" to enable Chinese prompts." >&2
    exit 1
  fi
}

start() {
  preflight

  if curl -sf http://127.0.0.1:8000/projects >/dev/null 2>&1; then
    echo "server already up on :8000"
  else
    nohup uv run --project cairn cairn serve --host 127.0.0.1 --port 8000 \
      >"$ROOT/datas/server.log" 2>&1 &
    echo "server pid=$! (first run may take minutes to create .venv and install deps)"
    for i in $(seq 1 60); do
      curl -sf http://127.0.0.1:8000/projects >/dev/null 2>&1 && break
      sleep 1
    done
    if ! curl -sf http://127.0.0.1:8000/projects >/dev/null 2>&1; then
      echo "! server did not respond within 60s. tail -5 server.log:" >&2
      tail -5 "$ROOT/datas/server.log" 2>/dev/null || echo "(no server.log)" >&2
    fi
  fi

  if pgrep -f 'cairn dispatch --config dispatch.yaml' >/dev/null 2>&1; then
    echo "dispatcher already running"
  else
    nohup uv run --project cairn cairn dispatch --config dispatch.yaml \
      >"$ROOT/datas/dispatcher.log" 2>&1 &
    echo "dispatcher pid=$!"
  fi
  echo "API:  http://127.0.0.1:8000/docs"
  echo "logs: $ROOT/datas/server.log  $ROOT/datas/dispatcher.log"
}

stop() {
  pkill -f 'cairn dispatch --config dispatch.yaml' 2>/dev/null || true
  pkill -f 'cairn serve --host 127.0.0.1 --port 8000' 2>/dev/null || true
  # also match bare serve
  pkill -f '/cairn/.venv/bin/cairn serve' 2>/dev/null || true
  pkill -f '/cairn/.venv/bin/cairn dispatch' 2>/dev/null || true
  echo "stopped (best effort)"
}

status() {
  echo "== processes =="
  pgrep -af 'cairn (serve|dispatch)' || echo "(none)"
  echo "== api =="
  curl -sf http://127.0.0.1:8000/projects && echo || echo "server not responding"
  echo "== dispatcher config =="
  [ -f "$ROOT/dispatch.yaml" ] && echo "dispatch.yaml present" || echo "dispatch.yaml MISSING (create it, then start)"
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  status) status ;;
  *) echo "usage: $0 {start|stop|restart|status}"; exit 1 ;;
esac
