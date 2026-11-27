#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly domain=chitoproject.ir ipv4=46.249.107.214
readonly xray_config=/usr/local/etc/xray/config.json xray_port=10443
readonly caddy_config=/etc/caddy/Caddyfile
readonly caddy_site=/etc/caddy/conf.d/chitoproject-domain-443.caddy
readonly proxy_dir=/etc/chitoproject-checkout-proxy
readonly proxy_unit=chitoproject-checkout-proxy.service
readonly unit_file=/etc/systemd/system/$proxy_unit
readonly state=/var/backups/chitoproject-domain-443

fail() { printf 'Checkout domain: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail 'Run as root.'
[[ $# -le 1 && ${1:-install} =~ ^(install|rollback)$ ]] || fail 'Usage: setup-checkout-domain.sh [install|rollback]'
exec 9>/run/lock/chitoproject-domain-443.lock
flock -n 9 || fail 'Another checkout domain operation is running.'

restore() {
    local result=0
    if [[ -e "$unit_file" ]]; then
        systemctl daemon-reload || return 1
        systemctl stop "$proxy_unit" || return 1
        systemctl disable "$proxy_unit" || return 1
        rm -- "$unit_file" || return 1
        systemctl daemon-reload || return 1
    fi
    if [[ -f "$state/xray.changed" ]]; then
        cp -a -- "$state/xray.original" "$xray_config" || return 1
        systemctl restart xray.service || return 1
        rm -- "$state/xray.changed" || return 1
    fi
    if [[ -f "$state/caddy.changed" ]]; then
        rm -f -- "$caddy_site" || return 1
        systemctl reload caddy.service || return 1
        rm -- "$state/caddy.changed" || return 1
    fi
    if [[ -f "$state/backup.ready" ]]; then
        rm -f -- "$proxy_dir/haproxy.cfg" || result=1
        [[ ! -d "$proxy_dir" ]] || rmdir -- "$proxy_dir" || result=1
    fi
    if ((result == 0)); then
        rm -f -- "$state/installed" "$state/backup.ready" "$state/xray.original"
        rmdir -- "$state" || result=1
    fi
    return "$result"
}

if [[ ${1:-install} == rollback ]]; then
    [[ -f "$state/backup.ready" ]] || fail 'No completed backup found.'
    restore || fail "Rollback needs attention. Backup: $state/xray.original"
    printf 'Original Xray and Caddy settings restored.\n'
    exit
fi

probe() {
    curl --noproxy '*' --silent --show-error --fail --max-time 8 \
        --resolve "$domain:$1:127.0.0.1" "$2"
}

if [[ -f "$state/installed" ]]; then
    systemctl is-active --quiet "$proxy_unit"
    [[ "$(probe 443 "https://$domain/")" == ChitoProject ]] || fail 'The installed endpoint did not respond as expected.'
    printf 'Already configured: https://%s\n' "$domain"
    exit
fi

[[ ! -e "$state" && ! -e "$unit_file" && ! -e "$proxy_dir" && ! -e "$caddy_site" ]] \
    || fail 'An existing checkout proxy setup was found. Inspect it before proceeding.'
[[ -f "$xray_config" && ! -L "$xray_config" ]] || fail 'Expected a regular Xray configuration file.'
systemctl is-active --quiet xray.service || fail 'Xray is not active.'
systemctl is-active --quiet caddy.service || fail 'Caddy is not active.'
[[ "$(systemctl show xray.service -p ExecStart --value)" == *"/usr/local/bin/xray run -config $xray_config"* ]] \
    || fail 'The Xray service uses a different startup configuration.'
grep -Eq '^[[:space:]]*import[[:space:]]+/etc/caddy/conf\.d/\*\.caddy([[:space:]]|$)' "$caddy_config" \
    || fail 'Caddy does not import the expected conf.d directory.'
[[ "$(getent ahostsv4 "$domain." | awk '{print $1}' | sort -u)" == "$ipv4" ]] \
    || fail "Set the DNS-only A record for $domain to $ipv4, then run again."
[[ -z "$(ss -H -lun 'sport = :443')" ]] || fail 'UDP 443 is in use.'
[[ -z "$(ss -H -ltn "sport = :$xray_port")" ]] || fail "TCP $xray_port is in use."
xray_pid=$(systemctl show xray.service -p MainPID --value)
listeners=$(ss -H -ltnp 'sport = :443')
[[ -n "$listeners" ]] || fail 'TCP 443 has no listener.'
while IFS= read -r listener; do
    [[ "$listener" == *"pid=$xray_pid,"* ]] || fail 'A process other than Xray listens on TCP 443.'
done <<< "$listeners"

staging=$(mktemp -d)
xray_candidate=''
cleanup() {
    local code=$?
    trap - EXIT
    if ((code != 0)) && [[ -f "$state/backup.ready" ]]; then
        printf 'Restoring the previous listeners.\n' >&2
        restore || printf 'Rollback needs attention. Backup: %s/xray.original\n' "$state" >&2
    fi
    if [[ -n "$xray_candidate" ]]; then rm -f -- "$xray_candidate"; fi
    rm -rf -- "$staging"
    exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

python3 - "$xray_config" "$staging/xray.json" "$domain" "$xray_port" <<'PY'
import json
import sys
from pathlib import Path

source, destination, domain, port = sys.argv[1:]
try:
    config = json.loads(Path(source).read_text())
except json.JSONDecodeError as error:
    raise SystemExit(f"Xray config is not plain JSON (line {error.lineno}).") from None

inbounds = [item for item in config.get("inbounds", []) if str(item.get("port")) == "443"]
if len(inbounds) != 1:
    raise SystemExit("Expected exactly one Xray inbound on TCP 443.")
inbound = inbounds[0]
stream = inbound.get("streamSettings") or {}
reality = stream.get("realitySettings") or {}
if (inbound.get("protocol"), stream.get("network", "tcp"), stream.get("security")) not in {
    ("vless", "tcp", "reality"),
    ("vless", "raw", "reality"),
}:
    raise SystemExit("Expected a VLESS TCP REALITY inbound.")
for name in reality.get("serverNames") or []:
    if name.rstrip(".").lower() == domain:
        raise SystemExit("The checkout hostname is already used by Xray REALITY.")
for key in ("tcpSettings", "rawSettings"):
    if (stream.get(key) or {}).get("acceptProxyProtocol"):
        raise SystemExit("This inbound already expects PROXY protocol; inspect it before migrating.")

inbound["listen"] = "127.0.0.1"
inbound["port"] = int(port)
Path(destination).write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
PY

if [[ ! -x /usr/sbin/haproxy ]]; then
    command -v apt-get >/dev/null || fail 'Install HAProxy through your package manager.'
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y haproxy
fi
cat > "$staging/haproxy.cfg" <<'HAPROXY'
global
    user haproxy
    group haproxy

defaults
    mode tcp
    timeout connect 5s
    timeout client 1h
    timeout server 1h

frontend public_tls
    bind [::]:443 v4v6
    tcp-request inspect-delay 5s
    tcp-request content accept if { req.ssl_hello_type 1 }
    use_backend checkout_tls if { req.ssl_sni -i chitoproject.ir }
    default_backend xray_reality

backend checkout_tls
    server caddy 127.0.0.1:9443

backend xray_reality
    server xray 127.0.0.1:10443
HAPROXY

/usr/sbin/haproxy -c -f "$staging/haproxy.cfg"
if ! /usr/local/bin/xray run -test -config "$staging/xray.json" >"$staging/xray.test.log" 2>&1; then
    install -m 0600 "$staging/xray.test.log" /var/log/chitoproject-xray-config-test.log
    fail 'Xray validation failed. A private log is available in /var/log/chitoproject-xray-config-test.log.'
fi

mkdir -m 0700 -- "$state"
cp -a -- "$xray_config" "$state/xray.original"
touch "$state/backup.ready"

touch "$state/caddy.changed"
cat > "$caddy_site" <<'CADDY'
http://chitoproject.ir {
    redir https://chitoproject.ir{uri} 308
}

https://chitoproject.ir:9443 {
    tls {
        issuer acme {
            disable_tlsalpn_challenge
        }
    }

    @checkout path /orders /orders/* /static/checkout.css /static/checkout.js
    handle @checkout {
        reverse_proxy 127.0.0.1:8000 {
            header_up Host 46.249.107.214
        }
    }

    handle / {
        respond "ChitoProject" 200
    }

    handle {
        respond 404
    }
}
CADDY
chmod 0644 "$caddy_site"
caddy validate --config "$caddy_config"
systemctl reload caddy.service

# Obtain and verify the certificate before interrupting Xray.
for attempt in {1..15}; do
    if [[ "$(probe 9443 "https://$domain:9443/" 2>/dev/null)" == ChitoProject ]]; then
        break
    fi
    [[ $attempt != 15 ]] || fail 'Caddy TLS is not ready. Check inbound TCP 80 and the Caddy journal.'
    sleep 3
done

install -d -m 0755 "$proxy_dir"
install -m 0644 "$staging/haproxy.cfg" "$proxy_dir/haproxy.cfg"
cat > "$unit_file" <<'UNIT'
[Unit]
Description=ChitoProject checkout TCP routing
After=network.target xray.service caddy.service

[Service]
ExecStart=/usr/sbin/haproxy -db -f /etc/chitoproject-checkout-proxy/haproxy.cfg
Restart=on-failure
RestartSec=2
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 "$unit_file"
systemctl daemon-reload

# Place the candidate on the same filesystem for an atomic config replacement.
xray_candidate=$(mktemp "${xray_config}.checkout.XXXXXX")
cp --preserve=all -- "$xray_config" "$xray_candidate"
cat "$staging/xray.json" > "$xray_candidate"
touch "$state/xray.changed"
mv -T -- "$xray_candidate" "$xray_config"
xray_candidate=''
systemctl restart xray.service
for attempt in {1..10}; do
    if ss -H -ltnp "sport = :$xray_port" | grep -Fq "pid=$(systemctl show xray.service -p MainPID --value),"; then
        break
    fi
    [[ $attempt != 10 ]] || fail 'Xray did not open the local TCP port.'
    sleep 1
done

systemctl enable --now "$proxy_unit"
[[ "$(probe 443 "https://$domain/")" == ChitoProject ]] || fail 'HTTPS failed through HAProxy.'
status=$(curl --noproxy '*' --silent --show-error --max-time 8 \
    --resolve "$domain:443:127.0.0.1" --output /dev/null --write-out '%{http_code}' "https://$domain/admin")
[[ "$status" == 404 ]] || fail 'Unexpected response for the admin path on the checkout domain.'

touch "$state/installed"
printf 'Checkout HTTPS is ready: https://%s\n' "$domain"
printf 'Xray listens locally on TCP %s. Verify a real Xray client on public TCP 443.\n' "$xray_port"
