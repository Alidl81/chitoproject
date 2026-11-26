#!/usr/bin/env bash
set -Eeuo pipefail

# Copy ourselves before an in-place checkout or app-tree update can replace this file.
if [[ "${CHITOPROJECT_DEPLOY_BOOTSTRAPPED:-0}" != 1 && -f "${BASH_SOURCE[0]}" ]]; then
  SELF_COPY="$(mktemp /tmp/chitoproject-deploy.XXXXXX)"
  install -m 0700 "${BASH_SOURCE[0]}" "$SELF_COPY"
  export CHITOPROJECT_DEPLOY_BOOTSTRAPPED=1 CHITOPROJECT_DEPLOY_SCRIPT_COPY="$SELF_COPY"
  exec bash "$SELF_COPY" "$@"
fi
if [[ "${CHITOPROJECT_DEPLOY_BOOTSTRAPPED:-0}" == 1 ]]; then
  trap 'rm -f -- "$CHITOPROJECT_DEPLOY_SCRIPT_COPY"' EXIT
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root so systemd can restart services: sudo bash /opt/chitoproject/app/scripts/deploy.sh" >&2
  exit 2
fi

readonly APP_DIR=/opt/chitoproject/app
readonly REPO_CANDIDATE=/opt/chitoproject-repo
readonly INSTALL_DIR=/opt/chitoproject
readonly VENV_DIR=/opt/chitoproject/venv
readonly PYTHON_ROOT=/opt/chitoproject-python
readonly ENV_FILE=/etc/chitoproject/chitoproject.env
readonly DEPLOY_KEY=/opt/chitoproject/.ssh/id_ed25519
readonly KNOWN_HOSTS=/opt/chitoproject/.ssh/known_hosts
readonly EXPECTED_REMOTE=git@github.com:Alidl81/chitoproject.git
readonly UNITS=(chitoproject-api chitoproject-worker chitoproject-telegram chitoproject-bale)

fail() { echo "ChitoProject deploy stopped: $*" >&2; exit 2; }

[[ -d "$APP_DIR" && ! -L "$APP_DIR" ]] || fail "expected application directory missing or is a symlink: $APP_DIR"
[[ -d "$INSTALL_DIR" && -d /var/lib/chitoproject ]] || fail "installation or data directory is missing."
[[ -e "$ENV_FILE" && -f "$ENV_FILE" ]] || fail "private configuration file is missing; nothing was changed."
[[ -L "$APP_DIR/.env" && "$(readlink -- "$APP_DIR/.env")" == "$ENV_FILE" ]] || \
  fail "$APP_DIR/.env must remain a symlink to $ENV_FILE; inspect it before deployment."
[[ -r "$DEPLOY_KEY" && -r "$KNOWN_HOSTS" ]] || fail "chitoproject's read-only deploy key or verified known_hosts is unavailable."
runuser -u chitoproject -- test -r "$DEPLOY_KEY" || fail "service user chitoproject cannot read the deploy key."
runuser -u chitoproject -- test -r "$KNOWN_HOSTS" || fail "service user chitoproject cannot read known_hosts."

UV_BIN="${UV_BIN:-}"
if [[ -n "$UV_BIN" ]]; then
  [[ -x "$UV_BIN" ]] || fail "UV_BIN is not executable: $UV_BIN"
  runuser -u chitoproject -- test -x "$UV_BIN" || fail "chitoproject cannot execute uv at $UV_BIN."
else
  for candidate in "$(command -v uv 2>/dev/null || true)" /usr/local/bin/uv /usr/bin/uv /opt/chitoproject-python/bin/uv; do
    if [[ -n "$candidate" && -x "$candidate" ]] && runuser -u chitoproject -- test -x "$candidate"; then
      UV_BIN="$candidate"
      break
    fi
  done
  [[ -n "$UV_BIN" ]] || fail "uv executable accessible to chitoproject was not found. Set UV_BIN to its path."
