#!/usr/bin/env python3
"""FOMO Auto-Discovery & Trader Scoring Engine.

Automatically discovers high-performing traders from FOMO leaderboards,
evaluates risk and consistency metrics, scores them, and dynamically updates
wallets.json for the copy trading bot.

Usage:
    python auto_discovery.py                       # Run once with default config
    python auto_discovery.py --top 50              # Select top 50 traders
    python auto_discovery.py --windows 24h,7d,30d  # Evaluate across multi-windows
    python auto_discovery.py --daemon              # Run as daemon, refresh periodically
"""

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG_PATH = ROOT / "config.json"

DEFAULT_API_BASE = "https://api.fomoapi.io"


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] [Discovery] {msg}", flush=True)


def load_config() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception as e:
            log(f"Warning: Failed to parse config.json: {e}")
    return {}


class TraderDiscovery:
    def __init__(
        self,
        api_base: str = DEFAULT_API_BASE,
        api_key: Optional[str] = None,
        min_trades: int = 5,
        min_volume_usd: float = 2000.0,
        min_pnl_usd: float = 100.0,
        timeout: int = 15,
    ):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key or os.environ.get("FOMO_API_KEY")
        self.min_trades = min_trades
        self.min_volume_usd = min_volume_usd
        self.min_pnl_usd = min_pnl_usd
        self.timeout = timeout

    def _http_get(self, url: str) -> Optional[Dict[str, Any]]:
        headers = {
            "User-Agent": "fomo-copy-bot/auto-discovery/1.0",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            log(f"HTTP Error {e.code} for {url}")
        except urllib.error.URLError as e:
            log(f"Network error connecting to {url}: {e}")
        except Exception as e:
            log(f"Unexpected error fetching {url}: {e}")
        return None

    def fetch_leaderboard(self, window: str = "7d", limit: int = 50) -> List[Dict[str, Any]]:
        url = f"{self.api_base}/v2/leaderboard/{window}?limit={limit}"
        data = self._http_get(url)
        if not data:
            return []
        return data.get("traders", [])

    def calculate_score(self, trader: Dict[str, Any]) -> float:
        """Calculate a composite quality score (0 - 100) for a trader.

        Factors:
        - PnL (higher is better, logarithmically scaled)
        - Volume adequacy (proves liquidity)
        - Trades count (sweet spot between 10 and 3,000; excessive trades = bot)
        - Followers (social validation)
        - Window presence bonus
        """
        pnl = max(0.0, float(trader.get("pnlUsd", 0)))
        vol = max(0.0, float(trader.get("volumeUsd", 0)))
        trades = max(0, int(trader.get("trades", 0)))
        followers = max(0, int(trader.get("followers", 0)))

        # 1. PnL Score (0 - 40 pts)
        if pnl <= 0:
            pnl_score = 0.0
        elif pnl < 10000:
            pnl_score = (pnl / 10000.0) * 15.0
        elif pnl < 100000:
            pnl_score = 15.0 + ((pnl - 10000) / 90000.0) * 15.0
        else:
            pnl_score = 30.0 + min(10.0, (pnl - 100000) / 900000.0 * 10.0)

        # 2. Volume Score (0 - 25 pts)
        if vol < self.min_volume_usd:
            vol_score = 0.0
        elif vol < 50000:
            vol_score = (vol / 50000.0) * 15.0
        else:
            vol_score = 15.0 + min(10.0, (vol - 50000) / 450000.0 * 10.0)

        # 3. Trades Activity Score (0 - 20 pts)
        if trades < self.min_trades:
            trade_score = 0.0
        elif trades <= 500:
            trade_score = min(20.0, trades / 25.0)
        elif trades <= 3000:
            trade_score = 20.0
        else:
            # Over 3000 trades might be an ultra-high frequency bot or MEV
            trade_score = max(5.0, 20.0 - ((trades - 3000) / 5000.0) * 10.0)

        # 4. Social & Verification Score (0 - 15 pts)
        social_score = min(10.0, followers / 1000.0)
        if trader.get("verified", False):
            social_score += 5.0

        total_score = pnl_score + vol_score + trade_score + social_score
        return round(min(100.0, total_score), 2)

    def is_eligible(self, trader: Dict[str, Any]) -> bool:
        """Filter out ineligible or low-quality traders."""
        wallets = trader.get("wallets") or {}
        evm_addr = trader.get("address") or wallets.get("evm")
        if not evm_addr or not isinstance(evm_addr, str):
            return False
        evm_clean = evm_addr.strip().lower()
        if not evm_clean.startswith("0x") or len(evm_clean) != 42:
            return False

        pnl = float(trader.get("pnlUsd", 0))
        vol = float(trader.get("volumeUsd", 0))
        trades = int(trader.get("trades", 0))

        if pnl < self.min_pnl_usd:
            return False
        if vol < self.min_volume_usd:
            return False
        if trades < self.min_trades:
            return False

        return True

    def discover(
        self,
        windows: List[str] = ["24h", "7d", "30d"],
        top_n: int = 50,
    ) -> List[Dict[str, Any]]:
        """Query multiple leaderboard windows, aggregate, score, and rank traders."""
        log(f"Starting discovery across windows: {windows}...")
        trader_map: Dict[str, Dict[str, Any]] = {}

        for w in windows:
            items = self.fetch_leaderboard(window=w, limit=50)
            log(f"  Fetched {len(items)} traders from {w} leaderboard.")
            for item in items:
                wallets = item.get("wallets") or {}
                evm = wallets.get("evm")
                if not evm:
                    continue
                evm_lower = evm.strip().lower()
                if evm_lower not in trader_map:
                    trader_map[evm_lower] = {
                        "address": evm_lower,
                        "label": item.get("handle") or evm_lower[:10],
                        "displayName": item.get("displayName"),
                        "solana_wallet": wallets.get("solana"),
                        "followers": item.get("followers", 0),
                        "trades": item.get("trades", 0),
                        "volumeUsd": float(item.get("volumeUsd", 0)),
                        "pnlUsd": float(item.get("pnlUsd", 0)),
                        "windows_present": [w],
                        "holdings_count": item.get("holdings", 0),
                        "verified": item.get("verified", False),
                    }
                else:
                    trader_map[evm_lower]["windows_present"].append(w)
                    # Use the maximum PnL and volume across windows
                    trader_map[evm_lower]["pnlUsd"] = max(
                        trader_map[evm_lower]["pnlUsd"], float(item.get("pnlUsd", 0))
                    )
                    trader_map[evm_lower]["volumeUsd"] = max(
                        trader_map[evm_lower]["volumeUsd"], float(item.get("volumeUsd", 0))
                    )

        # Filter & score
        eligible_traders = []
        for addr, t in trader_map.items():
            if self.is_eligible(t):
                # Extra bonus for traders present in multiple windows (consistency)
                bonus = len(t["windows_present"]) * 3.0
                score = min(100.0, self.calculate_score(t) + bonus)
                t["score"] = round(score, 2)
                eligible_traders.append(t)

        # Sort descending by score
        eligible_traders.sort(key=lambda x: x["score"], reverse=True)
        selected = eligible_traders[:top_n]

        log(f"Discovered {len(trader_map)} unique traders, {len(eligible_traders)} passed filters, selected top {len(selected)}.")
        return selected

    def export_to_wallets_json(
        self,
        traders: List[Dict[str, Any]],
        output_path: Path = ROOT / "wallets.json",
        preserve_existing_custom: bool = True,
    ) -> None:
        """Atomically export the scored traders to wallets.json."""
        existing_custom = {}
        if preserve_existing_custom and output_path.exists():
            try:
                raw = json.loads(output_path.read_text())
                for entry in raw:
                    if isinstance(entry, dict) and entry.get("custom"):
                        existing_custom[entry["address"].lower()] = entry
            except Exception:
                pass

        export_list = []
        for c in existing_custom.values():
            export_list.append(c)

        seen_addrs = set(existing_custom.keys())
        for t in traders:
            addr = t["address"].lower()
            if addr in seen_addrs:
                continue
            seen_addrs.add(addr)
            export_list.append({
                "address": addr,
                "label": t["label"],
                "score": t["score"],
                "pnl_usd": t["pnlUsd"],
                "volume_usd": t["volumeUsd"],
                "trades": t["trades"],
                "windows": t.get("windows_present", []),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        DATA.mkdir(parents=True, exist_ok=True)
        backup_dir = DATA / "wallets_history"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"wallets_{int(time.time())}.json"

        if output_path.exists():
            try:
                shutil.copy2(output_path, backup_path)
            except Exception as e:
                log(f"Failed to backup old wallets.json: {e}")

        tmp_path = output_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(export_list, indent=2))
        tmp_path.replace(output_path)

        log(f"Successfully wrote {len(export_list)} wallets to {output_path.name} (backup saved).")


def print_traders_table(traders: List[Dict[str, Any]]) -> None:
    """Print formatted summary table to console."""
    if not traders:
        print("No eligible traders found.")
        return

    print("\n" + "=" * 90)
    print(f"{'Rank':<5} {'Score':<7} {'Handle':<18} {'EVM Address':<42} {'PnL ($)':<12}")
    print("-" * 90)
    for idx, t in enumerate(traders, 1):
        handle = t["label"][:16]
        addr = t["address"]
        score = f"{t['score']:.1f}"
        pnl = f"${t['pnlUsd']:,.0f}"
        print(f"{idx:<5} {score:<7} {handle:<18} {addr:<42} {pnl:<12}")
    print("=" * 90 + "\n")


def run_daemon(interval_hours: float, top_n: int, windows: List[str]):
    log(f"Running in DAEMON mode (refreshing every {interval_hours} hours)...")
    discovery = TraderDiscovery()
    while True:
        try:
            traders = discovery.discover(windows=windows, top_n=top_n)
            discovery.export_to_wallets_json(traders)
            print_traders_table(traders[:10])
        except Exception as e:
            log(f"Daemon error during discovery: {e}")

        sleep_secs = int(interval_hours * 3600)
        log(f"Sleeping for {interval_hours} hours ({sleep_secs}s)...")
        time.sleep(sleep_secs)


def main():
    parser = argparse.ArgumentParser(description="FOMO Auto-Discovery & Trader Scoring Engine")
    parser.add_argument("--top", type=int, default=50, help="Number of top traders to select (default: 50)")
    parser.add_argument("--windows", type=str, default="24h,7d,30d", help="Comma-separated windows (default: 24h,7d,30d)")
    parser.add_argument("--output", type=str, default="wallets.json", help="Output file path (default: wallets.json)")
    parser.add_argument("--daemon", action="store_true", help="Run periodically in daemon mode")
    parser.add_argument("--interval-hours", type=float, default=2.0, help="Interval in hours for daemon mode (default: 2.0)")
    args = parser.parse_args()

    cfg = load_config()
    discovery_cfg = cfg.get("discovery", {})

    top_n = args.top or discovery_cfg.get("top_n", 50)
    windows_list = [w.strip() for w in args.windows.split(",") if w.strip()]
    output_path = ROOT / args.output

    if args.daemon:
        run_daemon(interval_hours=args.interval_hours, top_n=top_n, windows=windows_list)
        return

    discovery = TraderDiscovery(
        min_trades=discovery_cfg.get("min_trades", 5),
        min_volume_usd=discovery_cfg.get("min_volume_usd", 2000.0),
        min_pnl_usd=discovery_cfg.get("min_pnl_usd", 100.0),
    )

    traders = discovery.discover(windows=windows_list, top_n=top_n)
    discovery.export_to_wallets_json(traders, output_path=output_path)
    print_traders_table(traders)


if __name__ == "__main__":
    main()
