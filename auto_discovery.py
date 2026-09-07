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
DEFAULT_TRENCHES_API_BASE = "https://robinhoodtrenches.com/api"


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
        trenches_api_base: str = DEFAULT_TRENCHES_API_BASE,
        min_trades: int = 5,
        max_trades: int = 600,
        min_volume_usd: float = 2000.0,
        min_pnl_usd: float = 100.0,
        min_payoff_ratio: float = 0.8,
        trenches_min_win_rate: float = 0.40,
        timeout: int = 15,
    ):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key or os.environ.get("FOMO_API_KEY")
        self.trenches_api_base = trenches_api_base.rstrip("/")
        self.min_trades = min_trades
        self.max_trades = max_trades
        self.min_volume_usd = min_volume_usd
        self.min_pnl_usd = min_pnl_usd
        self.min_payoff_ratio = min_payoff_ratio
        self.trenches_min_win_rate = trenches_min_win_rate
        self.timeout = timeout

    def _http_get(self, url: str) -> Optional[Any]:
        headers = {
            "User-Agent": "fomo-copy-bot/auto-discovery/1.0",
            "Accept": "application/json",
        }
        if self.api_key and "fomoapi.io" in url:
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
        if not data or not isinstance(data, dict):
            return []
        return data.get("traders", [])

    def fetch_trenches_traders(self, window: str = "7d") -> List[Dict[str, Any]]:
        url = f"{self.trenches_api_base}/traders?window={window}&stocks=false"
        data = self._http_get(url)
        if not data or not isinstance(data, list):
            return []
        return data

    def calculate_score(self, trader: Dict[str, Any]) -> float:
        """Calculate a composite quality score (0 - 100) for a trader.

        Factors:
        - PnL (higher is better, logarithmically scaled)
        - Volume adequacy (proves liquidity)
        - Trades count (sweet spot between 10 and 3,000; excessive trades = bot)
        - Followers (social validation)
        - On-chain Win Rate & Realized PnL modifiers (from robinhoodtrenches)
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
        elif trades <= 250:
            trade_score = min(20.0, trades / 12.5)
        elif trades <= 400:
            trade_score = 20.0
        else:
            # Penalize high-frequency bots/market makers
            trade_score = max(0.0, 20.0 - ((trades - 400) / 200.0) * 15.0)

        # 4. Social & Verification Score (0 - 15 pts)
        social_score = min(10.0, followers / 1000.0)
        if trader.get("verified", False):
            social_score += 5.0

        total_score = pnl_score + vol_score + trade_score + social_score

        # 5. On-Chain Win Rate & Payoff Ratio Modifiers (from Trenches)
        win_rate = trader.get("win_rate")
        if win_rate is not None:
            if win_rate >= 0.60:
                total_score += 15.0
            elif win_rate >= 0.50:
                total_score += 10.0
            elif win_rate < 0.35:
                total_score -= 15.0

        payoff_ratio = trader.get("payoff_ratio")
        if payoff_ratio is not None:
            if payoff_ratio >= 3.0:
                total_score += 15.0
            elif payoff_ratio >= 1.5:
                total_score += 10.0
            elif payoff_ratio >= 1.0:
                total_score += 5.0
            elif payoff_ratio < 0.8:
                total_score -= 20.0

        realized_pnl = trader.get("realized_pnl")
        if realized_pnl is not None:
            if realized_pnl < 0:
                total_score -= 25.0
            elif realized_pnl > 50000:
                total_score += 10.0

        if len(trader.get("sources", [])) > 1:
            total_score += 5.0

        return round(max(0.0, min(100.0, total_score)), 2)

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
        if self.max_trades and trades > self.max_trades:
            return False

        # On-chain sanity filters if available from trenches
        realized_pnl = trader.get("realized_pnl")
        if realized_pnl is not None and realized_pnl < 0:
            return False

        closed_trades = trader.get("closed_trades", 0)
        payoff_ratio = trader.get("payoff_ratio")
        win_rate = trader.get("win_rate")

        if closed_trades >= 5:
            # 1. Payoff Ratio Filter: discard traders whose worst loss vastly exceeds best win
            if payoff_ratio is not None and payoff_ratio < self.min_payoff_ratio:
                return False

            # 2. Dynamic Win Rate requirement linked to Payoff Ratio:
            # If payoff ratio is extraordinary (>= 2.0), allow lower win rate (>= 30%).
            # Otherwise require trenches_min_win_rate (>= 40%).
            min_wr = 0.30 if (payoff_ratio is not None and payoff_ratio >= 2.0) else self.trenches_min_win_rate
            if win_rate is not None and win_rate < min_wr:
                return False

        return True

    def discover(
        self,
        windows: List[str] = ["24h", "7d", "30d"],
        top_n: int = 50,
        sources: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Query multiple leaderboard windows and sources, aggregate, score, and rank traders."""
        cfg = load_config()
        if sources is None:
            sources = cfg.get("discovery", {}).get("sources", ["fomo", "trenches"])

        log(f"Starting discovery across sources: {sources}, windows: {windows}...")
        trader_map: Dict[str, Dict[str, Any]] = {}

        # 1. Fetch from FOMO if enabled
        if "fomo" in sources:
            for w in windows:
                items = self.fetch_leaderboard(window=w, limit=50)
                log(f"  Fetched {len(items)} traders from FOMO {w} leaderboard.")
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
                            "sources": ["fomo"],
                        }
                    else:
                        trader_map[evm_lower]["windows_present"].append(w)
                        trader_map[evm_lower]["pnlUsd"] = max(
                            trader_map[evm_lower]["pnlUsd"], float(item.get("pnlUsd", 0))
                        )
                        trader_map[evm_lower]["volumeUsd"] = max(
                            trader_map[evm_lower]["volumeUsd"], float(item.get("volumeUsd", 0))
                        )
                        if "fomo" not in trader_map[evm_lower]["sources"]:
                            trader_map[evm_lower]["sources"].append("fomo")

        # 2. Fetch from robinhoodtrenches if enabled
        if "trenches" in sources:
            for w in windows:
                items = self.fetch_trenches_traders(window=w)
                log(f"  Fetched {len(items)} traders from Trenches {w} leaderboard.")
                for item in items:
                    evm = item.get("address")
                    if not evm or not isinstance(evm, str):
                        continue
                    evm_lower = evm.strip().lower()
                    if not evm_lower.startswith("0x") or len(evm_lower) != 42:
                        continue
                    realized = float(item.get("realized_pnl") or 0)
                    vol = float(item.get("volume") or 0)
                    wr = item.get("win_rate")
                    win_rate = float(wr) if wr is not None else None
                    closed = int(item.get("closed_trades") or 0)
                    fills = int(item.get("fills") or 0)
                    followers = int(item.get("followers") or 0)
                    best = float(item.get("best_trade") or 0)
                    worst = float(item.get("worst_trade") or 0)
                    payoff = (best / abs(worst)) if worst < 0 else (5.0 if best > 0 else 0.0)

                    if evm_lower not in trader_map:
                        trader_map[evm_lower] = {
                            "address": evm_lower,
                            "label": item.get("handle") or evm_lower[:10],
                            "displayName": item.get("display_name"),
                            "followers": followers,
                            "trades": fills,
                            "volumeUsd": vol,
                            "pnlUsd": max(0.0, realized),
                            "realized_pnl": realized,
                            "win_rate": win_rate,
                            "closed_trades": closed,
                            "best_trade": best,
                            "worst_trade": worst,
                            "payoff_ratio": round(payoff, 2),
                            "windows_present": [w],
                            "verified": False,
                            "sources": ["trenches"],
                        }
                    else:
                        trader_map[evm_lower]["realized_pnl"] = realized
                        trader_map[evm_lower]["win_rate"] = win_rate
                        trader_map[evm_lower]["closed_trades"] = closed
                        trader_map[evm_lower]["best_trade"] = best
                        trader_map[evm_lower]["worst_trade"] = worst
                        trader_map[evm_lower]["payoff_ratio"] = round(payoff, 2)
                        trader_map[evm_lower]["trades"] = max(trader_map[evm_lower]["trades"], fills)
                        trader_map[evm_lower]["volumeUsd"] = max(trader_map[evm_lower]["volumeUsd"], vol)
                        if "trenches" not in trader_map[evm_lower]["sources"]:
                            trader_map[evm_lower]["sources"].append("trenches")
                        if w not in trader_map[evm_lower]["windows_present"]:
                            trader_map[evm_lower]["windows_present"].append(w)

        # Filter & score
        eligible_traders = []
        for addr, t in trader_map.items():
            if self.is_eligible(t):
                # Extra bonus for traders present in multiple windows (consistency)
                bonus = len(set(t["windows_present"])) * 3.0
                score = min(100.0, self.calculate_score(t) + bonus)
                t["score"] = round(score, 2)
                eligible_traders.append(t)

        # Sort descending by score
        eligible_traders.sort(key=lambda x: x["score"], reverse=True)
        selected = eligible_traders[:top_n]

        log(f"Discovery complete: {len(trader_map)} evaluated -> {len(eligible_traders)} eligible -> top {len(selected)} selected.")
        return selected

    def export_to_wallets_json(
        self,
        traders: List[Dict[str, Any]],
        output_path: Optional[Path] = None,
        preserve_custom: bool = True,
    ) -> None:
        """Export ranked traders to wallets.json, preserving custom/manual wallets if requested."""
        if output_path is None:
            output_path = ROOT / "wallets.json"

        existing_custom = {}
        if preserve_custom and output_path.exists():
            try:
                old_data = json.loads(output_path.read_text())
                for entry in old_data:
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
                "realized_pnl": t.get("realized_pnl"),
                "win_rate": t.get("win_rate"),
                "payoff_ratio": t.get("payoff_ratio"),
                "volume_usd": t["volumeUsd"],
                "trades": t["trades"],
                "sources": t.get("sources", []),
                "windows": list(set(t.get("windows_present", []))),
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

    print("\n" + "=" * 115)
    print(f"{'Rank':<5} {'Score':<7} {'Handle':<16} {'WinRate':<9} {'Payoff':<8} {'Realized PnL':<14} {'EVM Address':<42} {'Sources':<10}")
    print("-" * 115)
    for idx, t in enumerate(traders, 1):
        handle = t["label"][:14]
        addr = t["address"]
        score = f"{t['score']:.1f}"
        wr = f"{t['win_rate']:.0%}" if t.get("win_rate") is not None else "—"
        payoff = f"{t['payoff_ratio']:.2f}" if t.get("payoff_ratio") is not None else "—"
        realized = t.get("realized_pnl")
        pnl_str = f"${realized:,.0f}" if realized is not None else f"${t['pnlUsd']:,.0f}"
        srcs = ",".join(t.get("sources", ["fomo"]))
        print(f"{idx:<5} {score:<7} {handle:<16} {wr:<9} {payoff:<8} {pnl_str:<14} {addr:<42} {srcs:<10}")
    print("=" * 115 + "\n")


