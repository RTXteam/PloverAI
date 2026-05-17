#!/usr/bin/env bash
# bootstrap.sh — one-shot initial setup for a fresh Ubuntu 22.04
# EC2 instance. run this as a sudoer user (e.g. `ubuntu`) after
# SSH'ing in. it installs system packages, creates the `ploverai`
# service user, and clones the repo. it does NOT do the per-deploy
# steps (.env values, frontend build, nginx vhost, TLS) — those need
# values that vary per instance and are configured manually.
#
# usage:
#   curl -fsSL https://raw.githubusercontent.com/<org>/<repo>/main/deploy/bootstrap.sh | sudo bash -s -- <git-url>
#
# or after manual SSH:
#   sudo ./bootstrap.sh https://github.com/<org>/<repo>.git

set -euo pipefail

REPO_URL="${1:-}"
if [[ -z "$REPO_URL" ]]; then
    echo "usage: $0 <git-url>"
    echo "example: $0 https://github.com/RamseyLab/ploverai.git"
    exit 1
fi

# refuse to run on anything but Ubuntu — the apt commands won't work
# on AL2023 and we don't want partial-success runs.
if ! grep -qi ubuntu /etc/os-release; then
    echo "this bootstrap script targets Ubuntu 22.04. you appear to be on:"
    grep ^PRETTY_NAME /etc/os-release
    echo "for Amazon Linux 2023, install the equivalent packages manually."
    exit 1
fi

echo "==> system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get upgrade -y
apt-get install -y \
    nginx \
    python3.12 python3.12-venv python3-pip \
    apache2-utils \
    certbot python3-certbot-nginx \
    git \
    rsync \
    curl \
    build-essential

echo "==> node.js 22.x (build-time only)"
if ! command -v node >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y nodejs
fi
node --version

echo "==> service user 'ploverai'"
if ! id ploverai >/dev/null 2>&1; then
    useradd --system --create-home --shell /bin/bash --home-dir /var/lib/ploverai ploverai
fi
mkdir -p /var/log/ploverai
chown ploverai:ploverai /var/log/ploverai

echo "==> repo clone"
if [[ ! -d /var/lib/ploverai/app ]]; then
    sudo -u ploverai git clone "$REPO_URL" /var/lib/ploverai/app
else
    echo "    /var/lib/ploverai/app already exists — skipping clone."
fi

echo "==> /var/www/ploverai (nginx will serve from here)"
mkdir -p /var/www/ploverai
chown -R www-data:www-data /var/www/ploverai

echo
echo "==================================================================="
echo "bootstrap complete. next steps:"
echo "  1. sudo -u ploverai -i        # become the app user"
echo "  2. cd /var/lib/ploverai/app/pipeline && python3.12 -m venv .venv"
echo "  3. set up pipeline/.env and frontend/.env.local"
echo "  4. install backend deps + build frontend"
echo "  5. install nginx vhost + ploverai-api.service"
echo "  6. run certbot --nginx -d <your-domain>"
echo "==================================================================="
