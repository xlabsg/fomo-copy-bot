#!/usr/bin/env python3
"""RobinhoodTrenches Independent Flow & Tape Monitor.

Monitors smart money consensus ("Who Followed Who"), fresh pools, and the live
fill tape from robinhoodtrenches.com independently from the main bot.

Usage:
    python trenches_monitor.py            # Run once: show consensus flow & fresh pools
    python trenches_monitor.py --watch    # Continuous monitor loop (every 15s)
    python trenches_monitor.py --tape     # Show recent live tape fills
    python trenches_monitor.py --radar    # Show fresh pools radar only
"""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
FLOW_SIGNALS_FILE = DATA_DIR / "trenches_flow.jsonl"
BASE_URL = "https://robinhoodtrenches.com/api"


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] [Trenches-Monitor] {msg}", flush=True)


def http_get_json(url: str, timeout: int = 10) -> Optional[Any]:
    headers = {
        "User-Agent": "fomo-copy-bot/trenches-monitor/1.0",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"Error fetching {url}: {e}")
        return None


def fetch_flow() -> List[Dict[str, Any]]:
    data = http_get_json(f"{BASE_URL}/flow")
    return data if isinstance(data, list) else []


def fetch_radar() -> List[Dict[str, Any]]:
    data = http_get_json(f"{BASE_URL}/radar")
    return data if isinstance(data, list) else []


def fetch_tape(limit: int = 30) -> List[Dict[str, Any]]:
    data = http_get_json(f"{BASE_URL}/tape?limit={limit}&stocks=false")
    return data if isinstance(data, list) else []


def print_flow_table(flows: List[Dict[str, Any]]):
    if not flows:
        print("\nNo consensus flow records found.")
        return

    print("\n" + "=" * 115)
    print(f"🔥 WHO FOLLOWED WHO (Smart Money Consensus Flow)")
    print("=" * 115)
    print(f"{'#':<3} {'Symbol':<12} {'Lead Trader':<16} {'Followers':<10} {'Total USD':<14} {'Token Address':<42}")
    print("-" * 115)
    for idx, f in enumerate(flows[:15], 1):
        sym = f.get("symbol", "?")[:10]
        lead = f.get("lead", {})
        lead_h = lead.get("handle", "?")[:14]
        n_fol = f.get("follower_count", len(f.get("followers", [])))
        usd_tot = f.get("total_usd") or 0.0
        addr = f.get("token", "")
        print(f"{idx:<3} {sym:<12} {lead_h:<16} {n_fol:<10} ${usd_tot:>10,.0f}  {addr:<42}")
    print("=" * 115 + "\n")


def print_radar_table(radar: List[Dict[str, Any]]):
    if not radar:
        print("\nNo fresh pool records found.")
        return

    print("\n" + "=" * 115)
    print(f"📡 FRESH POOLS RADAR (Robinhood Chain New Pairs)")
    print("=" * 115)
    print(f"{'#':<3} {'Symbol':<12} {'First Buyer':<16} {'Liquidity ($)':<15} {'Pool Age':<12} {'Token Address':<42}")
    print("-" * 115)
    for idx, r in enumerate(radar[:10], 1):
        sym = r.get("symbol", "?")[:10]
        fb = r.get("first_buyer") or {}
        buyer = fb.get("handle", "?")[:14] if isinstance(fb, dict) else str(fb)[:14]
        liq = r.get("liquidity")
        liq_str = f"${float(liq):>10,.0f}" if liq is not None else "—"
        age = r.get("pool_age")
        if age is not None:
            age_str = f"{age//60}m" if age < 3600 else f"{age/3600:.1f}h"
        else:
            age_str = "—"
        addr = r.get("token", "")
        print(f"{idx:<3} {sym:<12} {buyer:<16} {liq_str:<15} {age_str:<12} {addr:<42}")
    print("=" * 115 + "\n")


def print_tape_table(tape: List[Dict[str, Any]]):
    if not tape:
        print("\nNo tape records found.")
        return

    print("\n" + "=" * 115)
    print(f"📟 LIVE TAPE (Top FOMO Traders Real-Time Fills)")
    print("=" * 115)
    print(f"{'Time':<10} {'Side':<6} {'Symbol':<12} {'USD':<12} {'Trader':<16} {'FirstBuy':<10} {'Tx':<20}")
    print("-" * 115)
    for t in tape[:20]:
        ts_val = t.get("ts", 0)
        tm = time.strftime("%H:%M:%S", time.localtime(ts_val))
        side = t.get("side", "").upper()
        sym = t.get("symbol", "?")[:10]
        usd = t.get("usd", 0) or 0
        trader = t.get("handle", "?")[:14]
        first = "YES" if t.get("new_position") else "—"
        tx_short = (t.get("tx", "")[:10] + "...") if t.get("tx") else "—"
        print(f"{tm:<10} {side:<6} {sym:<12} ${usd:>8,.0f}  {trader:<16} {first:<10} {tx_short:<20}")
    print("=" * 115 + "\n")


def record_flow(flows: List[Dict[str, Any]]):
    if not flows:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now_ts = time.time()
    with open(FLOW_SIGNALS_FILE, "a", encoding="utf-8") as f:
        for fl in flows:
            record = {
                "ts": now_ts,
                "datetime": datetime.now(timezone.utc).isoformat(),
                "token": fl.get("token"),
                "symbol": fl.get("symbol"),
                "lead_trader": fl.get("lead", {}).get("handle"),
                "follower_count": fl.get("follower_count", len(fl.get("followers", []))),
                "total_usd": fl.get("total_usd"),
            }
            f.write(json.dumps(record) + "\n")


def main():
    parser = argparse.ArgumentParser(description="RobinhoodTrenches Flow & Tape Monitor")
    parser.add_argument("--watch", action="store_true", help="Run continuous monitoring loop")
    parser.add_argument("--interval", type=int, default=15, help="Interval in seconds for watch mode (default: 15)")
    parser.add_argument("--tape", action="store_true", help="Show live fill tape")
    parser.add_argument("--radar", action="store_true", help="Show fresh pools radar")
    args = parser.parse_args()

    if args.tape:
        tape = fetch_tape()
        print_tape_table(tape)
        return

    if args.radar:
        radar = fetch_radar()
        print_radar_table(radar)
        return

    if args.watch:
        log(f"Starting continuous Trenches monitor (every {args.interval}s)...")
        while True:
            try:
                flows = fetch_flow()
                if flows:
                    record_flow(flows)
                    print_flow_table(flows)
            except Exception as e:
                log(f"Error in monitor loop: {e}")
            time.sleep(args.interval)
    else:
        flows = fetch_flow()
        radar = fetch_radar()
        print_flow_table(flows)
        print_radar_table(radar)


if __name__ == "__main__":
    main()