def run_daemon(interval_hours: float, top_n: int, windows: List[str], sources: Optional[List[str]] = None):
    log(f"Running in DAEMON mode (refreshing every {interval_hours} hours)...")
    cfg = load_config()
    discovery_cfg = cfg.get("discovery", {})
    discovery = TraderDiscovery(
        min_trades=discovery_cfg.get("min_trades", 5),
        max_trades=discovery_cfg.get("max_trades", 600),
        min_volume_usd=discovery_cfg.get("min_volume_usd", 2000.0),
        min_pnl_usd=discovery_cfg.get("min_pnl_usd", 100.0),
        min_payoff_ratio=discovery_cfg.get("min_payoff_ratio", 0.8),
        trenches_min_win_rate=discovery_cfg.get("trenches_min_win_rate", 0.40),
    )
    while True:
        try:
            traders = discovery.discover(windows=windows, top_n=top_n, sources=sources)
            discovery.export_to_wallets_json(traders)
            print_traders_table(traders[:10])
        except Exception as e:
            log(f"Daemon error during discovery: {e}")

        sleep_secs = int(interval_hours * 3600)
        log(f"Sleeping for {interval_hours} hours ({sleep_secs}s)...")
        time.sleep(sleep_secs)


def main():
    parser = argparse.ArgumentParser(description="FOMO & RobinhoodTrenches Auto-Discovery & Trader Scoring Engine")
    parser.add_argument("--top", type=int, default=None, help="Number of top traders to select (default from config or 50)")
    parser.add_argument("--windows", type=str, default=None, help="Comma-separated windows (default: 24h,7d,30d)")
    parser.add_argument("--sources", type=str, default=None, help="Comma-separated sources: fomo,trenches (default from config)")
    parser.add_argument("--output", type=str, default="wallets.json", help="Output file path (default: wallets.json)")
    parser.add_argument("--daemon", action="store_true", help="Run periodically in daemon mode")
    parser.add_argument("--interval-hours", type=float, default=None, help="Interval in hours for daemon mode (default from config)")
    args = parser.parse_args()

    cfg = load_config()
    discovery_cfg = cfg.get("discovery", {})

    top_n = args.top or discovery_cfg.get("top_n", 50)
    windows_raw = args.windows or ",".join(discovery_cfg.get("windows", ["24h", "7d", "30d"]))
    windows_list = [w.strip() for w in windows_raw.split(",") if w.strip()]
    sources_raw = args.sources or ",".join(discovery_cfg.get("sources", ["fomo", "trenches"]))
    sources_list = [s.strip() for s in sources_raw.split(",") if s.strip()]
    interval_hours = args.interval_hours or discovery_cfg.get("interval_hours", 2.0)
    output_path = ROOT / args.output

    if args.daemon:
        run_daemon(interval_hours=interval_hours, top_n=top_n, windows=windows_list, sources=sources_list)
        return

    discovery = TraderDiscovery(
        min_trades=discovery_cfg.get("min_trades", 5),
        max_trades=discovery_cfg.get("max_trades", 600),
        min_volume_usd=discovery_cfg.get("min_volume_usd", 2000.0),
        min_pnl_usd=discovery_cfg.get("min_pnl_usd", 100.0),
        min_payoff_ratio=discovery_cfg.get("min_payoff_ratio", 0.8),
        trenches_min_win_rate=discovery_cfg.get("trenches_min_win_rate", 0.40),
    )

    traders = discovery.discover(windows=windows_list, top_n=top_n, sources=sources_list)
    discovery.export_to_wallets_json(traders, output_path=output_path)
    print_traders_table(traders)


if __name__ == "__main__":
    main()
