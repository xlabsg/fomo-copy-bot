# FOMO Auto-Discovery & Copy Trading Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

An autonomous copy-trading system on **Robinhood Chain (EVM)** that **automatically discovers high-performing traders from [FOMO](https://fomo.family)**, dynamically scores and filters them, and mirrors their new token entries with automated risk control and staged exits.

---

## Key Features

1. **Automated Smart Money Discovery (`auto_discovery.py`)**
   - Continuously pulls multi-window leaderboards (`24h`, `7d`, `30d`, `all`) from FOMO.
   - Resolves handles to verified on-chain EVM wallet addresses.
   - Quantitative quality scoring (PnL, volume proof, trade frequency, consistency, social proof).
   - Anti-Rug & anti-bot filters (weeds out dev wallets, micro-scalpers/MEV, zero-liquidity tokens).
   - Atomically updates `wallets.json` with backups.

2. **High-Performance On-Chain Copy Engine (`bot.py`)**
   - Single batch `eth_getLogs` Transfer polling (~0.5s): 100 wallets incur the same RPC load as 1 wallet.
   - **Hot-Reloading**: Automatically detects updates to `wallets.json` and refreshes the watch list in memory without process restart or position disruption.
   - Uniswap V3 & V4 routing on Robinhood Chain (`discover_route`), gas optimization, and pre-approvals.
   - Risk guards: `min_liquidity_usd`, `max_price_impact_pct`, signal age timeout, honeypot validation.

3. **Dual-Loop Exit Management**
   - **Staged Exits**: e.g., sell 75% after 5 minutes, sell 25% after 10 minutes.
   - **Origin Trader Tracking**: Automatically liquidates the remaining position the moment the tracked trader sells.

4. **Paper Trading & Live Execution**
   - **Paper Mode** (`"live": false`): Fully simulated fills at real on-chain quotes without risking capital.
   - **Live Mode** (`"live": true`): Real on-chain execution via `CopyRouter` contract.

5. **Terminal Dashboard & Analytics**
   - `dash.py`: Full-screen curses interactive terminal UI (view open positions, signals, manual exits).
   - `stats.py`: Post-trade analytics, latency analysis, and pool-level swap replay.

---

## Quick Start

### 1. Installation

```bash
cd /Users/mark/GitHub/xlabsg/fomo-copy-bot

# Using uv (recommended)
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt

# Or using standard python venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Auto-Discover Top FOMO Traders

Discover and rank the top 20 traders across 24h, 7d, and 30d leaderboards:

```bash
python run.py --discover --top 20
```

This generates `wallets.json`:
```json
[
  {
    "address": "0x0a6ebed0155edb4b21d92ad02897a626cd90119e",
    "label": "unipcs",
    "score": 100.0,
    "pnl_usd": 15499382.0,
    "volume_usd": 2913240.0,
    "trades": 2750
  }
]
```

### 3. Run Paper Trading (Zero Risk)

Start the bot in paper simulation mode:

```bash
python run.py --paper
```
Or run with autonomous background discovery (periodically refreshes `wallets.json` every 2 hours):

```bash
python run.py --auto --paper
```

### 4. Interactive Terminal Dashboard

In a separate terminal window:

```bash
python run.py --dash
```
- `Up` / `Down`: Navigate open positions
- `o`: Open token on DexScreener
- `f`: Open trader's FOMO profile
- `s`: Manually trigger sell
- `q`: Quit

---

## Live Trading Setup

When ready to trade with real funds:

1. **Configure Environment**
   ```bash
   cp .env.example .env
   # Edit .env and supply PRIVATE_KEY (a dedicated fresh hot wallet with USDG + a little ETH for gas)
   ```

2. **Deploy Router Contract (Requires [Foundry](https://getfoundry.sh))**
   ```bash
   ./deploy.sh
   ```
   Copy the printed `Deployed to:` address into `config.json` under `"router"`.

3. **Enable Live Mode**
   In `config.json`:
   ```json
   "live": true
   ```
   Then launch:
   ```bash
   python run.py --auto --live
   ```

---

## Configuration Reference (`config.json`)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `live` | bool | `false` | `false` for paper trading, `true` for live execution |
| `buy_usd` | number | `100` | Amount in USDG to buy per copy signal |
| `max_positions` | int | `20` | Max simultaneous open positions |
| `min_liquidity_usd` | number | `10000` | Minimum pool liquidity required to buy |
| `max_price_impact_pct` | number | `10` | Maximum acceptable price impact |
| `slippage_pct` | number | `3` | Maximum buy slippage tolerance |
| `exits` | array | `[{"after_minutes": 5, "pct": 75}, ...]` | Staged exit timing and percentages |
| `discovery.enabled` | bool | `true` | Enable periodic smart money discovery |
| `discovery.interval_hours`| number | `2.0` | Hours between discovery refreshes |
| `discovery.top_n` | int | `50` | Number of top traders to monitor in `wallets.json` |
| `discovery.min_trades` | int | `5` | Minimum trader trade count filter |
| `discovery.min_volume_usd` | number | `2000` | Minimum trader volume filter |

---

## Running Tests

```bash
PYTHONPATH=. pytest tests/ -v
```

---

## Architecture Overview

```
[ FOMO Leaderboards (24h/7d/30d) ]
                 │
                 ▼
     [ auto_discovery.py ]  ──> [ wallets.json ] (Atomic file write + backups)
                                      │
                                      │ (Hot-reload check every 5s)
                                      ▼
[ Robinhood Chain ] ── eth_getLogs ──> [ bot.py ]
                                         ├── Risk Guard (Liquidity / Price Impact / Honeypot)
                                         ├── Route Discovery (Uniswap V3 / V4 USDG legs)
                                         ├── Execution (Paper quotes / Live CopyRouter)
                                         └── Exit Manager (Staged timers + Origin sell trigger)
                                                  │
                                                  ├──> [ dash.py ] (Terminal UI)
                                                  └──> [ stats.py ] (Performance analytics)
```

---

## Acknowledgements

Built upon concepts and on-chain routing from [QorbQuant/fomo-robinhood-copy](https://github.com/QorbQuant/fomo-robinhood-copy).
