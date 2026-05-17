#!/usr/bin/env bash
# update.sh — pull the latest code, rebuild the frontend, restart the
# backend. run as the `ploverai` service user from the repo root:
#
#   sudo -u ploverai -i
#   cd /var/lib/ploverai/app
#   ./deploy/update.sh
#
# the systemctl restart at the end + the sudo rsync need root; the
# script uses `sudo` for those steps, so you'll need passwordless
# sudo for the ploverai user OR run the whole thing as root (sudo
# bash deploy/update.sh).

set -euo pipefail

APP_DIR="${APP_DIR:-/var/lib/ploverai/app}"
WEB_DIR="${WEB_DIR:-/var/www/ploverai/out}"

cd "$APP_DIR"

echo "==> git pull"
git fetch --tags origin
git pull --ff-only

echo "==> backend deps"
cd "$APP_DIR/pipeline"
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
deactivate

echo "==> frontend build"
cd "$APP_DIR/frontend"
npm ci --silent          # ci is strict-but-faster than install for CI/deploy
npm run build

echo "==> sync static export → $WEB_DIR"
sudo rsync -a --delete "$APP_DIR/frontend/out/" "$WEB_DIR/"
sudo chown -R www-data:www-data /var/www/ploverai

echo "==> restart FastAPI"
sudo systemctl restart ploverai-api
sudo systemctl status ploverai-api --no-pager --lines=5

echo "==> reload nginx (in case any config changed)"
sudo nginx -t && sudo systemctl reload nginx

echo "update complete."
