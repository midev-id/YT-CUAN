#!/usr/bin/env bash
# Pull-based hub deploy, run every 2 minutes by yt-cuan-hub-deploy.timer on the hub server.
# The server reads the repo with a read-only deploy key; GitHub never gets access to the server.
# Rebuilds only when files the hub image uses changed, and rolls back if the new build isn't healthy.
set -euo pipefail
REPO=/opt/yt-cuan-hub/repo
PATHS=(hub app/static install.sh windows/install.ps1)
cd "$REPO"

exec 9>/run/yt-cuan-hub-deploy.lock
flock -n 9 || exit 0  # a deploy is already running

git fetch -q origin main
old=$(git rev-parse HEAD)
new=$(git rev-parse origin/main)
[ "$old" = "$new" ] && exit 0

if git diff --quiet "$old" "$new" -- "${PATHS[@]}"; then
  git reset -q --hard "$new"  # nothing the hub uses changed
  exit 0
fi

healthy() {
  for _ in $(seq 1 20); do
    curl -fsS http://127.0.0.1:9000/healthz >/dev/null 2>&1 && return 0
    sleep 3
  done
  return 1
}

echo "deploy $old -> $new"
git reset -q --hard "$new"
cd hub
if docker compose up -d --build --quiet-pull 2>&1 && healthy; then
  echo "deploy ok $(git rev-parse --short HEAD)"
  docker image prune -f >/dev/null
  exit 0
fi

echo "deploy FAILED, rolling back to $old" >&2
git reset -q --hard "$old"
docker compose up -d --build --quiet-pull 2>&1
healthy && echo "rollback ok" || echo "ROLLBACK ALSO UNHEALTHY" >&2
exit 1
