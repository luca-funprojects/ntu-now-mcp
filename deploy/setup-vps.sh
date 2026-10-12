#!/usr/bin/env bash
# One-shot setup for a fresh Ubuntu 24.04 server. Run as root from inside the
# unpacked/cloned ntu-now-mcp directory (cloud-init does this for you):
#
#   bash deploy/setup-vps.sh now-mcp.yourdomain.com
#
# Installs Docker, locks the firewall to SSH + HTTP/HTTPS, writes .env with
# fresh secrets, builds and starts the connector + Caddy, installs the ops
# service (maintenance over MCP) and the nightly auto-update, and writes the
# two connector URLs to CONNECTOR_URL and OPS_URL.  Re-running is safe.

set -euo pipefail

HOST="${1:-}"
if [[ -z "$HOST" ]]; then
  echo "usage: bash deploy/setup-vps.sh <public hostname pointing at this server>" >&2
  exit 1
fi
if [[ ! -f docker-compose.yml || ! -d now_mcp ]]; then
  echo "run this from inside the ntu-now-mcp directory" >&2
  exit 1
fi
APP_DIR="$(pwd)"

echo "==> Installing Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

echo "==> Firewall: allow SSH, HTTP (for the certificate), HTTPS only"
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
fi

echo "==> Writing .env"
[[ -f .env ]] || cp .env.example .env
gen() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }
set_env() {  # set_env KEY VALUE  (adds or replaces the line)
  if grep -qE "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else printf '%s=%s\n' "$1" "$2" >> .env; fi
}
SECRET="$(grep -E '^NOW_HTTP_SECRET=' .env | cut -d= -f2- || true)"
if [[ -z "$SECRET" || "$SECRET" == "change-me" ]]; then SECRET="$(gen)"; set_env NOW_HTTP_SECRET "$SECRET"; fi
OPS="$(grep -E '^OPS_SECRET=' .env | cut -d= -f2- || true)"
if [[ -z "$OPS" ]]; then OPS="$(gen)"; set_env OPS_SECRET "$OPS"; fi
set_env NOW_PUBLIC_HOST "$HOST"
chmod 600 .env
mkdir -p state data
chmod 700 state

OPS_ENABLE="$(grep -E '^OPS_ENABLE=' .env | cut -d= -f2- || true)"
if [[ "$OPS_ENABLE" == "1" ]]; then
  echo "==> Ops service (maintenance over MCP) — enabled in .env"
  apt-get install -y -q python3-venv >/dev/null
  if [[ ! -x /opt/ntu-now-ops/.venv/bin/python ]]; then
    python3 -m venv /opt/ntu-now-ops/.venv
  fi
  /opt/ntu-now-ops/.venv/bin/pip install --quiet --upgrade pip "mcp>=2.2,<3"
  sed "s|/opt/ntu-now-mcp|$APP_DIR|g" deploy/ntu-now-ops.service > /etc/systemd/system/ntu-now-ops.service
  systemctl daemon-reload
  systemctl enable --now ntu-now-ops
  systemctl restart ntu-now-ops
else
  echo "==> Ops service not enabled (set OPS_ENABLE=1 in .env and re-run to install it)"
fi

echo "==> Nightly code updates (04:17) and automatic OS security updates"
chmod +x deploy/auto-update.sh
echo "17 4 * * * root $APP_DIR/deploy/auto-update.sh >> /var/log/now-mcp-update.log 2>&1" > /etc/cron.d/now-mcp-update
if apt-get install -y -q unattended-upgrades >/dev/null 2>&1; then
  printf 'Unattended-Upgrade::Automatic-Reboot "true";\nUnattended-Upgrade::Automatic-Reboot-Time "05:00";\n' > /etc/apt/apt.conf.d/52now-reboot
  systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
fi

echo "==> Building and starting (first build takes a few minutes)"
docker compose up -d --build

echo "https://${HOST}/${SECRET}/mcp" > CONNECTOR_URL
echo "https://${HOST}/${SECRET}/board" > BOARD_URL
if [[ "$OPS_ENABLE" == "1" ]]; then echo "https://${HOST}/${OPS}/mcp" > OPS_URL; else rm -f OPS_URL; fi

echo
echo "==> Waiting for the certificate and health check"
for _ in $(seq 1 30); do
  if curl -fsS "https://${HOST}/healthz" >/dev/null 2>&1; then break; fi
  sleep 5
done
curl -sS "https://${HOST}/healthz" || echo "(not healthy yet: check DNS for ${HOST} and 'docker compose logs caddy')"
echo
echo "=================================================================="
echo " NOW connector URL (claude.ai -> Settings -> Connectors -> Add custom):"
echo "   $(cat CONNECTOR_URL)"
echo " Study Board (open in any browser; bookmark it, the address is the key):"
echo "   $(cat BOARD_URL)"
if [[ -f OPS_URL ]]; then
  echo " Server ops URL (add as a second connector, e.g. 'NTU Server Ops'):"
  echo "   $(cat OPS_URL)"
fi
echo
echo " Next: get a NOW session onto the server, either"
echo "   scp state/now.storage.json root@${HOST}:${APP_DIR}/state/      (from the laptop, after 'now-login')"
echo " or"
echo "   docker compose exec now-mcp python -m now_mcp.login --headless   (needs NOW_USERNAME/NOW_PASSWORD in .env)"
echo "=================================================================="
