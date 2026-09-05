#!/usr/bin/env python3
"""Unified CLI Runner for FOMO Auto-Discovery & Copy Trading Bot.

Usage:
    python run.py                  # Auto-discover top traders, then start copy bot
    python run.py --discover       # Run auto-discovery only (view leaderboard)
    python run.py --auto           # Run bot with periodic background discovery
    python run.py --paper          # Force paper trading mode
    python run.py --status         # View open/closed positions and PnL
    python run.py --dash           # Launch full-screen terminal dashboard
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] [Runner] {msg}", flush=True)


def background_discovery_worker(interval_hours: float, top_n: int, windows: list, sources: list = None, min_win_rate: float = 0.45):
    from auto_discovery import TraderDiscovery
    discovery = TraderDiscovery(trenches_min_win_rate=min_win_rate)
    while True:
        try:
            time.sleep(interval_hours * 3600)
            log(f"Periodic discovery triggered (every {interval_hours}h)...")
            traders = discovery.discover(windows=windows, top_n=top_n, sources=sources)
            discovery.export_to_wallets_json(traders)
            log(f"Discovery refreshed {len(traders)} traders. bot.py will hot-reload automatically.")
        except Exception as e:
            log(f"Background discovery error: {e}")


def main():
    parser = argparse.ArgumentParser(description="FOMO Auto-Discovery & Copy Trading System")
    parser.add_argument("--discover", action="store_true", help="Run trader discovery and exit")
    parser.add_argument("--top", type=int, default=None, help="Top N traders to follow (default from config or 50)")
    parser.add_argument("--sources", type=str, default=None, help="Discovery sources (e.g. fomo,trenches)")
    parser.add_argument("--auto", action="store_true", help="Run bot with periodic background auto-discovery")
    parser.add_argument("--paper", action="store_true", help="Force paper mode (simulated fills)")
    parser.add_argument("--live", action="store_true", help="Force live execution mode")
    parser.add_argument("--status", action="store_true", help="Show current bot status and positions")
    parser.add_argument("--dash", action="store_true", help="Launch terminal dashboard")
    args = parser.parse_args()

    cfg = {}
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())

    discovery_cfg = cfg.get("discovery", {})
    top_n = args.top or discovery_cfg.get("top_n", 50)
    windows = discovery_cfg.get("windows", ["24h", "7d", "30d"])
    sources = [s.strip() for s in args.sources.split(",")] if args.sources else discovery_cfg.get("sources", ["fomo", "trenches"])
    interval_hours = discovery_cfg.get("interval_hours", 2.0)
    min_win_rate = discovery_cfg.get("trenches_min_win_rate", 0.45)

    # 1. Quick commands
    if args.status:
        subprocess.run([sys.executable, str(ROOT / "bot.py"), "status"])
        return

    if args.dash:
        subprocess.run([sys.executable, str(ROOT / "dash.py")])
        return

    if args.discover:
        from auto_discovery import TraderDiscovery, print_traders_table
        discovery = TraderDiscovery(
            min_trades=discovery_cfg.get("min_trades", 5),
            min_volume_usd=discovery_cfg.get("min_volume_usd", 2000.0),
            min_pnl_usd=discovery_cfg.get("min_pnl_usd", 100.0),
            trenches_min_win_rate=min_win_rate,
        )
        traders = discovery.discover(windows=windows, top_n=top_n, sources=sources)
        discovery.export_to_wallets_json(traders)
        print_traders_table(traders)
        return

    # 2. Pre-run discovery if wallets.json doesn't exist or is empty
    wallets_file = ROOT / "wallets.json"
    needs_discovery = not wallets_file.exists()
    if not needs_discovery:
        try:
            w_data = json.loads(wallets_file.read_text())
            if not w_data:
                needs_discovery = True
        except Exception:
            needs_discovery = True

    if needs_discovery:
        log("No valid wallets.json found. Running initial discovery...")
        from auto_discovery import TraderDiscovery, print_traders_table
        discovery = TraderDiscovery(
            min_trades=discovery_cfg.get("min_trades", 5),
            min_volume_usd=discovery_cfg.get("min_volume_usd", 2000.0),
            min_pnl_usd=discovery_cfg.get("min_pnl_usd", 100.0),
            trenches_min_win_rate=min_win_rate,
        )
        traders = discovery.discover(windows=windows, top_n=top_n, sources=sources)
        discovery.export_to_wallets_json(traders)
        print_traders_table(traders[:10])

    # 3. Mode override
    if args.paper:
        cfg["live"] = False
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        log("Config set to PAPER mode (live: false).")
    elif args.live:
        cfg["live"] = True
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        log("Config set to LIVE mode (live: true).")

    # 4. If auto background discovery requested
    if args.auto:
        log(f"Spawning background auto-discovery worker (every {interval_hours}h)...")
        t = threading.Thread(
            target=background_discovery_worker,
            args=(interval_hours, top_n, windows, sources, min_win_rate),
            daemon=True,
        )
        t.start()

    # 5. Launch bot
    log("Starting copybot main loop...")
    import bot
    bot.cmd_run()


if __name__ == "__main__":
    main()
