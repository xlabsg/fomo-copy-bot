#!/usr/bin/env python3
"""rh-copybot analytics: exit timing, follower flow, wallet leaderboard, latency.

  python stats.py                 both instances, text report
  python stats.py --paper         paper only (most trades)   --live  live only
  python stats.py --hours 4       price path horizon after each buy (default 2h)
  python stats.py --refresh       ignore cached price paths

Price paths are rebuilt from the Uniswap pool's own Swap events after each buy
(exact, includes every trade by anyone). Returns are measured against OUR fill
price and quoted in the pool's quote token; USD conversion uses the quote's
price at buy time (fine for USDG/WETH pools, approximate for memecoin-quoted).
"""

import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests
from eth_abi import encode
from eth_utils import keccak

_HERE = Path(__file__).resolve().parent  # works in ~/Documents and in /opt on the droplet
INSTANCES = {"LIVE": _HERE, "PAPER": _HERE.parent / "rh-copybot-paper", "PAPER-B": _HERE.parent / "rh-copybot-paper2"}
RPC = "https://rpc.mainnet.chain.robinhood.com"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
V3_SWAP = "0x" + keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
V4_SWAP = "0x" + keccak(text="Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)").hex()
HORIZONS_MIN = [1, 2, 5, 10, 15, 25, 45, 60, 90, 120]
EXIT_COST = 0.03  # round-trip-ish cost of selling straight back (measured ~2-4%)
CACHE = _HERE / "data/pricepaths"
CACHE.mkdir(parents=True, exist_ok=True)
_s = requests.Session()
_s.headers["User-Agent"] = "rh-copybot-stats/0.1"


# ---------------------------------------------------------------- rpc

def rpc(method, params):
    for attempt in range(4):
        try:
            r = _s.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=40)
            if r.status_code == 429:
                time.sleep(2 + attempt)
                continue
            b = r.json()
            if "error" in b:
                raise RuntimeError(b["error"].get("message", b["error"]))
            return b["result"]
        except (requests.RequestException, RuntimeError) as e:
            if attempt == 3:
                raise
            time.sleep(1 + attempt)


def get_logs(flt, lo, hi, depth=0):
    try:
        return rpc("eth_getLogs", [{**flt, "fromBlock": hex(lo), "toBlock": hex(hi)}])
    except RuntimeError as e:
        if depth > 6 or hi - lo < 200:
            raise
        mid = (lo + hi) // 2
        return get_logs(flt, lo, mid, depth + 1) + get_logs(flt, mid + 1, hi, depth + 1)


_blk_ts = {}


