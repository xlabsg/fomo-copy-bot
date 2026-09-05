# FOMO Auto-Discovery & Copy Trading Bot

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Network: Robinhood Chain](https://img.shields.io/badge/Network-Robinhood%20Chain%20(4663)-green.svg)](https://chain.robinhood.com)

An autonomous copy-trading and smart-money tracking system on **Robinhood Chain (EVM)** that **automatically discovers top-performing traders from [FOMO](https://fomo.family)**, dynamically scores and filters them, and mirrors their new token entries with automated risk management and staged exits.

---

## Highlights

1. **Automated Smart Money Discovery (`auto_discovery.py`)**
   - Continuously monitors multi-window leaderboards (`24h`, `7d`, `30d`) on FOMO.
   - Resolves handles to verified on-chain EVM wallet addresses.
   - Quantitative quality scoring based on PnL, volume proof, trade frequency, consistency, and social signals.
   - Anti-Rug & Anti-Bot filters (weeds out dev wallets, micro-scalpers/MEV, zero-liquidity tokens).
   - Atomically updates `wallets.json` with historical backups.

2. **High-Performance On-Chain Copy Engine (`bot.py`)**
   - Single batch `eth_getLogs` Transfer polling: 100 wallets incur the same RPC load as 1 wallet.
   - **Hot-Reloading**: Automatically detects disk updates to `wallets.json` and refreshes active target wallets without restarting the process or disrupting existing positions.
   - Uniswap V3 & V4 routing on Robinhood Chain (`discover_route`), gas optimization, and pre-approvals.
   - Real-time risk guards: `min_liquidity_usd`, `max_price_impact_pct`, max signal age timeout, round-trip loss floor, and honeypot validation.

3. **Dual-Loop Exit Management**
   - **Staged Time Exits**: e.g., sell 75% after 5 minutes, sell remaining 25% after 10 minutes.
   - **Origin Trader Tracking**: Liquidates the position immediately if the tracked smart money exits early.

4. **Zero-Risk Paper Trading & Live Execution**
   - **Paper Mode** (`"live": false`): Fully simulated fills at real on-chain quotes without risking capital.
   - **Live Mode** (`"live": true`): Real on-chain execution via `CopyRouter` smart contract.

5. **Terminal Dashboard & Analytics**
   - `dash.py`: Full-screen curses interactive terminal UI (view open positions, signals, manual exits).
   - `stats.py`: Post-trade analytics, latency analysis, and pool-level swap replay.

---

## Architecture Overview

```text
[ FOMO Leaderboards (24h/7d/30d) ]
                 │
                 ▼
     [ auto_discovery.py ]  ──> [ wallets.json ] (Atomic write + backups)
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

## Quick Start

### 1. Installation

```bash
git clone https://github.com/xlabsg/fomo-copy-bot.git
cd fomo-copy-bot

# Recommended: using uv
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt

# Or using standard python venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Auto-Discover Top FOMO Traders

Discover and rank the top 50 traders across 24h, 7d, and 30d leaderboards:

```bash
python run.py --discover --top 50
```

This generates `wallets.json`:
```json
[
  {
    "address": "0x0a6ebed0155edb4b21d92ad02897a626cd90119e",
    "label": "unipcs",
    "score": 100.0,
    "pnl_usd": 15355859.0,
    "volume_usd": 2913240.0,
    "trades": 2750
  }
]
```

### 3. Run Paper Trading (Zero Capital Risk)

Start the bot in paper simulation mode:

```bash
python run.py --paper
```

Or run with periodic background auto-discovery (periodically refreshes `wallets.json` every 2 hours):

```bash
python run.py --auto --paper
```

### 4. Interactive Terminal Dashboard

In a separate terminal window:

```bash
python run.py --dash
```

**Hotkeys:**
- `Up` / `Down`: Navigate open positions
- `o`: Open token on DexScreener
- `f`: Open trader's FOMO profile
- `s`: Manually trigger sell
- `q`: Quit

Or print a quick snapshot and exit:
```bash
python dash.py --once
```

---

## Configuration Reference (`config.json`)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `live` | bool | `false` | `false` for simulated paper trading, `true` for live execution |
| `buy_usd` | number | `100` | USDG amount to buy per copy signal |
| `paper_cash_usd` | number | `100000` | Simulated bankroll for paper trading |
| `max_positions` | int | `20` | Maximum simultaneous open positions |
| `poll_seconds` | number | `2.0` | Polling interval for RPC log queries |
| `heartbeat_seconds` | number | `30` | Interval to print health and block lag heartbeat |
| `min_liquidity_usd` | number | `10000` | Minimum pool liquidity required to buy |
| `min_origin_usd` | number | `50` | Minimum purchase size by the tracked wallet to trigger copy |
| `max_price_impact_pct` | number | `10` | Maximum acceptable price impact |
| `slippage_pct` | number | `3` | Maximum buy slippage tolerance |
| `exits` | array | `[{"after_minutes": 5, "pct": 75}, ...]` | Staged exit timing and percentages |
| `discovery.enabled` | bool | `true` | Enable periodic smart money discovery |
| `discovery.interval_hours`| number | `2.0` | Hours between discovery refreshes |
| `discovery.top_n` | int | `50` | Number of top traders to monitor in `wallets.json` |
| `discovery.min_trades` | int | `5` | Minimum trader trade count filter |
| `discovery.min_volume_usd` | number | `2000` | Minimum trader volume filter |

---

## Live Trading Setup

When you are ready to trade on-chain with real funds:

1. **Security & Wallet Isolation**
   - Always use a **dedicated fresh hot wallet** containing only the funds you intend to trade (USDG + a small amount of ETH for gas).
   - Never use your personal or high-value wallets.

2. **Configure Environment**
   ```bash
   cp .env.example .env
   # Edit .env and supply your PRIVATE_KEY (and optional custom RPC_URL)
   ```

3. **Deploy Router Contract (Requires [Foundry](https://getfoundry.sh))**
   ```bash
   ./deploy.sh
   ```
   Copy the printed `Deployed to:` address into `config.json` under `"router"`.

4. **Launch Live Execution**
   ```bash
   python run.py --auto --live
   ```

---

## CLI Command Summary

| Command | Description |
|---------|-------------|
| `python run.py --discover` | Fetch FOMO leaderboard and generate `wallets.json` |
| `python run.py --paper` | Run copybot in paper simulation mode |
| `python run.py --auto --paper` | Run paper copybot with background trader discovery |
| `python run.py --live` | Run copybot in live execution mode |
| `python run.py --status` | Display open/closed positions and realized/unrealized PnL |
| `python run.py --dash` | Launch full-screen interactive curses dashboard |
| `python bot.py route <token>` | Dry-run route discovery and pool quote for a specific token |
| `python stats.py` | Display analytics, price paths, and execution latency report |

---

## Running Unit Tests

```bash
PYTHONPATH=. pytest tests/ -v
```

---

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.
