#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root to read PostgreSQL and private project files." >&2
  exit 2
fi
RECIPIENT="${BACKUP_RECIPIENT:-}"
if [[ -z "$RECIPIENT" ]]; then
  echo "Set BACKUP_RECIPIENT to an age public recipient before creating a backup." >&2
  exit 2
fi
if ! command -v age >/dev/null 2>&1; then
  echo "Install age before creating encrypted backups." >&2
  exit 2
fi

BACKUP_DIR="${1:-/var/backups/chitoproject}"
install -d -o root -g root -m 0700 "$BACKUP_DIR"
umask 077
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TMP_DIR"' EXIT

runuser -u postgres -- pg_dump --format=custom --dbname=chitoproject > "$TMP_DIR/database.dump"
tar -C /var/lib/chitoproject -czf "$TMP_DIR/uploads.tar.gz" uploads
tar -C "$TMP_DIR" -czf - database.dump uploads.tar.gz | age --recipient "$RECIPIENT" \
  --output "$BACKUP_DIR/chitoproject-${STAMP}.tar.gz.age"
chmod 0600 "$BACKUP_DIR/chitoproject-${STAMP}.tar.gz.age"
echo "Encrypted backup created: $BACKUP_DIR/chitoproject-${STAMP}.tar.gz.age"
