#!/usr/bin/env bash
# Run ON the droplet (Ubuntu 22.04/24.04) as root, from /opt/rh-copybot.
# Installs Python deps into a venv and registers both bots as systemd services.
set -euo pipefail
cd "$(dirname "$0")/.."
# Package installs are expensive on a 512MB box (an apt-get run got OOM-killed
# with four bots resident): only touch apt/pip when actually needed.
if [ ! -d .venv ]; then
  apt-get update -qq && apt-get install -y -qq python3-venv python3-pip rsync >/dev/null
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
fi
if [ ! -f .venv/.requirements.sha ] || ! sha256sum -c --quiet .venv/.requirements.sha 2>/dev/null; then
  .venv/bin/pip install -q -r requirements.txt && sha256sum requirements.txt > .venv/.requirements.sha
else
  echo "requirements unchanged; skipping pip"
fi
mkdir -p data ../rh-copybot-paper/data
[ -e ../rh-copybot-paper/wallets.json ] || ln -s ../rh-copybot/wallets.json ../rh-copybot-paper/wallets.json
install -m 644 deploy/rh-copybot.service /etc/systemd/system/rh-copybot.service
install -m 644 deploy/rh-copybot-paper.service /etc/systemd/system/rh-copybot-paper.service
install -m 644 deploy/rh-copybot-notify.service /etc/systemd/system/rh-copybot-notify.service
install -m 644 deploy/rh-copybot-paper2.service /etc/systemd/system/rh-copybot-paper2.service
[ -d ../rh-copybot-paper2 ] && systemctl enable rh-copybot-paper2 >/dev/null
systemctl daemon-reload
systemctl enable rh-copybot rh-copybot-paper >/dev/null
echo "installed. start with:  systemctl start rh-copybot rh-copybot-paper"
echo "logs:                   journalctl -fu rh-copybot"
