#!/usr/bin/env bash
# Install (or update) the Medicine Buddy app on a Wireless Reachy Mini, so it appears in
# Reachy Mini Control -> Applications. Also syncs arm + Google Calendar write keys from the
# laptop repo .env into ~/.local/share/medicine-buddy/.env on the robot — that file is
# gitignored locally and is NOT in the pip package.
#
#   scripts/install_on_robot.sh [robot-host] [git-ref|local]
#   scripts/install_on_robot.sh reachy-mini.local main
#   scripts/install_on_robot.sh reachy-mini.local local   # upload this checkout (no git push)
#
# Env:
#   SKIP_PIP=1     only sync robot .env, do not pip install
#   RESTART_APP=1  stop/start medicine_buddy via the robot HTTP API after install
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROBOT="${1:-reachy-mini.local}"
REF="${2:-main}"

trap 'echo "ERROR: install_on_robot.sh failed at line $LINENO (exit $?)." >&2' ERR

_read_env_key() {
  # Always succeeds: missing keys → empty string (do not trip set -e / pipefail).
  local key="$1" file="$ROOT/.env" line=""
  [[ -f "$file" ]] || { echo ""; return 0; }
  line="$(grep -E "^[[:space:]]*${key}=" "$file" | tail -1 || true)"
  if [[ -z "$line" ]]; then
    echo ""
    return 0
  fi
  echo "${line#*=}" | tr -d '"' | tr -d "'"
  return 0
}

ARM_URL="$(_read_env_key MEDICINE_BUDDY_ARM_URL)"
ARM_TIMEOUT="$(_read_env_key MEDICINE_BUDDY_ARM_TIMEOUT_S)"
DRY_RUN="$(_read_env_key MEDICINE_BUDDY_DRY_RUN)"
MOVE_VIA="$(_read_env_key MEDICINE_BUDDY_ARM_MOVE_VIA)"
GOOGLE_CLIENT_ID="$(_read_env_key MEDICINE_BUDDY_GOOGLE_CLIENT_ID)"
GOOGLE_CLIENT_SECRET="$(_read_env_key MEDICINE_BUDDY_GOOGLE_CLIENT_SECRET)"
GOOGLE_REFRESH_TOKEN="$(_read_env_key MEDICINE_BUDDY_GOOGLE_REFRESH_TOKEN)"
GOOGLE_CALENDAR_ID="$(_read_env_key MEDICINE_BUDDY_GOOGLE_CALENDAR_ID)"

echo "Robot: pollen@$ROBOT  ref=$REF  arm_url=${ARM_URL:-'(none)'}"
if [[ -n "${GOOGLE_CLIENT_ID}" && -n "${GOOGLE_REFRESH_TOKEN}" && -n "${GOOGLE_CALENDAR_ID}" ]]; then
  echo "Google Calendar write: will sync from laptop .env"
else
  echo "Google Calendar write: incomplete in laptop .env (optional)"
fi

