#!/usr/bin/env bash
# Run the whole Medicine Buddy app from this computer against the robot in .env (REACHY_HOST),
# or pass the robot's address: scripts/run_robot_host.sh --host 192.168.1.42
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/apps/medicine_buddy"

if [[ ! -d "$APP/.venv" ]]; then
  "$ROOT/scripts/bootstrap.sh"
fi
# shellcheck disable=SC1091
source "$APP/.venv/bin/activate"
cd "$ROOT"
echo "Status page: http://127.0.0.1:8787/   Family settings: http://127.0.0.1:8787/family"
exec python -m medicine_buddy.main "$@"