def block_ts(n):
    if n not in _blk_ts:
        _blk_ts[n] = int(rpc("eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)
    return _blk_ts[n]


_head = None


def block_at(ts):
    """Block number closest to a unix timestamp (2-3 RPC calls, ~10 blocks/s chain)."""
    global _head
    if _head is None:
        h = int(rpc("eth_blockNumber", []), 16)
        _head = (h, block_ts(h))
    h, hts = _head
    guess = max(1, min(h, h - int((hts - ts) * 10)))
    for _ in range(4):
        gts = block_ts(guess)
        diff = ts - gts
        if abs(diff) < 2:
            break
        guess = max(1, min(h, guess + int(diff * 10)))
    return guess


def call(to, data, block="latest"):
    return rpc("eth_call", [{"to": to, "data": data}, block])


def decimals(token):
    if int(token, 16) == 0:
        return 18  # native ETH
    try:
        return int(call(token, "0x313ce567"), 16)
    except Exception:
        return 18


# ---------------------------------------------------------------- price paths from swap events

def pool_of(pos):
    """Identify the pool our SELL route hits first (token -> quote): returns
    (kind, pool_id_or_address, quote_addr, token_is_0, dec_token, dec_quote)."""
    tok = pos["token"].lower()
    leg = pos["legs_sell"][0]
    if leg["kind"] == 1:
        k = leg["key"]
        c0, c1 = k["c0"].lower(), k["c1"].lower()
        pid = "0x" + keccak(encode(["address", "address", "uint24", "int24", "address"],
                                   [k["c0"], k["c1"], k["fee"], k["tick"], k["hooks"]])).hex()
        quote = c1 if tok == c0 else c0
        return "v4", pid, quote, tok == c0
    path = bytes.fromhex(leg["path"][2:])
    a, fee, b = "0x" + path[:20].hex(), int.from_bytes(path[20:23], "big"), "0x" + path[23:43].hex()
    data = ("0x1698ee82" + a[2:].rjust(64, "0") + b[2:].rjust(64, "0") + hex(fee)[2:].rjust(64, "0"))
    pool = "0x" + call(V3_FACTORY, data)[-40:]
    quote = b.lower()
    return "v3", pool, quote, tok < quote  # token0 is the lower address


def swap_path(pos, hours, refresh=False):
    """[(ts, price_token_in_quote, quote_flow)] from the pool's Swap events for
    `hours` after the buy. quote_flow > 0 means someone BOUGHT the token."""
    tok = pos["token"].lower()
    cache = CACHE / f"{tok}_{int(pos['bought_at'])}_{hours}h.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())
    kind, pid, quote, tok_is_0 = pool_of(pos)
    dt, dq = decimals(tok), decimals(quote)
    b0 = block_at(pos["bought_at"])
    b1 = min(block_at(pos["bought_at"] + hours * 3600), int(rpc("eth_blockNumber", []), 16))
    if kind == "v4":
        flt = {"address": POOL_MANAGER, "topics": [V4_SWAP, pid]}
    else:
        flt = {"address": pid, "topics": [V3_SWAP]}
    logs = get_logs(flt, b0 - 30, b1)
    out = []
    for lg in logs:
        d = bytes.fromhex(lg["data"][2:])
        words = [d[i:i + 32] for i in range(0, len(d), 32)]
        a0 = int.from_bytes(words[0], "big", signed=True)
        a1 = int.from_bytes(words[1], "big", signed=True)
        sqrt_p = int.from_bytes(words[2], "big")
        if sqrt_p == 0:
            continue
        p = (sqrt_p / 2**96) ** 2  # token1 raw per token0 raw
        price = (p * 10**dt / 10**dq) if tok_is_0 else ((1 / p) * 10**dt / 10**dq)
        qd = a1 if tok_is_0 else a0  # quote delta
        # v3 amounts = pool deltas (+ = into pool); v4 amounts = user deltas (- = paid)
        flow = (qd if kind == "v3" else -qd) / 10**dq
        blk = int(lg["blockNumber"], 16)
        out.append((blk, price, flow))
    # block -> ts: linear between the two anchors (10 blocks/s), good to ~1s
    t0, t1 = block_ts(b0), block_ts(b1)
    rate = (b1 - b0) / max(1, t1 - t0)
    path = [(round(t0 + (blk - b0) / rate, 1), price, flow) for blk, price, flow in out]
    cache.write_text(json.dumps({"path": path, "quote": quote, "kind": kind}))
    return {"path": path, "quote": quote, "kind": kind}


# ---------------------------------------------------------------- loading

def load(inst):
    d = INSTANCES[inst]
    st = json.loads((d / "data/state.json").read_text()) if (d / "data/state.json").exists() else {}
    cfg = json.loads((d / "config.json").read_text())
    positions = list(st.get("positions", {}).values()) + st.get("closed", [])
    sigs = [json.loads(l) for l in (d / "data/signals.jsonl").read_text().splitlines() if l.strip()] \
        if (d / "data/signals.jsonl").exists() else []
    return cfg, positions, sigs


def price_now(tokens):
    out = {}
    toks = list(tokens)
    for i in range(0, len(toks), 30):
        try:
            r = _s.get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(toks[i:i + 30]), timeout=15).json()
        except Exception:
            continue
        best = {}
        for p in r.get("pairs") or []:
            if p.get("chainId") != "robinhood" or not p.get("priceUsd"):
                continue
            a = (p.get("baseToken") or {}).get("address", "").lower()
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if a in toks and (a not in best or liq > best[a][0]):
                best[a] = (liq, float(p["priceUsd"]))
        out.update({a: v[1] for a, v in best.items()})
    return out


# ---------------------------------------------------------------- analyses

REAL_BUY_MARKERS = ("bought", "already holding", "re-entry cooldown", "max positions", "insufficient",
                    "liquidity", "price impact", "round trip", "routing", "thin pool", "low gas", "buy tx failed",
                    "quote failed", "honeypot", "sells vs", "no dexscreener price")


def real_buys(sigs):
    """Signals that were genuine new-token purchases by a watched wallet: drop
    stock tokens, top-ups of an existing bag, dust, and airdrops (many wallets
    'buying' the same token within a minute)."""
    keep = [s for s in sigs if any(m in s.get("outcome", "") for m in REAL_BUY_MARKERS)
            and "stock token" not in s.get("outcome", "") and "wallet already held" not in s.get("outcome", "")]
    by_tok = defaultdict(list)
    for s in keep:
        by_tok[s["token"]].append(s)
    out = []
    for tok, ss in by_tok.items():
        ss.sort(key=lambda s: s["ts"])
        airdrop = False
        for i in range(len(ss)):
            window = {x["wallet"] for x in ss[i:] if x["ts"] - ss[i]["ts"] <= 60}
            if len(window) >= 8:
                airdrop = True
                break
        if not airdrop:
            out.extend(ss)
    return out


def exit_timing(inst, positions, hours, refresh):
    """Per position: return vs our fill at each horizon; peak timing."""
    rows = []
    for pos in positions:
        if not pos.get("initial_raw") or not pos.get("legs_sell"):
            continue
        try:
            sp = swap_path(pos, hours, refresh)
        except Exception as e:
            print(f"   (no price path for {pos['symbol']}: {str(e)[:80]})")
            continue
        path = sp["path"]
        if not path:
            continue
        tokens = pos["initial_raw"] / 10**pos["decimals"]
        # our fill in quote units: first trade at/after our buy time is the best anchor for
        # the quote's USD price; entry price = buy_usd / tokens (USD) -> quote units
        t_buy = pos["bought_at"]
        after = [x for x in path if x[0] >= t_buy - 5]
        if not after:
            continue
        p_entry_quote = after[0][1]
        usd_per_quote = (pos["buy_usd"] / tokens) / p_entry_quote if p_entry_quote else 0
        if usd_per_quote <= 0:
            continue

        def price_at(minutes):
            t = t_buy + minutes * 60
            last = None
            for ts, p, _f in path:
                if ts <= t:
                    last = p
                else:
                    break
            return last if last is not None else after[0][1]

        entry_usd = pos["buy_usd"] / tokens
        ret = {}
        for m in HORIZONS_MIN:
            if t_buy + m * 60 > time.time():
                ret[m] = None
            else:
                ret[m] = price_at(m) * usd_per_quote / entry_usd - 1
        window = [x for x in after if x[0] <= t_buy + hours * 3600]
        peak = max(window, key=lambda x: x[1]) if window else after[0]
        trough = min(window, key=lambda x: x[1]) if window else after[0]
        buys = sum(f for _t, _p, f in window if f > 0)
        sells = -sum(f for _t, _p, f in window if f < 0)
        first5 = [x for x in window if x[0] <= t_buy + 300]
        rows.append({
            "sym": pos["symbol"], "from": pos.get("origin_label", "?"), "ret": ret,
            "peak_ret": peak[1] * usd_per_quote / entry_usd - 1, "peak_min": (peak[0] - t_buy) / 60,
            "trough_ret": trough[1] * usd_per_quote / entry_usd - 1,
            "buy_flow_usd": buys * usd_per_quote, "sell_flow_usd": sells * usd_per_quote,
            "buy_flow_5m": sum(f for _t, _p, f in first5 if f > 0) * usd_per_quote,
            "n_swaps": len(window),
            "actual_pnl": pos.get("pnl_usd"), "closed": "closed_at" in pos, "buy_usd": pos["buy_usd"],
        })
    return rows


def pct(x):
    return "   n/a" if x is None else f"{x * 100:+6.1f}%"


def report_exit_timing(inst, rows, buy_usd):
    print(f"\n{'=' * 118}\n {inst}: EXIT TIMING — return vs our fill if the WHOLE position were sold N minutes after the buy "
          f"({len(rows)} trades with price paths)\n{'=' * 118}")
    if not rows:
        return
    print(f"   {'minutes':>8} | {'avg':>8} {'median':>8} {'win%':>6} | {'pnl if all sold then':>22}  (after {EXIT_COST:.0%} exit cost)")
    for m in HORIZONS_MIN:
        vals = [r["ret"][m] for r in rows if r["ret"][m] is not None]
        if not vals:
            continue
        net = [v - EXIT_COST for v in vals]
        total = sum(v * buy_usd for v in net)
        print(f"   {m:>8} | {statistics.mean(vals) * 100:+7.1f}% {statistics.median(vals) * 100:+7.1f}% "
              f"{sum(1 for v in net if v > 0) / len(net) * 100:5.0f}% | {total:+14,.2f} on {len(vals)} trades")
    actual = [r for r in rows if r["closed"] and r["actual_pnl"] is not None]
    if actual:
        print(f"   {'ACTUAL':>8} | staged exits on the {len(actual)} closed trades: {sum(r['actual_pnl'] for r in actual):+,.2f} "
              f"({sum(1 for r in actual if r['actual_pnl'] > 0) / len(actual) * 100:.0f}% wins)")
    peaks = [r["peak_min"] for r in rows]
    print(f"\n   peak after entry: median {statistics.median(peaks):.0f} min, "
          f"{sum(1 for p in peaks if p <= 5) / len(peaks) * 100:.0f}% peak within 5 min, "
          f"{sum(1 for p in peaks if p <= 15) / len(peaks) * 100:.0f}% within 15 min, "
          f"{sum(1 for p in peaks if p >= 60) / len(peaks) * 100:.0f}% after 60 min")
    print(f"   avg peak return {statistics.mean(r['peak_ret'] for r in rows) * 100:+.0f}%, "
          f"avg trough {statistics.mean(r['trough_ret'] for r in rows) * 100:+.0f}%  (what a perfect / worst exit would have seen)")
    print(f"\n   {'token':<10}{'from':<14}{'2m':>8}{'5m':>8}{'10m':>8}{'25m':>8}{'60m':>8}{'120m':>8}"
          f"{'peak':>8}{'@min':>6}{'buy flow':>11}{'sell flow':>11}{'swaps':>7}   actual")
    for r in sorted(rows, key=lambda r: -(r["ret"][60] if r["ret"][60] is not None else -9)):
        print(f"   {r['sym'][:10]:<10}{r['from'][:13]:<14}{pct(r['ret'][2]):>8}{pct(r['ret'][5]):>8}{pct(r['ret'][10]):>8}"
              f"{pct(r['ret'][25]):>8}{pct(r['ret'][60]):>8}{pct(r['ret'][120]):>8}{pct(r['peak_ret']):>8}"
              f"{r['peak_min']:>6.0f}{r['buy_flow_usd']:>11,.0f}{r['sell_flow_usd']:>11,.0f}{r['n_swaps']:>7}"
              f"   {('%+.2f' % r['actual_pnl']) if r['actual_pnl'] is not None else 'open'}")


def report_flow(inst, sigs, rows):
    """Follower flow among the WATCHED wallets: after the first watched wallet
    buys a token, how many others pile in, how fast, and does it matter?"""
    print(f"\n{'=' * 118}\n {inst}: FOLLOWER FLOW among watched wallets\n{'=' * 118}")
    by_tok = defaultdict(list)
    for s in real_buys(sigs):
        by_tok[s["token"]].append(s)
    # first buyer + followers (distinct wallets) within windows
    ret60 = {r["sym"]: r["ret"][60] for r in rows}
    buckets = defaultdict(list)
    lines = []
    for tok, ss in by_tok.items():
        ss = sorted(ss, key=lambda s: s["ts"])
        first = ss[0]
        t0 = first["ts"]
        wallets = {}
        for s in ss:
            wallets.setdefault(s["wallet"], s["ts"] - t0)
        f5 = sum(1 for w, dt in wallets.items() if 0 < dt <= 300)
        f15 = sum(1 for w, dt in wallets.items() if 0 < dt <= 900)
        f60 = sum(1 for w, dt in wallets.items() if 0 < dt <= 3600)
        copied = any(s.get("outcome") == "bought" for s in ss)
        r = ret60.get(first["symbol"])
        if copied and r is not None:
            buckets["0 followers" if f60 == 0 else "1-2 followers" if f60 <= 2 else "3+ followers"].append(r)
        lines.append((f60, first["symbol"], first["label"], f5, f15, f60, copied, r))
    print(f"   {'token':<10}{'first buyer':<15}{'+5m':>5}{'+15m':>6}{'+60m':>6}  copied  60m return")
    for f60, sym, label, f5, f15, f60_, copied, r in sorted(lines, reverse=True)[:25]:
        print(f"   {sym[:10]:<10}{label[:14]:<15}{f5:>5}{f15:>6}{f60_:>6}  {'yes' if copied else 'no ':<6}  {pct(r)}")
    print("\n   does follower count predict the 60-minute return? (copied trades)")
    for k in ("0 followers", "1-2 followers", "3+ followers"):
        v = buckets.get(k, [])
        if v:
            print(f"   {k:<14} n={len(v):<3} avg {statistics.mean(v) * 100:+6.1f}%  median {statistics.median(v) * 100:+6.1f}%  "
                  f"win {sum(1 for x in v if x > EXIT_COST) / len(v) * 100:.0f}%")


def report_wallets(inst, sigs, positions, rows):
    print(f"\n{'=' * 118}\n {inst}: WALLET LEADERBOARD — who actually produces signals, and are they any good\n{'=' * 118}")
    W = defaultdict(lambda: {"signals": 0, "first": 0, "copied": 0, "pnl": 0.0, "wins": 0, "closed": 0, "r60": []})
    seen_first = {}
    for s in sorted(real_buys(sigs), key=lambda s: s["ts"]):
        w = W[s["label"]]
        w["signals"] += 1
        if s["token"] not in seen_first:
            seen_first[s["token"]] = s["label"]
            w["first"] += 1
        if s.get("outcome") == "bought":
            w["copied"] += 1
    for p in positions:
        w = W[p.get("origin_label", "?")]
        if "closed_at" in p:
            w["closed"] += 1
            w["pnl"] += p.get("pnl_usd", 0)
            w["wins"] += p.get("pnl_usd", 0) > 0
    for r in rows:
        if r["ret"][60] is not None:
            W[r["from"]]["r60"].append(r["ret"][60])
    active = {k: v for k, v in W.items() if v["signals"]}
    silent = [w["label"] if isinstance(w, dict) else w for w in []]
    print(f"   {len(active)} wallets made at least one real new-token buy in this period (airdrops, stock tokens and "
          f"top-ups excluded)")
    print(f"   {'wallet':<16}{'new buys':>9}{'first-in':>9}{'copied':>7}{'closed':>7}{'wins':>5}{'realized':>10}{'avg 60m ret':>13}")
    for k, v in sorted(active.items(), key=lambda kv: (-kv[1]["pnl"], -kv[1]["signals"]))[:30]:
        r60 = f"{statistics.mean(v['r60']) * 100:+.1f}%" if v["r60"] else ""
        print(f"   {k[:15]:<16}{v['signals']:>8}{v['first']:>9}{v['copied']:>7}{v['closed']:>7}{v['wins']:>5}{v['pnl']:>+10.2f}{r60:>13}")


def report_latency(inst, sigs, positions):
    ages = [s["signal_age_s"] for s in sigs if s.get("outcome") == "bought" and s.get("signal_age_s") is not None]
    print(f"\n{'=' * 118}\n {inst}: LATENCY & HOLD TIMES\n{'=' * 118}")
    if ages:
        print(f"   signal age at detection (origin block -> we see it): median {statistics.median(ages):.1f}s, "
              f"p90 {sorted(ages)[int(len(ages) * 0.9)]:.1f}s, max {max(ages):.0f}s  (n={len(ages)})")
    lats = [s["latency"] for s in sigs if s.get("latency")]
    if lats:
        def med(k):
            return statistics.median(l[k] for l in lats)
        print(f"   full chain, live fills (n={len(lats)}): origin block -> mined median {med('total'):.1f}s  =  "
              f"detect {med('detect'):.1f}s + route/quote {med('route'):.1f}s + build/send {med('send'):.1f}s + mining {med('mine'):.1f}s"
              f"   | median {med('blocks_behind'):.0f} blocks behind the origin, worst {max(l['total'] for l in lats):.1f}s")
        slow = sorted(lats, key=lambda l: -l["total"])[:3]
        print("   slowest: " + ", ".join(f"{l['total']:.1f}s (route {l['route']:.1f}s)" for l in slow))
    holds = []
    for p in positions:
        for s in p.get("sells", []):
            if s.get("why") == "origin exit":
                holds.append((s["ts"] - p["bought_at"]) / 60)
    if holds:
        print(f"   origin wallets started exiting after: median {statistics.median(holds):.0f} min, "
              f"{sum(1 for h in holds if h <= 10) / len(holds) * 100:.0f}% within 10 min, "
              f"{sum(1 for h in holds if h >= 60) / len(holds) * 100:.0f}% held over an hour  (n={len(holds)})")
    skips = defaultdict(int)
    for s in sigs:
        o = s.get("outcome", "")
        if o.startswith("skip"):
            k = o.replace("skip: ", "")
            k = ("already holding" if "already" in k else "liquidity below min" if "liquidity" in k else
                 "origin buy too small" if "origin buy" in k else "no route" if "routing" in k else
                 "price impact" if "impact" in k else "insufficient USDG/cash" if ("USDG" in k or "cash" in k) else
                 "max positions" if "max positions" in k else "gas" if "gas" in k else
                 "stock token" if "stock" in k else "thin pool, few sells" if "thin" in k else
                 "round trip" if "round trip" in k else k[:40])
            skips[k] += 1
    print("   why signals were skipped:", ", ".join(f"{k} {v}" for k, v in sorted(skips.items(), key=lambda kv: -kv[1])))


def main():
    args = sys.argv[1:]
    insts = (["PAPER"] if "--paper" in args else ["PAPER-B"] if "--paper-b" in args else ["LIVE"] if "--live" in args
             else ["PAPER", "PAPER-B", "LIVE"])
    hours = int(args[args.index("--hours") + 1]) if "--hours" in args else 2
    refresh = "--refresh" in args
    for inst in insts:
        if not INSTANCES[inst].exists():
            continue
        cfg, positions, sigs = load(inst)
        print(f"\n{inst}: {len(positions)} positions, {len(sigs)} signals — rebuilding price paths ({hours}h after each buy)...",
              flush=True)
        rows = exit_timing(inst, positions, hours, refresh)
        report_exit_timing(inst, rows, cfg.get("buy_usd", 25))
        report_flow(inst, sigs, rows)
        report_wallets(inst, sigs, positions, rows)
        report_latency(inst, sigs, positions)


if __name__ == "__main__":
    main()
