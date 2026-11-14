#!/usr/bin/env bash
# Install (or update) the Medicine Buddy app on a Wireless Reachy Mini, so it appears in
# Reachy Mini Control -> Applications. The robot needs internet. You'll be asked for the
# robot's SSH password (see Pollen's Wireless setup guide).
#
#   scripts/install_on_robot.sh [robot-host] [git-ref]
#   scripts/install_on_robot.sh reachy-mini.local main
set -euo pipefail

ROBOT="${1:-reachy-mini.local}"
REF="${2:-main}"
SPEC="medicine_buddy @ git+https://github.com/tommasosommaruga/LosPollosForTheElders@${REF}#subdirectory=apps/medicine_buddy"

echo "Installing '$SPEC' on pollen@$ROBOT ..."
ssh "pollen@$ROBOT" "/venvs/apps_venv/bin/pip install --upgrade '$SPEC'"
echo
echo "Done. Start it from Reachy Mini Control -> Applications -> Medicine Buddy, or:"
echo "  curl -X POST http://$ROBOT:8000/api/apps/start-app/medicine_buddy"