sync_robot_env() {
  local frag
  frag="$(mktemp)"
  {
    if [[ -n "${ARM_URL}" ]]; then
      echo "MEDICINE_BUDDY_ARM_URL=${ARM_URL}"
    else
      echo "WARN: MEDICINE_BUDDY_ARM_URL missing in $ROOT/.env — robot will stay on FakeArm." >&2
    fi
    if [[ -n "${ARM_TIMEOUT}" ]]; then
      echo "MEDICINE_BUDDY_ARM_TIMEOUT_S=${ARM_TIMEOUT}"
    fi
    if [[ -n "${DRY_RUN}" ]]; then
      echo "MEDICINE_BUDDY_DRY_RUN=${DRY_RUN}"
    fi
    if [[ -n "${MOVE_VIA}" ]]; then
      echo "MEDICINE_BUDDY_ARM_MOVE_VIA=${MOVE_VIA}"
    fi
    if [[ -n "${GOOGLE_CLIENT_ID}" ]]; then
      echo "MEDICINE_BUDDY_GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID}"
    fi
    if [[ -n "${GOOGLE_CLIENT_SECRET}" ]]; then
      echo "MEDICINE_BUDDY_GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET}"
    fi
    if [[ -n "${GOOGLE_REFRESH_TOKEN}" ]]; then
      echo "MEDICINE_BUDDY_GOOGLE_REFRESH_TOKEN=${GOOGLE_REFRESH_TOKEN}"
    fi
    if [[ -n "${GOOGLE_CALENDAR_ID}" ]]; then
      echo "MEDICINE_BUDDY_GOOGLE_CALENDAR_ID=${GOOGLE_CALENDAR_ID}"
    fi
  } >"$frag"

  if [[ ! -s "$frag" ]]; then
    echo "WARN: nothing to sync from laptop .env"
    rm -f "$frag"
    return 0
  fi

  echo "Syncing arm + Google env to pollen@$ROBOT:~/.local/share/medicine-buddy/.env ..."
  echo "(SSH/SCP may ask for the robot password — type it when prompted.)"
  # Do NOT feed a heredoc into ssh stdin: that steals the password prompt.
  scp "$frag" "pollen@$ROBOT:/tmp/medicine_buddy_sync.env"
  ssh -t "pollen@$ROBOT" 'bash -lc "
set -euo pipefail
DATA_DIR=\"\$HOME/.local/share/medicine-buddy\"
ENV_FILE=\"\$DATA_DIR/.env\"
mkdir -p \"\$DATA_DIR\"
umask 077
touch \"\$ENV_FILE\"
tmp=\"\$(mktemp)\"
grep -vE \"^[[:space:]]*MEDICINE_BUDDY_(ARM_URL|ARM_TIMEOUT_S|DRY_RUN|ARM_MOVE_VIA|GOOGLE_CLIENT_ID|GOOGLE_CLIENT_SECRET|GOOGLE_REFRESH_TOKEN|GOOGLE_CALENDAR_ID)=\" \"\$ENV_FILE\" >\"\$tmp\" || true
cat \"\$tmp\" /tmp/medicine_buddy_sync.env >\"\$ENV_FILE\"
rm -f \"\$tmp\" /tmp/medicine_buddy_sync.env
echo \"--- robot .env (synced keys, secrets redacted) ---\"
grep -E \"^[[:space:]]*MEDICINE_BUDDY_(ARM_|DRY_RUN|GOOGLE_)\" \"\$ENV_FILE\" \\
  | sed -E \"s/(GOOGLE_CLIENT_SECRET|GOOGLE_REFRESH_TOKEN)=.*/\\1=(set)/\" || true
"'
  rm -f "$frag"
  echo "Robot env synced (persists under ~/.local/share/medicine-buddy/.env)."
}

install_package() {
  if [[ "${SKIP_PIP:-0}" == "1" ]]; then
    echo "SKIP_PIP=1 — not running pip install."
    return 0
  fi
  if [[ "$REF" == "local" ]]; then
    echo "Uploading local apps/medicine_buddy to pollen@$ROBOT and pip-installing..."
    ssh -t "pollen@$ROBOT" "rm -rf /tmp/medicine_buddy_pkg && mkdir -p /tmp/medicine_buddy_pkg"
    rsync -az --delete \
      --exclude '.venv' --exclude 'data/' --exclude '__pycache__' --exclude '.pytest_cache' \
      --exclude '*.egg-info' --exclude '.ruff_cache' \
      "$ROOT/apps/medicine_buddy/" "pollen@$ROBOT:/tmp/medicine_buddy_pkg/"
    ssh -t "pollen@$ROBOT" "/venvs/apps_venv/bin/pip install --upgrade '/tmp/medicine_buddy_pkg'"
  else
    SPEC="medicine_buddy @ git+https://github.com/tommasosommaruga/LosPollosForTheElders@${REF}#subdirectory=apps/medicine_buddy"
    echo "Installing '$SPEC' on pollen@$ROBOT ..."
    ssh -t "pollen@$ROBOT" "/venvs/apps_venv/bin/pip install --upgrade '$SPEC'"
  fi
  echo "Package install finished."
}

restart_app() {
  if [[ "${RESTART_APP:-0}" != "1" ]]; then
    return 0
  fi
  echo "Restarting medicine_buddy on $ROBOT ..."
  curl -sS -m 10 -X POST "http://$ROBOT:8000/api/apps/stop-app/medicine_buddy" >/dev/null || true
  sleep 2
  curl -sS -m 15 -X POST "http://$ROBOT:8000/api/apps/start-app/medicine_buddy" || true
  echo
}

sync_robot_env
install_package
restart_app

echo
echo "Done."
echo "  Arm URL on robot: ${ARM_URL:-'(none)'}"
echo "  Google calendar id: ${GOOGLE_CALENDAR_ID:-'(none)'}"
echo "  Start: Reachy Mini Control -> Applications -> Medicine Buddy"
echo "  Or:    curl -X POST http://$ROBOT:8000/api/apps/start-app/medicine_buddy"
echo "  Install local + sync .env + restart:"
echo "    RESTART_APP=1 $0 $ROBOT local"
echo "  Env-only sync (no pip):"
echo "    SKIP_PIP=1 RESTART_APP=1 $0 $ROBOT"
