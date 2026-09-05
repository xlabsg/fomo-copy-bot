#!/usr/bin/env bash
# Run on your LAPTOP:  ./deploy/push.sh root@DROPLET_IP
# Copies both bot folders (code, config, wallets, .env, and data/ = open positions)
# to the droplet and runs setup there. Safe to re-run to push updates.
set -euo pipefail
HOST="${1:?usage: push.sh root@IP}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PAPER="$HERE/../rh-copybot-paper"
if pgrep -f "^python[0-9.]* .*bot\.py" >/dev/null; then
  echo "!! a bot is still running on this laptop. Stop it first (Ctrl+C) so two copies never trade with the same wallet."; exit 1
fi
# never ship code that cannot even be imported (the live bot would crash-loop)
TMP="$(mktemp -d)"; mkdir -p "$TMP/data"; cp "$HERE/wallets.json" "$TMP/"
python3 -c "import json;c=json.load(open('$HERE/config.json'));c.update(live=False,router='');json.dump(c,open('$TMP/config.json','w'))"
if ! RPC_URL=https://rpc.mainnet.chain.robinhood.com BOT_HOME="$TMP" python3 -c "import sys;sys.path.insert(0,'$HERE');import bot" >/dev/null 2>&1; then
  echo "!! bot.py fails to import — not pushing"; rm -rf "$TMP"; exit 1
fi
rm -rf "$TMP"
ssh "$HOST" 'mkdir -p /opt/rh-copybot /opt/rh-copybot-paper'
# The droplet's data/ (positions, logs, pairing) and .env (keys, tokens) are the
# live truth once deployed: NEVER overwrite them on an update. They are only
# seeded on the very first push (remote has no state.json yet).
if ssh "$HOST" 'test -f /opt/rh-copybot/data/state.json'; then
  LIVE_EXCL=(--exclude data --exclude .env); PAPER_EXCL=(--exclude data)
  echo "update: leaving the droplet's data/ and .env untouched"
else
  LIVE_EXCL=(--exclude data/pricepaths); PAPER_EXCL=()
  echo "first deploy: seeding data/ and .env from this laptop"
fi
rsync -az --delete --exclude .venv --exclude __pycache__ --exclude contracts/out --exclude contracts/cache \
  "${LIVE_EXCL[@]}" "$HERE/" "$HOST:/opt/rh-copybot/"
rsync -az --exclude __pycache__ --exclude wallets.json "${PAPER_EXCL[@]}" "$PAPER/" "$HOST:/opt/rh-copybot-paper/"
PAPER2="$HERE/../rh-copybot-paper2"
if [ -d "$PAPER2" ]; then
  ssh "$HOST" 'mkdir -p /opt/rh-copybot-paper2'
  rsync -az --exclude __pycache__ "${PAPER_EXCL[@]}" "$PAPER2/" "$HOST:/opt/rh-copybot-paper2/"
fi
ssh "$HOST" 'chmod +x /opt/rh-copybot/deploy/setup.sh && /opt/rh-copybot/deploy/setup.sh'
echo
echo "next:  ssh $HOST 'systemctl restart rh-copybot rh-copybot-paper && journalctl -fu rh-copybot'"
