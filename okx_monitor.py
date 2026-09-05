#!/usr/bin/env python3
"""OKX OnchainOS Independent Hot-Token & Market Monitor.

Monitors trending tokens, social sentiment (X mentions), and real capital inflow
from OKX Web3 OnchainOS API without coupling to the copy-trading bot.

Usage:
    python okx_monitor.py              # Run once and display hot tokens table
    python okx_monitor.py --watch      # Continuous monitoring loop
    python okx_monitor.py --chain 4663 # Target specific chain (4663 = Robinhood Chain)
    python okx_monitor.py --test       # Test connectivity and signature generator
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SIGNALS_FILE = DATA_DIR / "okx_signals.jsonl"
ENV_FILE = ROOT / ".env"

OKX_BASE_URL = "https://web3.okx.com"


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] [OKX-Monitor] {msg}", flush=True)


def load_env() -> Dict[str, str]:
    """Parse key-value pairs from .env file."""
    env = {}
    if ENV_FILE.exists():
        try:
            for line in ENV_FILE.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
        except Exception as e:
            log(f"Warning reading .env: {e}")
    # Also inherit system environment
    for k, v in os.environ.items():
        if k not in env:
            env[k] = v
    return env


def make_okx_headers(
    api_key: str,
    api_secret: str,
    passphrase: str,
    project_id: Optional[str],
    method: str,
    request_path: str,
    body: str = "",
) -> Dict[str, str]:
    """Generate authenticated OKX Web3 request headers."""
    # Timestamp in ISO 8601 UTC format with milliseconds
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    message = ts + method.upper() + request_path + body
    mac = hmac.new(api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
    sign = base64.b64encode(mac.digest()).decode("utf-8")

    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "fomo-copy-bot/okx-monitor/1.0",
    }
    if project_id:
        headers["OK-ACCESS-PROJECT"] = project_id
    return headers


class OKXOnchainMonitor:
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        passphrase: Optional[str] = None,
        project_id: Optional[str] = None,
        timeout: int = 12,
    ):
        env = load_env()
        self.api_key = api_key or env.get("OKX_API_KEY")
        self.api_secret = api_secret or env.get("OKX_API_SECRET")
        self.passphrase = passphrase or env.get("OKX_PASSPHRASE")
        self.project_id = project_id or env.get("OKX_PROJECT_ID")
        self.timeout = timeout

    @property
    def has_auth(self) -> bool:
        return bool(self.api_key and self.api_secret and self.passphrase)

    def fetch_hot_tokens(
        self,
        chain_index: str = "4663",
        ranking_type: str = "4",  # 4 = Trending, 5 = Twitter Mentions
        rank_by: str = "12",       # 12 = Social score, 5 = Volume, 14 = Net Inflow
        time_frame: str = "2",     # 1 = 5m, 2 = 1h, 3 = 4h, 4 = 24h
        limit: int = 20,
    ) -> Optional[List[Dict[str, Any]]]:
        """Query /api/v6/dex/market/token/hot-token."""
        path = f"/api/v6/dex/market/token/hot-token?chainIndex={chain_index}&rankingType={ranking_type}&rankBy={rank_by}&rankingTimeFrame={time_frame}&limit={limit}"
        url = f"{OKX_BASE_URL}{path}"

        headers = {
            "Accept": "application/json",
            "User-Agent": "fomo-copy-bot/okx-monitor/1.0",
        }
        if self.has_auth:
            headers.update(
                make_okx_headers(
                    api_key=self.api_key,
                    api_secret=self.api_secret,
                    passphrase=self.passphrase,
                    project_id=self.project_id,
                    method="GET",
                    request_path=path,
                )
            )

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                code = str(data.get("code", ""))
                if code == "0":
                    return data.get("data", [])
                log(f"OKX API returned non-zero code: {code} msg: {data.get('msg')}")
                return None
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="ignore")
            if e.code == 402:
                log("HTTP 402 Payment Required: OKX OnchainOS requires credentials or subscription.")
            elif e.code == 401:
                log("HTTP 401 Unauthorized: Invalid OKX API credentials or signature.")
            else:
                log(f"HTTP Error {e.code}: {raw[:150]}")
            return None
        except Exception as e:
            log(f"Connection error: {e}")
            return None

    def record_signals(self, tokens: List[Dict[str, Any]]) -> None:
        """Persist captured hot token signals to data/okx_signals.jsonl."""
        if not tokens:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        now_ts = time.time()
        with open(SIGNALS_FILE, "a", encoding="utf-8") as f:
            for t in tokens:
                record = {
                    "ts": now_ts,
                    "datetime": datetime.now(timezone.utc).isoformat(),
                    "chain_index": t.get("chainIndex"),
                    "symbol": t.get("tokenSymbol"),
                    "address": t.get("tokenContractAddress"),
                    "price": t.get("price"),
                    "change_pct": t.get("change"),
                    "inflow_usd": t.get("inflowUsd"),
                    "volume_usd": t.get("volume"),
                    "liquidity_usd": t.get("liquidity"),
                    "top10_hold_pct": t.get("top10HoldPercent"),
                    "dev_hold_pct": t.get("devHoldPercent"),
                    "vibe_score": t.get("vibeScore"),
                    "mentions_count": t.get("mentionsCount"),
                    "risk_level": t.get("riskLevelControl"),
                }
                f.write(json.dumps(record) + "\n")
        log(f"Logged {len(tokens)} token records to {SIGNALS_FILE.name}.")


def print_hot_tokens_table(tokens: List[Dict[str, Any]]):
    if not tokens:
        print("\nNo token records returned.")
        return

    print("\n" + "=" * 110)
    print(f"{'#':<3} {'Symbol':<12} {'Price ($)':<12} {'Change%':<9} {'Net Inflow ($)':<16} {'Liquidity ($)':<15} {'Top10 Hold%':<12} {'Address':<42}")
    print("-" * 110)
    for idx, t in enumerate(tokens, 1):
        sym = t.get("tokenSymbol", "?")[:10]
        pr = t.get("price") or "—"
        try:
            pr_val = float(pr)
            pr_str = f"${pr_val:.4g}"
        except Exception:
            pr_str = str(pr)
        chg = t.get("change") or "0"
        chg_str = f"{float(chg):+.1f}%" if chg != "—" else "—"
        inflow = t.get("inflowUsd") or "0"
        try:
            inflow_str = f"${float(inflow):,.0f}"
        except Exception:
            inflow_str = str(inflow)
        liq = t.get("liquidity") or "0"
        try:
            liq_str = f"${float(liq):,.0f}"
        except Exception:
            liq_str = str(liq)
        top10 = t.get("top10HoldPercent") or "—"
        top10_str = f"{float(top10):.1f}%" if top10 != "—" else "—"
        addr = t.get("tokenContractAddress", "")
        print(f"{idx:<3} {sym:<12} {pr_str:<12} {chg_str:<9} {inflow_str:<16} {liq_str:<15} {top10_str:<12} {addr:<42}")
    print("=" * 110 + "\n")


def print_credential_help():
    print("""
