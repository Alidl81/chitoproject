#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root: sudo bash scripts/install-vps.sh <git-repo-url> <host>" >&2
  exit 2
fi
if [[ $# -ne 2 ]]; then
  echo "Usage: sudo bash scripts/install-vps.sh <git-repo-url> <host>" >&2
  exit 2
fi

REPO_URL="$1"
PUBLIC_HOST="$2"
HOST_KIND="$(python3 - "$PUBLIC_HOST" <<'PY'
import ipaddress
import sys

try:
    address = ipaddress.IPv4Address(sys.argv[1])
except ipaddress.AddressValueError:
    print("domain")
else:
    if not address.is_global:
        raise SystemExit("The IPv4 address must be publicly routable.")
    print("ipv4")
PY
)"
if [[ "$HOST_KIND" == "domain" ]]; then
  if [[ "$PUBLIC_HOST" =~ ^[0-9.]+$ || ! "$PUBLIC_HOST" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ || \
        "$PUBLIC_HOST" != *.* || "$PUBLIC_HOST" == *..* ]]; then
    echo "Provide a public IPv4 address or a fully qualified domain, without scheme or path." >&2
    exit 2
  fi
fi
if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer targets Ubuntu 24.04 LTS." >&2
  exit 2
fi

# This is a fresh-install path. Refuse existing application data or proxy config
# before package installation or any configuration changes.
for path in /opt/chitoproject/app /opt/chitoproject/venv /etc/chitoproject /var/lib/chitoproject /etc/caddy /etc/apt/sources.list.d/caddy-stable.list /usr/share/keyrings/caddy-stable-archive-keyring.gpg; do
  if [[ -e "$path" || -L "$path" ]]; then
    echo "Existing path: $path. Refusing to overwrite; inspect it and use a reviewed migration." >&2
    exit 2
  fi
done
for unit in api worker telegram bale; do
  if [[ -e "/etc/systemd/system/chitoproject-${unit}.service" ]]; then
    echo "Existing ChitoProject service detected; refusing fresh installation." >&2
    exit 2
  fi
done
if command -v caddy >/dev/null 2>&1; then
  echo "Existing Caddy installation detected; configure it manually after a reviewed migration." >&2
  exit 2
fi
if ! id chitoproject >/dev/null 2>&1 || [[ "$(getent passwd chitoproject | cut -d: -f6)" != /opt/chitoproject ]]; then
  echo "Create the chitoproject service account and its read-only deploy key first; see docs/DEPLOYMENT_FA.md." >&2
  exit 2
fi
if [[ "$REPO_URL" != git@github.com:Alidl81/chitoproject.git ]]; then
  echo "Use the SSH URL git@github.com:Alidl81/chitoproject.git and a read-only deploy key." >&2
  exit 2
fi
if ! runuser -u chitoproject -- test -r /opt/chitoproject/.ssh/id_ed25519 || \
   ! runuser -u chitoproject -- test -r /opt/chitoproject/.ssh/known_hosts; then
  echo "The service account cannot read its SSH deploy key or verified known_hosts." >&2
  exit 2
fi
export GIT_SSH_COMMAND='ssh -i /opt/chitoproject/.ssh/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/opt/chitoproject/.ssh/known_hosts'
if ! runuser -u chitoproject -- git ls-remote --exit-code "$REPO_URL" refs/heads/main >/dev/null; then
  echo "The service account cannot read main using its deploy key. No installation was performed." >&2
  exit 2
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git openssh-client ca-certificates curl gnupg age openssl \
  debian-keyring debian-archive-keyring apt-transport-https \
  python3.12 python3.12-venv postgresql postgresql-contrib

curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  > /etc/apt/sources.list.d/caddy-stable.list
chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
chmod o+r /etc/apt/sources.list.d/caddy-stable.list
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y caddy

python3 - "$(caddy version | awk 'NR == 1 {print $1}')" <<'PY'
import sys

raw = sys.argv[1].removeprefix("v").split("-")[0]
version = tuple(int(part) for part in raw.split(".")[:3])
if version < (2, 11, 1):
    raise SystemExit(f"Caddy 2.11.1+ is required for public IP certificates; found {raw}.")
PY

install -d -o chitoproject -g chitoproject -m 0750 /opt/chitoproject
install -d -o chitoproject -g chitoproject -m 0750 /var/lib/chitoproject
install -d -o chitoproject -g chitoproject -m 0700 /var/lib/chitoproject/uploads
install -d -o root -g chitoproject -m 0750 /etc/chitoproject

APP_DIR=/opt/chitoproject/app
VENV_DIR=/opt/chitoproject/venv
if [[ -e "$APP_DIR" ]]; then
  echo "$APP_DIR already exists. Use scripts/deploy.sh for updates." >&2
  exit 2
fi
runuser -u chitoproject -- git clone --branch main --single-branch "$REPO_URL" "$APP_DIR"
runuser -u chitoproject -- python3.12 -m venv "$VENV_DIR"
runuser -u chitoproject -- "$VENV_DIR/bin/python" -m pip install --upgrade pip
runuser -u chitoproject -- "$VENV_DIR/bin/python" -m pip install -e "$APP_DIR"

systemctl enable --now postgresql
if runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='chitoproject'" | grep -q 1 || \
   runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='chitoproject'" | grep -q 1; then
  echo "Existing PostgreSQL role or database chitoproject; refusing to modify it." >&2
  exit 2
fi
DB_PASSWORD="$(openssl rand -hex 32)"
APP_SECRET="$(openssl rand -hex 48)"
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -v chitoproject_password="$DB_PASSWORD" <<'SQL'
CREATE ROLE chitoproject LOGIN PASSWORD :'chitoproject_password';
CREATE DATABASE chitoproject OWNER chitoproject;
SQL

cat > /etc/chitoproject/chitoproject.env <<EOF
APP_ENV=production
APP_NAME=ChitoProject
PUBLIC_BASE_URL=https://${PUBLIC_HOST}
DATABASE_URL=postgresql+asyncpg://chitoproject:${DB_PASSWORD}@127.0.0.1:5432/chitoproject
SECRET_KEY=${APP_SECRET}
TELEGRAM_BOT_TOKEN=
BALE_BOT_TOKEN=
TELEGRAM_BOT_USERNAME=
BALE_BOT_USERNAME=
UPLOADS_DIR=/var/lib/chitoproject/uploads
MAX_UPLOAD_MB=20
MAX_ARCHIVE_MB=500
SESSION_TTL_HOURS=12
LOGIN_MAX_ATTEMPTS=5
LOGIN_LOCK_MINUTES=15
BOT_POLLING_TIMEOUT=25
BOT_API_TIMEOUT=35
BOT_WORKER_CONCURRENCY=8
EOF
chown root:chitoproject /etc/chitoproject/chitoproject.env
chmod 0640 /etc/chitoproject/chitoproject.env
ln -s /etc/chitoproject/chitoproject.env "$APP_DIR/.env"

install -o root -g root -m 0644 "$APP_DIR/deploy/systemd/chitoproject-api.service" /etc/systemd/system/chitoproject-api.service
install -o root -g root -m 0644 "$APP_DIR/deploy/systemd/chitoproject-worker.service" /etc/systemd/system/chitoproject-worker.service
install -o root -g root -m 0644 "$APP_DIR/deploy/systemd/chitoproject-telegram.service" /etc/systemd/system/chitoproject-telegram.service
install -o root -g root -m 0644 "$APP_DIR/deploy/systemd/chitoproject-bale.service" /etc/systemd/system/chitoproject-bale.service

install -d -o root -g root -m 0755 /etc/caddy/conf.d
if [[ "$HOST_KIND" == "ipv4" ]]; then
  cat > /etc/caddy/conf.d/chitoproject.caddy <<EOF
${PUBLIC_HOST} {
    tls {
        issuer acme https://acme-v02.api.letsencrypt.org/directory {
            profile shortlived
        }
    }
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
EOF
else
  cat > /etc/caddy/conf.d/chitoproject.caddy <<EOF
${PUBLIC_HOST} {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
EOF
fi
if ! grep -Fq 'import /etc/caddy/conf.d/*.caddy' /etc/caddy/Caddyfile; then
  printf '\nimport /etc/caddy/conf.d/*.caddy\n' >> /etc/caddy/Caddyfile
fi

caddy validate --config /etc/caddy/Caddyfile
systemctl enable --now caddy
systemctl daemon-reload
systemctl enable chitoproject-api chitoproject-worker chitoproject-telegram chitoproject-bale

cd "$APP_DIR"
runuser -u chitoproject -- "$VENV_DIR/bin/python" -m app.cli migrate
runuser -u chitoproject -- "$VENV_DIR/bin/python" -m app.cli issue-owner-code

systemctl restart caddy
systemctl enable --now chitoproject-api chitoproject-worker
# Bot services are started after their credentials are configured.
for i in {1..30}; do
  if curl --fail --silent -H "Host: ${PUBLIC_HOST}" http://127.0.0.1:8000/readyz >/dev/null; then break; fi
  if [[ "$i" == 30 ]]; then echo "Local readiness check failed; inspect service logs." >&2; exit 1; fi
  sleep 1
done

cat <<EOF

ChitoProject is installed at ${PUBLIC_HOST}.
The first owner code was printed above and expires after 24 hours. Use it once at:
  https://${PUBLIC_HOST}/admin/activate

Bot tokens and usernames are currently blank. Edit /etc/chitoproject/chitoproject.env,
then run: sudo systemctl restart chitoproject-api chitoproject-worker chitoproject-telegram chitoproject-bale

For later releases, run as root from the checkout:
  sudo bash ${APP_DIR}/scripts/deploy.sh
EOF
