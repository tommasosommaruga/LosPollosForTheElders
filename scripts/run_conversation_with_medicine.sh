#!/usr/bin/env bash
# Same PC as Reachy: load generated env and start pollen conversation app.
# Requires: medicine-buddy already serving (other terminal: ./scripts/run_robot_host.sh)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/integrations/reachy_conversation/.env.generated"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE"
  echo "Start Medicine Buddy first in another terminal:"
  echo "  $ROOT/scripts/run_robot_host.sh"
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

CMD="${REACHY_CONVERSATION_CMD:-reachy-mini-conversation-app}"
echo "Starting: $CMD"
echo "MEDICINE_BUDDY_URL=$MEDICINE_BUDDY_URL"
exec $CMD "$@"
