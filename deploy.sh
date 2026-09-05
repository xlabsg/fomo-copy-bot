#!/usr/bin/env bash
# Deploys CopyRouter to Robinhood Chain mainnet from the PRIVATE_KEY in .env,
# then paste the printed address into config.json "router".
set -euo pipefail
cd "$(dirname "$0")"
set -a; source .env; set +a
RPC="${RPC_URL:-https://rpc.mainnet.chain.robinhood.com}"
forge create --root contracts src/CopyRouter.sol:CopyRouter \
  --rpc-url "$RPC" --private-key "$PRIVATE_KEY" --broadcast \
  --constructor-args 0xCaf681a66D020601342297493863E78C959E5cb2 0x8366a39CC670B4001A1121B8F6A443A643e40951
