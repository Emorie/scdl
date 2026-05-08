#!/usr/bin/env bash
set -e

cd "$(dirname "$0")/.."

echo "Pulling latest code from GitHub..."
git pull

echo "Rebuilding and restarting scdl-web..."
docker compose up -d --build

echo "Done."
echo "Open the app at: http://YOUR-NAS-IP:8090"