fi
[[ -x "$VENV_DIR/bin/python" ]] || fail "the application virtual environment is missing."
python3 - "$VENV_DIR/bin/python" "$PYTHON_ROOT" <<'PY'
import os
import subprocess
import sys

python, root = sys.argv[1:]
probe = subprocess.run(
    [python, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3]))); print(sys.base_prefix)"],
    check=True, capture_output=True, text=True,
)
version, base_prefix = probe.stdout.splitlines()
if version != "3.12.16" or os.path.commonpath((os.path.realpath(base_prefix), os.path.realpath(root))) != os.path.realpath(root):
    raise SystemExit("ChitoProject deploy stopped: venv must use uv-managed Python 3.12.16 in /opt/chitoproject-python.")
PY

SOURCE_UPDATED=0
APP_SWAPPED=0
APP_ROLLED_BACK=0
SERVICES_STOPPED=0
DEPLOY_SUCCESS=0
REPO_DIR=""
OLD_SHA=""
NEW_SHA=""
STAGE_DIR=""
EXPECTED_DIR=""
APP_BACKUP=""
ACTIVE_BEFORE=()

rollback_on_failure() {
  local status="$1"
  if [[ "$DEPLOY_SUCCESS" == 1 ]]; then return 0; fi
  if [[ "$SERVICES_STOPPED" == 1 ]]; then
    for unit in "${UNITS[@]}"; do systemctl stop "$unit.service" 2>/dev/null || true; done
  fi
  if [[ "$APP_SWAPPED" == 1 && -d "$APP_BACKUP" ]]; then
    rm -rf -- "$APP_DIR"
    mv -- "$APP_BACKUP" "$APP_DIR"
    APP_SWAPPED=0
    APP_ROLLED_BACK=1
  fi
  if [[ "$SOURCE_UPDATED" == 1 && -n "$OLD_SHA" && -n "$REPO_DIR" ]]; then
    git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" reset --hard "$OLD_SHA" >/dev/null 2>&1 || true
  fi
  if [[ "$APP_ROLLED_BACK" == 1 && -x "$VENV_DIR/bin/python" && -d "$APP_DIR" ]]; then
    runuser -u chitoproject -- "$UV_BIN" pip install --python "$VENV_DIR/bin/python" -e "$APP_DIR" >/dev/null 2>&1 || true
  fi
  for unit in "${ACTIVE_BEFORE[@]}"; do systemctl restart "$unit.service" >/dev/null 2>&1 || true; done
  [[ -z "$EXPECTED_DIR" ]] || rm -rf -- "$EXPECTED_DIR"
  [[ -z "$STAGE_DIR" ]] || rm -rf -- "$STAGE_DIR"
  return 0
}
finish_deploy() {
  local status=$?
  trap - EXIT
  rollback_on_failure "$status"
  if [[ -n "${CHITOPROJECT_DEPLOY_SCRIPT_COPY:-}" ]]; then
    rm -f -- "$CHITOPROJECT_DEPLOY_SCRIPT_COPY"
  fi
  exit "$status"
}
trap finish_deploy EXIT

[[ -d "$REPO_CANDIDATE/.git" || -f "$REPO_CANDIDATE/.git" ]] || \
  fail "the expected private source checkout is missing at $REPO_CANDIDATE."
