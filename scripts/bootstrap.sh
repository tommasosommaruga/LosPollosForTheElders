#!/usr/bin/env bash
# First-time setup after git clone/pull on the robot host PC.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/apps/medicine_buddy"

cd "$APP"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -e ".[dev]"

if [[ ! -f "$ROOT/agents.local.md" ]]; then
  cp "$ROOT/agents.local.md.example" "$ROOT/agents.local.md"
  echo "Created agents.local.md from example — edit camera / IP fields if needed."
fi

echo "Bootstrap OK. Next: $ROOT/scripts/run_robot_host.sh"
