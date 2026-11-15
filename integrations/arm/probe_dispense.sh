#!/usr/bin/env bash
# Timed one-shot POST to the arm PC /v1/dispense (unique request_id each run).
#
# Usage:
#   ./integrations/arm/probe_dispense.sh
#   ./integrations/arm/probe_dispense.sh "small bottle"
#   ARM_URL=http://172.24.0.187:9090 ./integrations/arm/probe_dispense.sh Aspirin
#
# Env:
#   ARM_URL / MEDICINE_BUDDY_ARM_URL  base URL (default from repo .env or 172.24.0.187:9090)
#   DRY_RUN=true|false                default true
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
if [[ -f "$ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  set -a
  # Only pull ARM URL lines — avoid sourcing secrets into the shell casually.
  # Prefer full .env if already exported by the user; otherwise read the key.
  _arm_from_env="$(grep -E '^[[:space:]]*MEDICINE_BUDDY_ARM_URL=' "$ROOT/.env" | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
  set +a
fi

ARM_URL="${ARM_URL:-${MEDICINE_BUDDY_ARM_URL:-${_arm_from_env:-http://172.24.0.187:9090}}}"
ARM_URL="${ARM_URL%/}"
NAME="${1:-small bottle}"
DRY_RUN="${DRY_RUN:-true}"
REQUEST_ID="$(uuidgen | tr '[:upper:]' '[:lower:]')"

echo "POST ${ARM_URL}/v1/dispense  name=${NAME}  request_id=${REQUEST_ID}  dry_run=${DRY_RUN}"

time curl -sS -X POST "${ARM_URL}/v1/dispense" \
  -H "Content-Type: application/json" \
  -d "{\"schema\":\"medicine_buddy.arm.v1\",\"message_type\":\"dispense\",\"request_id\":\"${REQUEST_ID}\",\"medicine\":{\"id\":99,\"name\":\"${NAME}\",\"person\":\"Mario\"},\"slot_hint\":null,\"ocr_min_confidence\":0.65,\"actions\":[\"locate\",\"pick_to_bowl\",\"verify_place\"],\"one_shot_detect\":true,\"dry_run\":${DRY_RUN}}" \
  -w "\n\ntime_total=%{time_total}s  http=%{http_code}\n"
