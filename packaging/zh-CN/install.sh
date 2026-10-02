#!/usr/bin/env bash
# Apply the cairn-zh-CN Chinese localization add-on to an existing Cairn checkout.
#
# Usage (any of these work):
#   unzip cairn-zh-CN-{{VERSION}}.zip -d /path/to/Cairn
#   cd /path/to/Cairn && ./install.sh                       # package unzipped inside the project
#   ./install.sh /path/to/cairn                              # explicit project root
#
# What it installs:
#   1. cairn/src/cairn/dispatcher/prompts/zh-CN/   -> Chinese-output agent prompts
#   2. cairn/src/cairn/server/static/index.html     -> bilingual EN/中文 web UI
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve the Cairn project root:
#   1) explicit --target/arg, 2) package unzipped inside a project (../ has cairn/src),
#   3) current directory.
if [ "${1:-}" != "" ]; then
  TARGET="$(cd "$1" && pwd)"
elif [ -d "$SELF_DIR/../cairn/src/cairn" ]; then
  TARGET="$(cd "$SELF_DIR/.." && pwd)"
elif [ -d "$(pwd)/cairn/src/cairn" ]; then
  TARGET="$(pwd)"
else
  echo "error: cannot locate a Cairn project root." >&2
  echo "usage: $0 [path-to-cairn-project-root]" >&2
  exit 1
fi

if [ ! -d "$TARGET/cairn/src/cairn" ]; then
  echo "error: '$TARGET' does not look like a Cairn project root (missing cairn/src/cairn)" >&2
  exit 1
fi

cp -r "$SELF_DIR/files/." "$TARGET/"
echo "[+] installed zh-CN prompts + bilingual index.html into $TARGET"

if [ -f "$TARGET/dispatch.yaml" ]; then
  if grep -q 'prompt_group' "$TARGET/dispatch.yaml"; then
    echo "[?] dispatch.yaml already has a prompt_group line — set it to: prompt_group: \"zh-CN\""
  else
    echo "[?] dispatch.yaml has no prompt_group — add under runtime:  prompt_group: \"zh-CN\""
  fi
else
  echo "[?] no dispatch.yaml found (normal for a fresh clone). Create one from dispatch.local.example.yaml"
  echo "    and set runtime.prompt_group to \"zh-CN\"."
fi

cat <<'EOF'

Done. Two switches to go Chinese:
  1. Prompts (agent output): in dispatch.yaml set
       runtime:
         prompt_group: "zh-CN"
     then restart server + dispatcher.
  2. Web UI: click the 中文 button in the top-right corner of the page (persisted in the browser).
EOF