========================================================================================
ℹ️  OKX OnchainOS API Credentials Setup Guide
========================================================================================
To enable live OKX Hot-Token monitoring, add your developer credentials to:
  ~/fomo-copy-bot/.env  (on EC2)  or  .env  (locally)

Example .env:
  OKX_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
  OKX_API_SECRET=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
  OKX_PASSPHRASE=YourCustomPassphrase123
  OKX_PROJECT_ID=1234567890 (optional)

How to obtain free credentials:
  1. Visit: https://web3.okx.com/zh-hans/onchainos/dev-portal
  2. Create a Project and generate an API Key.
  3. Enter the Key, Secret, and Passphrase into .env.
========================================================================================
""")


def main():
    parser = argparse.ArgumentParser(description="OKX OnchainOS Hot-Token Independent Monitor")
    parser.add_argument("--chain", type=str, default="4663", help="Chain Index (default: 4663 for Robinhood Chain, 1 for ETH, 501 for Solana)")
    parser.add_argument("--type", type=str, default="4", help="Ranking Type (4 = Trending, 5 = Twitter Mentions)")
    parser.add_argument("--watch", action="store_true", help="Run continuous monitoring loop")
    parser.add_argument("--interval", type=int, default=60, help="Interval in seconds for watch mode (default: 60)")
    parser.add_argument("--limit", type=int, default=20, help="Number of tokens to fetch (default: 20)")
    parser.add_argument("--test", action="store_true", help="Test connectivity to OKX API")
    args = parser.parse_args()

    monitor = OKXOnchainMonitor()

    if not monitor.has_auth:
        print_credential_help()
        log("No OKX API credentials found. Testing connection to OKX endpoint...")

    if args.test:
        log(f"Testing connectivity to OKX OnchainOS endpoint (Chain: {args.chain})...")
        res = monitor.fetch_hot_tokens(chain_index=args.chain, ranking_type=args.type, limit=5)
        if res is not None:
            log(f"Connection SUCCESS! Fetched {len(res)} tokens.")
            print_hot_tokens_table(res)
        else:
            log("Test completed (check logs above for credentials requirement).")
        return

    if args.watch:
        log(f"Starting continuous OKX Hot-Token monitor (every {args.interval}s, chain {args.chain})...")
        while True:
            try:
                tokens = monitor.fetch_hot_tokens(chain_index=args.chain, ranking_type=args.type, limit=args.limit)
                if tokens:
                    monitor.record_signals(tokens)
                    print_hot_tokens_table(tokens)
            except Exception as e:
                log(f"Error in monitor loop: {e}")
            time.sleep(args.interval)
    else:
        tokens = monitor.fetch_hot_tokens(chain_index=args.chain, ranking_type=args.type, limit=args.limit)
        if tokens:
            monitor.record_signals(tokens)
            print_hot_tokens_table(tokens)


if __name__ == "__main__":
    main()
