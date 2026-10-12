#!/usr/bin/env bash
# Pull the latest code and rebuild only if something changed. Run nightly by
# cron (installed by setup-vps.sh) and on demand by the ops service's `update`.
set -euo pipefail
cd /opt/ntu-now-mcp
before="$(git rev-parse HEAD)"
git fetch --quiet origin
git reset --hard --quiet origin/main
after="$(git rev-parse HEAD)"
if [[ "$before" != "$after" ]]; then
  echo "$(date -Is) updating ${before:0:7} -> ${after:0:7}"
  docker compose up -d --build
  docker image prune -f >/dev/null
  /opt/ntu-now-ops/.venv/bin/pip install --quiet --upgrade "mcp>=2.2,<3" || true
else
  echo "$(date -Is) up to date (${after:0:7})"
fi
