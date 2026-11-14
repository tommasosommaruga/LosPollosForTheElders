#!/usr/bin/env bash
# One-time (and after every git pull) setup on the computer: venv + install the app.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/apps/medicine_buddy"

cd "$APP"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -e ".[dev]"   # also installs reachy-mini + Pollen's conversation app

if [[ ! -f "$ROOT/.env" ]]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "Created .env from .env.example - set REACHY_HOST to your robot's IP."
fi
if [[ ! -f "$ROOT/agents.local.md" ]]; then
  cp "$ROOT/agents.local.md.example" "$ROOT/agents.local.md"
fi

echo "Bootstrap OK. Run the app: $ROOT/scripts/run_robot_host.sh  (or install it on the robot: scripts/install_on_robot.sh)"
