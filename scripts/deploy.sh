#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root so systemd services can be restarted: sudo bash scripts/deploy.sh" >&2
  exit 2
fi
APP_DIR=/opt/projecthub/app
VENV_DIR=/opt/projecthub/venv
if [[ ! -d "$APP_DIR/.git" || ! -x "$VENV_DIR/bin/python" ]]; then
  echo "The VPS installation was not found at $APP_DIR." >&2
  exit 2
fi

if [[ -n "$(runuser -u projecthub -- git -C "$APP_DIR" status --porcelain)" ]]; then
  echo "The checkout has local changes. Commit or preserve them before deployment." >&2
  exit 2
fi
runuser -u projecthub -- git -C "$APP_DIR" fetch --prune origin
runuser -u projecthub -- git -C "$APP_DIR" pull --ff-only origin main
runuser -u projecthub -- "$VENV_DIR/bin/python" -m pip install -e "$APP_DIR"
runuser -u projecthub -- bash -c "cd '$APP_DIR' && '$VENV_DIR/bin/python' -m app.cli migrate"

systemctl restart projecthub-api projecthub-worker
for unit in projecthub-telegram projecthub-bale; do
  if systemctl is-enabled --quiet "$unit"; then
    systemctl restart "$unit"
  fi
done
systemctl --no-pager --full status projecthub-api projecthub-worker
echo "ChitoProject updated and services restarted."