REPO_DIR="$REPO_CANDIDATE"
git_repo() { git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" "$@"; }

export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS"
REMOTE_URL="$(git_repo remote get-url origin 2>/dev/null)" || fail "source checkout has no readable origin remote."
[[ "$REMOTE_URL" == "$EXPECTED_REMOTE" || "$REMOTE_URL" == "ssh://git@github.com/Alidl81/chitoproject.git" || "$REMOTE_URL" == "https://github.com/Alidl81/chitoproject.git" ]] || fail "origin is not the configured ChitoProject repository."
[[ "$(git_repo branch --show-current)" == main ]] || fail "source checkout is not on main."
REPO_STATUS="$(git_repo status --porcelain=v1 --untracked-files=all)" || fail "cannot inspect source checkout."
[[ -z "$REPO_STATUS" ]] || \
  fail "source checkout has modified or untracked files; review and preserve them first."

# Reject unexpected ignored files as well; only runtime Python caches and an app .env link are allowed.
python3 - "$REPO_DIR" "$APP_DIR" <<'PY'
import os
import subprocess
import sys

repo, app = map(os.path.realpath, sys.argv[1:])
is_app_repo = repo == app
tracked = set(os.fsdecode(subprocess.check_output(
    ["git", "-c", f"safe.directory={repo}", "-C", repo, "ls-files", "-z"]
)).split("\0")) - {""}
tracked_dirs = {path.rsplit("/", 1)[0] for path in tracked if "/" in path}
for directory in tuple(tracked_dirs):
    while "/" in directory:
        directory = directory.rsplit("/", 1)[0]
        tracked_dirs.add(directory)
allowed_cache = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")

def generated(path):
    parts = path.split("/")
    return (any(part in allowed_cache for part in parts)
            or any(part.endswith(".egg-info") for part in parts)
            or any(part.endswith((".pyc", ".pyo")) for part in parts))

unexpected = []
for current, dirs, files in os.walk(repo, topdown=True, followlinks=False):
    rel_dir = os.path.relpath(current, repo)
    dirs[:] = [name for name in dirs if not (rel_dir == "." and name == ".git")]
    for name in list(dirs):
        path = os.path.join(current, name)
        rel = os.path.relpath(path, repo).replace(os.sep, "/")
        if os.path.islink(path):
            unexpected.append(rel)
        elif rel not in tracked_dirs and not generated(rel):
            unexpected.append(rel + "/")
    for name in files:
        path = os.path.join(current, name)
        rel = os.path.relpath(path, repo).replace(os.sep, "/")
        if rel == ".git":
            continue
        if rel == ".env" and is_app_repo and os.path.islink(path) and os.readlink(path) == "/etc/chitoproject/chitoproject.env":
            continue
        if rel in tracked:
            continue
        if generated(rel):
            continue
        unexpected.append(rel)
if unexpected:
    raise SystemExit("ChitoProject deploy stopped: unexpected local path(s) in source checkout: " + ", ".join(sorted(unexpected)[:12]))
PY

for unit in "${UNITS[@]}"; do
  systemctl show "$unit.service" >/dev/null 2>&1 || fail "systemd unit $unit.service is missing."
  [[ "$(systemctl show -p User --value "$unit.service")" == chitoproject ]] || fail "$unit.service is not configured for the chitoproject service account."
  [[ -z "$(systemctl show -p DropInPaths --value "$unit.service")" ]] || fail "$unit.service has local drop-in overrides; review them before deployment."
done

OLD_SHA="$(git_repo rev-parse HEAD)"
git_repo fetch --no-tags "$EXPECTED_REMOTE" +refs/heads/main:refs/remotes/origin/main
NEW_SHA="$(git_repo rev-parse refs/remotes/origin/main)"
[[ $# -le 1 ]] || fail "usage: deploy.sh [expected-main-commit]"
[[ $# -eq 0 || "$1" == "$NEW_SHA" ]] || fail "origin/main changed after the deploy script was selected."
git_repo merge-base --is-ancestor "$OLD_SHA" "$NEW_SHA" || \
  fail "origin/main is not a fast-forward from the local main; no code was deployed."
APP_SHA="$OLD_SHA"
APP_HAS_GIT=0
if [[ -d "$APP_DIR/.git" && ! -L "$APP_DIR/.git" ]]; then
  APP_HAS_GIT=1
  APP_SHA="$(git -c safe.directory="$APP_DIR" -C "$APP_DIR" rev-parse --verify HEAD)" || \
    fail "cannot identify the installed Git checkout."
  git_repo merge-base --is-ancestor "$APP_SHA" "$NEW_SHA" || \
    fail "installed Git checkout is outside the verified main history."
fi
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EXPECTED_DIR="$(mktemp -d "$INSTALL_DIR/.app-expected.XXXXXX")"
git_repo archive "$APP_SHA" | tar -x -C "$EXPECTED_DIR"
python3 - "$APP_DIR" "$EXPECTED_DIR" "$ENV_FILE" "$APP_HAS_GIT" <<'PY'
import hashlib
import os
import sys

actual_root, expected_root, env_file, app_has_git = sys.argv[1:]
actual_root, expected_root = map(os.path.realpath, (actual_root, expected_root))
allowed_cache = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")

def generated(path):
    parts = path.split("/")
    return (any(part in allowed_cache for part in parts)
            or any(part.endswith(".egg-info") for part in parts)
            or any(part.endswith((".pyc", ".pyo")) for part in parts))

expected_files = set()
expected_dirs = {"."}
for current, dirs, files in os.walk(expected_root):
    rel_dir = os.path.relpath(current, expected_root)
    for name in dirs:
        rel = os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/")
        expected_dirs.add(rel)
    for name in files:
        rel = os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/")
        expected_files.add(rel)

unexpected = []
missing = []
modified = []
actual_files = set()
for current, dirs, files in os.walk(actual_root, topdown=True, followlinks=False):
    rel_dir = os.path.relpath(current, actual_root)
    if rel_dir == "." and app_has_git == "1":
        dirs[:] = [name for name in dirs if name != ".git"]
    for name in list(dirs):
        path = os.path.join(current, name)
        rel = os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/")
        if os.path.islink(path):
            unexpected.append(rel)
        elif rel not in expected_dirs and not generated(rel):
            unexpected.append(rel + "/")
    for name in files:
        path = os.path.join(current, name)
        rel = os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/")
        if rel == ".env":
            if not os.path.islink(path) or os.readlink(path) != env_file:
                modified.append(".env (expected a preserved external configuration symlink)")
            continue
        if os.path.islink(path):
            unexpected.append(rel)
        elif rel in expected_files:
            actual_files.add(rel)
        elif not generated(rel):
            unexpected.append(rel)

for rel in sorted(expected_files - actual_files):
    missing.append(rel)
for rel in sorted(expected_files & actual_files):
    with open(os.path.join(actual_root, rel), "rb") as actual, open(os.path.join(expected_root, rel), "rb") as expected:
        if hashlib.sha256(actual.read()).digest() != hashlib.sha256(expected.read()).digest():
            modified.append(rel)
if unexpected or missing or modified:
    parts = []
    if modified: parts.append("modified: " + ", ".join(modified[:8]))
    if missing: parts.append("missing: " + ", ".join(missing[:8]))
    if unexpected: parts.append("unexpected: " + ", ".join(unexpected[:8]))
    raise SystemExit("ChitoProject deploy stopped: application tree does not match the checked-out release (" + "; ".join(parts) + ").")
PY

git_repo -c core.hooksPath=/dev/null merge --ff-only --no-edit refs/remotes/origin/main >/dev/null
SOURCE_UPDATED=1

if [[ "$APP_SHA" == "$NEW_SHA" ]]; then
  rm -rf -- "$EXPECTED_DIR"
  EXPECTED_DIR=""
  DEPLOY_SUCCESS=1
  echo "ChitoProject is already current on main and the installed app matches $NEW_SHA"
  exit 0
fi

APP_BACKUP="$INSTALL_DIR/.app-rollback-$STAMP"
[[ ! -e "$APP_BACKUP" && ! -L "$APP_BACKUP" ]] || fail "a prior deployment staging path exists; inspect it before continuing."
STAGE_DIR="$(mktemp -d "$INSTALL_DIR/.app-stage.XXXXXX")"
git_repo archive HEAD | tar -x -C "$STAGE_DIR"
chown -R chitoproject:chitoproject "$STAGE_DIR"
cp -a -- "$APP_DIR/.env" "$STAGE_DIR/.env"

for unit in "${UNITS[@]}"; do
  if systemctl is-active --quiet "$unit.service"; then ACTIVE_BEFORE+=("$unit"); fi
done
SERVICES_STOPPED=1
for unit in chitoproject-bale chitoproject-telegram chitoproject-worker chitoproject-api; do
  systemctl stop "$unit.service"
done

mv -- "$APP_DIR" "$APP_BACKUP"
APP_SWAPPED=1
mv -- "$STAGE_DIR" "$APP_DIR"

runuser -u chitoproject -- "$UV_BIN" pip install --python "$VENV_DIR/bin/python" -e "$APP_DIR"
runuser -u chitoproject -- bash -c "cd '$APP_DIR' && '$VENV_DIR/bin/python' -m app.cli migrate"

for unit in chitoproject-api chitoproject-worker chitoproject-telegram chitoproject-bale; do
  token_name=""
  case "$unit" in
    chitoproject-telegram) token_name=TELEGRAM_BOT_TOKEN ;;
    chitoproject-bale) token_name=BALE_BOT_TOKEN ;;
  esac
  has_token=0
  if [[ -n "$token_name" ]] && python3 - "$ENV_FILE" "$token_name" <<'PY'
import sys

path, expected = sys.argv[1:]
value = ""
with open(path, encoding="utf-8") as stream:
    for line in stream:
        key, separator, candidate = line.partition("=")
        if separator and key.strip() == expected:
            value = candidate.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            break
sys.exit(0 if value else 1)
PY
  then
    has_token=1
  fi
  if systemctl is-enabled --quiet "$unit.service" || [[ " ${ACTIVE_BEFORE[*]} " == *" $unit "* ]] || [[ "$has_token" == 1 ]]; then
    systemctl restart "$unit.service"
  fi
done

PUBLIC_HOST="$(runuser -u chitoproject -- bash -c \
  "cd '$APP_DIR' && '$VENV_DIR/bin/python' -c 'from urllib.parse import urlparse; from app.config import settings; print(urlparse(settings.public_base_url).hostname or \"\")'")"
[[ -n "$PUBLIC_HOST" ]] || fail "PUBLIC_BASE_URL has no host; service startup was attempted but health checks were skipped."
READY=0
for _ in {1..30}; do
  if curl --fail --silent --show-error -H "Host: $PUBLIC_HOST" http://127.0.0.1:8000/healthz >/dev/null 2>&1 && \
     curl --fail --silent --show-error -H "Host: $PUBLIC_HOST" http://127.0.0.1:8000/readyz >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done
[[ "$READY" == 1 ]] || fail "local /healthz or /readyz check failed; inspect chitoproject-api and Caddy logs."
for unit in chitoproject-api chitoproject-worker; do
  systemctl is-active --quiet "$unit.service" || fail "$unit.service is not active after restart."
done
for unit in chitoproject-telegram chitoproject-bale; do
  token_name=TELEGRAM_BOT_TOKEN
  [[ "$unit" != chitoproject-bale ]] || token_name=BALE_BOT_TOKEN
  if python3 - "$ENV_FILE" "$token_name" <<'PY'
import sys

path, expected = sys.argv[1:]
value = ""
with open(path, encoding="utf-8") as stream:
    for line in stream:
        key, separator, candidate = line.partition("=")
        if separator and key.strip() == expected:
            value = candidate.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            break
sys.exit(0 if value else 1)
PY
  then
    systemctl is-active --quiet "$unit.service" || fail "$unit.service has a configured bot token but is not active after restart."
  fi
done

DEPLOY_SUCCESS=1
rm -rf -- "$EXPECTED_DIR" "$STAGE_DIR"
echo "ChitoProject deployed from main: $OLD_SHA -> $NEW_SHA"
if [[ -n "$APP_BACKUP" && -d "$APP_BACKUP" ]]; then
  echo "Previous app code is preserved at $APP_BACKUP (configuration, database, and uploads remain outside the app tree)."
fi
