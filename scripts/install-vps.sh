#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root: sudo bash scripts/install-vps.sh <git-repo-url> <domain>" >&2
  exit 2
fi
if [[ $# -ne 2 ]]; then
  echo "Usage: sudo bash scripts/install-vps.sh <git-repo-url> <domain>" >&2
  exit 2
fi

REPO_URL="$1"
DOMAIN="$2"
if [[ ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ || "$DOMAIN" == *..* ]]; then
  echo "Provide a plain domain name, without scheme or path." >&2
  exit 2
fi
if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer currently targets Debian and Ubuntu VPS hosts." >&2
  exit 2
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git openssh-client python3.12 python3.12-venv postgresql postgresql-contrib caddy openssl age

if ! id projecthub >/dev/null 2>&1; then
  useradd --system --home-dir /opt/projecthub --create-home --shell /usr/sbin/nologin projecthub
fi
install -d -o projecthub -g projecthub -m 0750 /opt/projecthub
install -d -o projecthub -g projecthub -m 0750 /var/lib/projecthub
install -d -o projecthub -g projecthub -m 0700 /var/lib/projecthub/uploads
install -d -o root -g projecthub -m 0750 /etc/projecthub

APP_DIR=/opt/projecthub/app
VENV_DIR=/opt/projecthub/venv
if [[ -e "$APP_DIR" ]]; then
  echo "$APP_DIR already exists. Use scripts/deploy.sh for updates." >&2
  exit 2
fi
runuser -u projecthub -- git clone --branch main --single-branch "$REPO_URL" "$APP_DIR"
runuser -u projecthub -- python3.12 -m venv "$VENV_DIR"
runuser -u projecthub -- "$VENV_DIR/bin/python" -m pip install --upgrade pip
runuser -u projecthub -- "$VENV_DIR/bin/python" -m pip install -e "$APP_DIR"

DB_PASSWORD="$(openssl rand -hex 32)"
APP_SECRET="$(openssl rand -hex 48)"
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -v projecthub_password="$DB_PASSWORD" <<'SQL'
CREATE ROLE projecthub LOGIN PASSWORD :'projecthub_password';
CREATE DATABASE projecthub OWNER projecthub;
SQL

cat > /etc/projecthub/projecthub.env <<EOF
APP_ENV=production
APP_NAME=ChitoProject
PUBLIC_BASE_URL=https://${DOMAIN}
DATABASE_URL=postgresql+asyncpg://projecthub:${DB_PASSWORD}@127.0.0.1:5432/projecthub
SECRET_KEY=${APP_SECRET}
TELEGRAM_BOT_TOKEN=
BALE_BOT_TOKEN=
TELEGRAM_BOT_USERNAME=
BALE_BOT_USERNAME=
UPLOADS_DIR=/var/lib/projecthub/uploads
MAX_UPLOAD_MB=20
MAX_ARCHIVE_MB=500
SESSION_TTL_HOURS=12
LOGIN_MAX_ATTEMPTS=5
LOGIN_LOCK_MINUTES=15
BOT_POLLING_TIMEOUT=25
BOT_API_TIMEOUT=35
BOT_WORKER_CONCURRENCY=8
EOF
chown root:projecthub /etc/projecthub/projecthub.env
chmod 0640 /etc/projecthub/projecthub.env
ln -s /etc/projecthub/projecthub.env "$APP_DIR/.env"

install -o root -g root -m 0644 "$APP_DIR/deploy/systemd/projecthub-api.service" /etc/systemd/system/projecthub-api.service
install -o root -g root -m 0644 "$APP_DIR/deploy/systemd/projecthub-worker.service" /etc/systemd/system/projecthub-worker.service
install -o root -g root -m 0644 "$APP_DIR/deploy/systemd/projecthub-telegram.service" /etc/systemd/system/projecthub-telegram.service
install -o root -g root -m 0644 "$APP_DIR/deploy/systemd/projecthub-bale.service" /etc/systemd/system/projecthub-bale.service

install -d -o root -g root -m 0755 /etc/caddy/conf.d
cat > /etc/caddy/conf.d/projecthub.caddy <<EOF
${DOMAIN} {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
EOF
if ! grep -Fq 'import /etc/caddy/conf.d/*.caddy' /etc/caddy/Caddyfile; then
  printf '\nimport /etc/caddy/conf.d/*.caddy\n' >> /etc/caddy/Caddyfile
fi

systemctl enable --now postgresql caddy
systemctl daemon-reload
systemctl enable projecthub-api projecthub-worker projecthub-telegram projecthub-bale

cd "$APP_DIR"
runuser -u projecthub -- "$VENV_DIR/bin/python" -m app.cli migrate
runuser -u projecthub -- "$VENV_DIR/bin/python" -m app.cli issue-owner-code

systemctl restart caddy
systemctl enable --now projecthub-api projecthub-worker projecthub-telegram projecthub-bale

cat <<EOF

ChitoProject is installed at ${DOMAIN}.
The first owner code was printed above and expires after 24 hours. Use it once at:
  https://${DOMAIN}/admin/activate

Bot tokens and usernames are currently blank. Edit /etc/projecthub/projecthub.env,
then run: sudo systemctl restart projecthub-telegram projecthub-bale

For later releases, run as root from the checkout:
  sudo bash ${APP_DIR}/scripts/deploy.sh
EOF
