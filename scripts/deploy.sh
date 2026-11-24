#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root so systemd services can be restarted: sudo bash scripts/deploy.sh" >&2
  exit 2
fi
APP_DIR=/opt/chitoproject/app
VENV_DIR=/opt/chitoproject/venv
if [[ ! -d "$APP_DIR/.git" || ! -x "$VENV_DIR/bin/python" ]]; then
  echo "The VPS installation was not found at $APP_DIR." >&2
  exit 2
fi

if [[ -n "$(runuser -u chitoproject -- git -C "$APP_DIR" status --porcelain)" ]]; then
  echo "The checkout has local changes. Commit or preserve them before deployment." >&2
  exit 2
fi
export GIT_SSH_COMMAND='ssh -i /opt/chitoproject/.ssh/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/opt/chitoproject/.ssh/known_hosts'
runuser -u chitoproject -- git -C "$APP_DIR" fetch --prune origin
runuser -u chitoproject -- git -C "$APP_DIR" pull --ff-only origin main
runuser -u chitoproject -- "$VENV_DIR/bin/python" -m pip install -e "$APP_DIR"
runuser -u chitoproject -- bash -c "cd '$APP_DIR' && '$VENV_DIR/bin/python' -m app.cli migrate"

systemctl restart chitoproject-api chitoproject-worker
for unit in chitoproject-telegram chitoproject-bale; do
  if systemctl is-enabled --quiet "$unit"; then
    systemctl restart "$unit"
  fi
done
systemctl --no-pager --full status chitoproject-api chitoproject-worker
echo "ChitoProject updated and services restarted."
