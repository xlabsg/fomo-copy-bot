#!/usr/bin/env python3
"""rh-copybot: copy new-token buys from a list of wallets on Robinhood Chain.

Every poll (default 1s):
  1. one eth_getLogs pair fetches ERC-20 Transfers touching ANY watched wallet
  2. a wallet RECEIVING a token it held 0 of one block earlier = new-token buy
     -> buy `buy_usd` of USDG worth via CopyRouter (Uniswap V3/V4 legs)
  3. the ORIGIN wallet SENDING that token into a swap/contract = exit signal
  4. exits: `exits` stages by time (50% @1h, 25% @24h), remainder on origin exit

Usage:
  python bot.py                 run (paper unless config.live = true)
  python bot.py status          open/closed positions + PnL
  python bot.py route <token>   dry-run route discovery + quote for one token
  python bot.py holdings <addr> what a watched wallet holds right now
  python bot.py sell <token|symbol> [pct]   sell an open position now (asks to confirm)
  python bot.py adopt <token> <usd_spent>   register tokens the wallet holds but the bot lost track of
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

import requests
from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

# BOT_HOME=<dir> runs the same code from another directory (own config.json,
# wallets.json, .env, data/) — used for the parallel paper instance.
ROOT = Path(os.environ.get("BOT_HOME") or Path(__file__).resolve().parent).expanduser().resolve()
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

# ---- Robinhood Chain mainnet (chain 4663), Uniswap official deployment
CHAIN_ID = 4663
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
QUOTER_V2 = "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7"
V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
POSITION_MANAGER = "0x58daec3116aae6d93017baaea7749052e8a04fa7"
SWAP_ROUTER02 = "0xCaf681a66D020601342297493863E78C959E5cb2"
POOL_MANAGER = "0x8366a39CC670B4001A1121B8F6A443A643e40951"
ZERO = "0x0000000000000000000000000000000000000000"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
FEE_TIERS = (100, 500, 3000, 10000)
FUNDING_SYMBOLS = {"WETH", "ETH", "USDC", "USDT", "USDG", "DAI"}
MAX_UINT = 2**256 - 1
USDG_DEC = 6

LEG_T = "(uint8,bytes,(address,address,uint24,int24,address),bool)"
SWAP_SIG = f"swap({LEG_T}[],uint256,uint256,address)"


# ---------------------------------------------------------------- config / env

def read_env():
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update(os.environ)
    return env


ENV = read_env()
CFG = json.loads((ROOT / "config.json").read_text())
RPC_URL = ENV.get("RPC_URL") or CFG["rpc"]
ROUTER = CFG.get("router") or ""
SLIPPAGE = CFG.get("slippage_pct", 3) / 100


def load_wallets():
    raw = json.loads((ROOT / "wallets.json").read_text())
    out = {}
    for w in raw:
        a = (w["address"] if isinstance(w, dict) else w).lower()
        out[a] = (w.get("label") if isinstance(w, dict) else None) or a[:10]
    return out


WALLETS = load_wallets()  # lower addr -> label
_wallets_mtime = (ROOT / "wallets.json").stat().st_mtime if (ROOT / "wallets.json").exists() else 0.0
_last_wallets_check = 0.0

def check_wallets_reload():
    global WALLETS, _wallets_mtime, _last_wallets_check
    now = time.time()
    if now - _last_wallets_check < 5.0:
        return
    _last_wallets_check = now
    w_path = ROOT / "wallets.json"
    if not w_path.exists():
        return
    try:
        mtime = w_path.stat().st_mtime
        if mtime != _wallets_mtime:
            _wallets_mtime = mtime
            new_w = load_wallets()
            if set(new_w.keys()) != set(WALLETS.keys()):
                log(f"  [HOT-RELOAD] wallets.json changed on disk! Updated to {len(new_w)} wallets (was {len(WALLETS)})")
                WALLETS = new_w
    except Exception as e:
        log(f"  [warn] failed to check/reload wallets.json: {e}")


_cfg_mtime = (ROOT / "config.json").stat().st_mtime if (ROOT / "config.json").exists() else 0.0
_last_cfg_check = 0.0

def check_config_reload():
    global CFG, _cfg_mtime, _last_cfg_check, SLIPPAGE, ROUTER
    now = time.time()
    if now - _last_cfg_check < 5.0:
        return
    _last_cfg_check = now
    c_path = ROOT / "config.json"
    if not c_path.exists():
        return
    try:
        mtime = c_path.stat().st_mtime
        if mtime != _cfg_mtime:
            _cfg_mtime = mtime
            new_cfg = json.loads(c_path.read_text())
            CFG.update(new_cfg)
            SLIPPAGE = CFG.get("slippage_pct", 3) / 100
            ROUTER = CFG.get("router") or ROUTER
            log("  [HOT-RELOAD] config.json reloaded from disk!")
    except Exception as e:
        log(f"  [warn] failed to check/reload config.json: {e}")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def append_jsonl(name, rec):
    with open(DATA / name, "a") as f:
        f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------- JSON-RPC

_s = requests.Session()
_s.headers["User-Agent"] = "rh-copybot/0.1"


FALLBACK_RPC = CFG.get("fallback_rpc") or CFG["rpc"]
_rpc_health = {"fails": 0, "down_until": 0.0, "announced": 0.0}


def rpc_url():
    """Primary RPC, or the public fallback while the primary is marked down."""
    if RPC_URL != FALLBACK_RPC and time.time() < _rpc_health["down_until"]:
        return FALLBACK_RPC
    return RPC_URL


def _reconnect(reason):
    """Drop every pooled keep-alive connection so the next request opens a new
    TCP/TLS session — a reused connection can be pinned to a bad gateway node."""
    global _s
    try:
        _s.close()
    except Exception:
        pass
    _s = requests.Session()
    _s.headers["User-Agent"] = "rh-copybot/0.1"
    append_jsonl("rpc_events.jsonl", {"ts": time.time(), "event": "reconnect", "reason": reason})


def _primary_failed(e):
    """Count hard failures of the primary (403/401/5xx/connection errors);
    after 3 in a row, route everything to the fallback for 60s."""
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", 0) if resp is not None else 0
    append_jsonl("rpc_events.jsonl", {"ts": time.time(), "event": "primary_error", "code": code,
                                      "type": type(e).__name__})
    if code in (401, 403) and time.time() - _rpc_health.get("reconnected", 0) > 5:
        _rpc_health["reconnected"] = time.time()
        _reconnect(f"http {code}")
    if code == 429 or (resp is not None and code < 400):
        return
    _rpc_health["fails"] += 1
    if resp is not None and time.time() - _rpc_health.get("body_logged", 0) > 120:
        _rpc_health["body_logged"] = time.time()
        try:
            body = resp.text[:300].replace("\n", " ").strip() or "(empty body)"
            hdrs = {k: v for k, v in resp.headers.items()
                    if k.lower() in ("content-type", "server", "cf-ray", "x-alchemy-error", "www-authenticate",
                                     "retry-after", "x-ratelimit-remaining")}
        except Exception:
            body, hdrs = "(unreadable)", {}
        log(f"  [rpc] primary answered {code}: {body} | headers {hdrs}")
    if _rpc_health["fails"] >= 3 and RPC_URL != FALLBACK_RPC:
        # sticky: if the primary fails again soon after coming back, stay away longer (1m -> 5m -> 15m)
        recent = time.time() - _rpc_health.get("last_down", 0) < 600
        hold = min(_rpc_health.get("hold", 60) * 5, 900) if recent else 60
        _rpc_health.update(down_until=time.time() + hold, hold=hold, last_down=time.time(), fails=0,
                           episodes=_rpc_health.get("episodes", 0) + 1)
        append_jsonl("rpc_events.jsonl", {"ts": time.time(), "event": "failover", "hold_s": hold})
        if time.time() - _rpc_health["announced"] > 300:
            _rpc_health["announced"] = time.time()
            log(f"  [rpc] primary RPC failing ({code or type(e).__name__}), episode {_rpc_health['episodes']} — "
                f"using the public RPC for {hold // 60} min")


def rpc(method, params, retries=3, url=None):
    for attempt in range(retries):
        target = url or rpc_url()
        try:
            r = _s.post(target, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                      "params": params}, timeout=20)
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                raise RuntimeError(f"{method}: {body['error']}")
            if target == RPC_URL:
                _rpc_health["fails"] = 0
            return body["result"]
        except (requests.RequestException, RuntimeError) as e:
            resp = getattr(e, "response", None)
            if isinstance(e, requests.RequestException) and target == RPC_URL and not url:
                _primary_failed(e)
                if rpc_url() != target:
                    return rpc(method, params, retries=2)  # failover just engaged: finish on the fallback
                if resp is not None and resp.status_code in (401, 403) and attempt < retries - 1:
                    continue  # fresh connection now: retry at once
            if attempt == retries - 1:
                raise
            time.sleep(2.0 if resp is not None and resp.status_code == 429 else 0.5 * (attempt + 1))


def rpc_batch(calls, url=None):
    """One HTTP request for several JSON-RPC calls (public RPCs rate-limit per
    request, so the whole poll tick costs one). Results in call order."""
    if not CFG.get("rpc_batching", True):
        return [rpc(m, p, url=url) for m, p in calls]
    payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
    target = url or rpc_url()
    try:
        r = _s.post(target, json=payload, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        if target == RPC_URL:
            _primary_failed(e)
            if rpc_url() != target:
                return rpc_batch(calls)
        raise
    if target == RPC_URL:
        _rpc_health["fails"] = 0
    body = sorted(r.json(), key=lambda x: x["id"])
    for b in body:
        if "error" in b:
            raise RuntimeError(f"{calls[b['id']][0]}: {b['error']}")
    return [b["result"] for b in body]


def selector(sig):
    return keccak(text=sig)[:4]


def calldata(sig, types, args):
    return "0x" + (selector(sig) + encode(types, args)).hex()


def addr(a):
    return to_checksum_address(a)


def call(to, sig, types, args, rets, block="latest"):
    out = rpc("eth_call", [{"to": addr(to), "data": calldata(sig, types, args)}, block])
    return decode(rets, bytes.fromhex(out[2:]))


def call_batch(items, block="latest"):
    """Many eth_calls in ONE HTTP request. items: [(to, sig, types, args, rets)];
    returns decoded tuples, None where a call failed."""
    if not CFG.get("rpc_batching", True):
        out = []
        for to, sig, types, args, rets in items:
            try:
                out.append(call(to, sig, types, args, rets, block))
            except Exception:
                out.append(None)
        return out
    payload = [{"jsonrpc": "2.0", "id": i, "method": "eth_call",
                "params": [{"to": addr(to), "data": calldata(sig, types, args)}, block]}
               for i, (to, sig, types, args, rets) in enumerate(items)]
    for attempt in range(3):
        target = rpc_url()
        try:
            r = _s.post(target, json=payload, timeout=20)
            r.raise_for_status()
            body = {b["id"]: b for b in r.json()}
            if target == RPC_URL:
                _rpc_health["fails"] = 0
            break
        except requests.RequestException as e:
            if target == RPC_URL:
                _primary_failed(e)
                if rpc_url() != target:
                    return call_batch(items, block)
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
    out = []
    for i, (to, sig, types, args, rets) in enumerate(items):
        b = body.get(i, {})
        try:
            out.append(decode(rets, bytes.fromhex(b["result"][2:])) if b.get("result") else None)
        except Exception:
            out.append(None)
    return out


def balance_of(token, who, block="latest"):
    return call(token, "balanceOf(address)", ["address"], [addr(who)], ["uint256"], block)[0]


_code_cache = {}


def is_contract(a):
    a = a.lower()
    if a not in _code_cache:
        _code_cache[a] = rpc("eth_getCode", [a, "latest"]) not in ("0x", None)
    return _code_cache[a]


_block_ts = {}


def block_time(block):
    if block not in _block_ts:
        for attempt in range(6):
            blk = rpc("eth_getBlockByNumber", [hex(block), False])
            if blk:
                _block_ts[block] = int(blk["timestamp"], 16)
                break
            time.sleep(0.3)  # fresh block not yet visible on this node
        else:
            raise RuntimeError(f"block {block} not found")
    return _block_ts[block]


# ---------------------------------------------------------------- token metadata

_META_FILE = DATA / "tokens.json"
_meta = json.loads(_META_FILE.read_text()) if _META_FILE.exists() else {}


def _decode_string(hexdata):
    raw = bytes.fromhex(hexdata[2:])
    if len(raw) == 32:
        return raw.rstrip(b"\x00").decode("utf-8", "replace")
    n = int.from_bytes(raw[32:64], "big")
    return raw[64:64 + n].decode("utf-8", "replace")


def token_meta(token):
    t = token.lower()
    if t not in _meta:
        try:
            sym = _decode_string(rpc("eth_call", [{"to": t, "data": "0x95d89b41"}, "latest"]))
            dec = int(rpc("eth_call", [{"to": t, "data": "0x313ce567"}, "latest"]), 16)
        except Exception:
            sym, dec = t[:8], 18
        try:
            name = _decode_string(rpc("eth_call", [{"to": t, "data": "0x06fdde03"}, "latest"]))  # name()
        except Exception:
            name = ""
        _meta[t] = {"symbol": sym, "decimals": dec, "name": name}
        _META_FILE.write_text(json.dumps(_meta, indent=1))
    return _meta[t]


# ---------------------------------------------------------------- dexscreener

_px_cache, _px_good = {}, {}
PRICE_TTL = 15


def dex_get(url):
    for attempt in range(3):
        r = _s.get(url, timeout=15)
        if r.status_code == 429:
            time.sleep(1 + attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise requests.RequestException("dexscreener 429")


def token_info(token, fresh=False):
    """{price, liquidity, symbol, buys24, sells24, pairs} from the deepest
    Robinhood Chain pair. Failures are never cached; last good value served."""
    key = token.lower()
    now = time.time()
    if not fresh and key in _px_cache and now - _px_cache[key][0] < PRICE_TTL:
        return _px_cache[key][1]
    info = {"price": None, "liquidity": 0.0, "symbol": None, "buys24": 0, "sells24": 0, "pairs": [],
            "pair_created_at": None, "age_seconds": None}
    try:
        pairs = [p for p in (dex_get(f"https://api.dexscreener.com/latest/dex/tokens/{token}")
                             .get("pairs") or []) if p.get("chainId") == "robinhood"]
    except (requests.RequestException, ValueError):
        return _px_good.get(key, info)
    info["pairs"] = pairs
    info["buys24"] = sum(((p.get("txns") or {}).get("h24") or {}).get("buys") or 0 for p in pairs)
    info["sells24"] = sum(((p.get("txns") or {}).get("h24") or {}).get("sells") or 0 for p in pairs)
    created_times = [p.get("pairCreatedAt") for p in pairs if p.get("pairCreatedAt")]
    if created_times:
        pair_created_at = min(created_times) / 1000.0
        info["pair_created_at"] = pair_created_at
        info["age_seconds"] = max(0.0, now - pair_created_at)
    best = None
    for p in pairs:
        liq = (p.get("liquidity") or {}).get("usd") or 0
        base, quote = p.get("baseToken", {}), p.get("quoteToken", {})
        px, sym = None, None
        if (base.get("address") or "").lower() == key:
            px, sym = p.get("priceUsd"), base.get("symbol")
        elif (quote.get("address") or "").lower() == key and p.get("priceUsd") and p.get("priceNative"):
            sym = quote.get("symbol")
            try:
                px = float(p["priceUsd"]) / float(p["priceNative"])
            except (ValueError, ZeroDivisionError):
                px = None
        if px is not None and (best is None or liq > best[0]):
            best = (liq, float(px), sym)
    if best:
        info.update(liquidity=best[0], price=best[1], symbol=best[2])
    _px_cache[key] = (now, info)
    if info["price"] is not None:
        _px_good[key] = info
    return info


def dex_pairs(token, label):
    """Uniswap pairs of one version (v3/v4), deepest first: [(other, liq, pair_id)]."""
    out = []
    pairs = [p for p in token_info(token)["pairs"]
             if p.get("dexId") == "uniswap" and (p.get("labels") or []) == [label]]
    for p in sorted(pairs, key=lambda p: -((p.get("liquidity") or {}).get("usd") or 0)):
        base, quote = p["baseToken"]["address"], p["quoteToken"]["address"]
        other = quote if base.lower() == token.lower() else base
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if liq >= 1000 and other.lower() not in {a.lower() for a, _, _ in out}:
            out.append((other, liq, p["pairAddress"]))
    return out


def honeypot_reason(info):
    b, s = info.get("buys24", 0), info.get("sells24", 0)
    if b >= 10 and s == 0:
        return f"0 sells vs {b} buys (honeypot signature)"
    if b >= 30 and s > 0 and b / s > 25:
        return f"buys/sells {b}/{s} (near-unsellable)"
    return None


# ---------------------------------------------------------------- routing (Uniswap V3 + V4)
# Leg dicts: {"kind":0,"path":"0x.."}  v3 multihop
#            {"kind":1,"key":{c0,c1,fee,tick,hooks},"zf":bool}  v4 single pool

def encode_path(*hops):
    out = b""
    for h in hops:
        out += h.to_bytes(3, "big") if isinstance(h, int) else bytes.fromhex(h[2:])
    return "0x" + out.hex()


def leg_tuple(leg):
    if leg["kind"] == 0:
        return (0, bytes.fromhex(leg["path"][2:]), (ZERO, ZERO, 0, 0, ZERO), False)
    k = leg["key"]
    return (1, b"", (addr(k["c0"]), addr(k["c1"]), k["fee"], k["tick"], addr(k["hooks"])), leg["zf"])


def deepest_fees(token, quotes):
    """{quote: (depth_usd, fee) or None} for several quote tokens at once:
    one batch for all getPool lookups, one for the balances of pools that exist."""
    combos = [(q, fee) for q, _p, _d in quotes for fee in FEE_TIERS]
    pools = call_batch([(V3_FACTORY, "getPool(address,address,uint24)", ["address", "address", "uint24"],
                         [addr(token), addr(q), fee], ["address"]) for q, fee in combos])
    live = [(q, fee, r[0]) for (q, fee), r in zip(combos, pools) if r and int(r[0], 16) != 0]
    bals = call_batch([(q, "balanceOf(address)", ["address"], [addr(pool)], ["uint256"]) for q, _f, pool in live]) if live else []
    best = {q: None for q, _p, _d in quotes}
    meta = {q: (p, d) for q, p, d in quotes}
    for (q, fee, _pool), b in zip(live, bals):
        if not b:
            continue
        price, dec = meta[q]
        depth = b[0] / 10**dec * price
        if best[q] is None or depth > best[q][0]:
            best[q] = (depth, fee)
    return best


def deepest_fee(token, quote, qprice, qdec):
    return deepest_fees(token, [(quote, qprice, qdec)])[quote]


_uw_fee = None


def usdg_weth_fee():
    global _uw_fee
    if _uw_fee is None:
        best = None
        for fee in FEE_TIERS:
            pool = call(V3_FACTORY, "getPool(address,address,uint24)", ["address", "address", "uint24"],
                        [addr(USDG), addr(WETH), fee], ["address"])[0]
            if int(pool, 16) == 0:
                continue
            d = balance_of(WETH, pool)
            if best is None or d > best[0]:
                best = (d, fee)
        _uw_fee = best[1]
    return _uw_fee


def quote_v3(path_hex, amount_in):
    return call(QUOTER_V2, "quoteExactInput(bytes,uint256)", ["bytes", "uint256"],
                [bytes.fromhex(path_hex[2:]), amount_in],
                ["uint256", "uint160[]", "uint32[]", "uint256"])[0]


def quote_v4(key, zf, amount_in):
    t = "((address,address,uint24,int24,address),bool,uint128,bytes)"
    return call(V4_QUOTER, f"quoteExactInputSingle({t})", [t],
                [((addr(key["c0"]), addr(key["c1"]), key["fee"], key["tick"], addr(key["hooks"])),
                  zf, amount_in, b"")], ["uint256", "uint256"])[0]


def _quote_item(leg, amt):
    if leg["kind"] == 0:
        return (QUOTER_V2, "quoteExactInput(bytes,uint256)", ["bytes", "uint256"],
                [bytes.fromhex(leg["path"][2:]), amt], ["uint256", "uint160[]", "uint32[]", "uint256"])
    k = leg["key"]
    t = "((address,address,uint24,int24,address),bool,uint128,bytes)"
    return (V4_QUOTER, f"quoteExactInputSingle({t})", [t],
            [((addr(k["c0"]), addr(k["c1"]), k["fee"], k["tick"], addr(k["hooks"])), leg["zf"], amt, b"")],
            ["uint256", "uint256"])


def quote_routes(legs, amounts):
    """Quote several input amounts through the same legs; one batched RPC
    round trip per leg instead of one call per amount per leg."""
    amts = [int(a) for a in amounts]
    for leg in legs:
        res = call_batch([_quote_item(leg, a) for a in amts])
        if any(r is None for r in res):
            raise RuntimeError("quote reverted")
        amts = [r[0] for r in res]
    return amts


def quote_route(legs, amount_in):
    return quote_routes(legs, [amount_in])[0]


V4_INIT_TOPIC = "0x" + keccak(text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)").hex()
_pool_key_cache = {}


def v4_pool_key(pool_id):
    """PoolKey for a V4 pool id. PositionManager.poolKeys knows pools whose
    liquidity went through it; hook-managed pools (e.g. APU/AI) are missing
    there, so fall back to the PoolManager's Initialize event, which carries
    the full key. That log query is address+topic indexed and answers from
    block 0 instantly, but Alchemy's free tier caps getLogs ranges, so it is
    tried on the primary RPC first and then on the public one."""
    if pool_id in _pool_key_cache:
        return _pool_key_cache[pool_id]
    key = None
    c0, c1, fee, tick, hooks = call(POSITION_MANAGER, "poolKeys(bytes25)", ["bytes25"],
                                    [bytes.fromhex(pool_id[2:52])],
                                    ["address", "address", "uint24", "int24", "address"])
    if int(c1, 16) != 0:  # registered there (c0 may legitimately be native ETH = 0)
        key = {"c0": c0, "c1": c1, "fee": fee, "tick": tick, "hooks": hooks}
    else:
        flt = {"fromBlock": "0x0", "toBlock": "latest", "address": POOL_MANAGER,
               "topics": [V4_INIT_TOPIC, pool_id]}
        for url in dict.fromkeys([RPC_URL, CFG.get("fallback_rpc") or CFG["rpc"]]):
            try:
                logs = rpc("eth_getLogs", [flt], retries=1, url=url)
            except Exception:
                continue
            if logs:
                lg = logs[0]
                d = bytes.fromhex(lg["data"][2:])
                key = {"c0": "0x" + lg["topics"][2][-40:], "c1": "0x" + lg["topics"][3][-40:],
                       "fee": int.from_bytes(d[0:32], "big"),
                       "tick": int.from_bytes(d[32:64], "big", signed=True),
                       "hooks": "0x" + d[64:96][-20:].hex()}
            break
    if key and int(key["c1"], 16) == 0:
        key = None  # malformed
    if key and int(key["c0"], 16) == 0 and not native_ok():
        key = None  # native-ETH pool: needs CopyRouter v2 (config native_eth_routes)
    _pool_key_cache[pool_id] = key
    return key


def v3_prefix_to(target, eth_price):
    """Cheapest v3 legs USDG->target and target->USDG, or None."""
    t = target.lower()
    if t == USDG.lower():
        return [], []
    if t == WETH.lower():
        uw = usdg_weth_fee()
        return ([{"kind": 0, "path": encode_path(USDG, uw, WETH)}],
                [{"kind": 0, "path": encode_path(WETH, uw, USDG)}])
    both = deepest_fees(target, [(USDG, 1.0, USDG_DEC), (WETH, eth_price, 18)])
    d, w = both[USDG], both[WETH]
    if d and d[0] >= 1000:
        return ([{"kind": 0, "path": encode_path(USDG, d[1], target)}],
                [{"kind": 0, "path": encode_path(target, d[1], USDG)}])
    if w and w[0] >= 1000:
        uw = usdg_weth_fee()
        return ([{"kind": 0, "path": encode_path(USDG, uw, WETH, w[1], target)}],
                [{"kind": 0, "path": encode_path(target, w[1], WETH, uw, USDG)}])
    return None


def v4_direct(a, b):
    """Deepest V4 pool between tokens a and b with a resolvable key ->
    ({"key","zf_a_to_b","liq"}) or None."""
    for other, liq, pid in dex_pairs(a, "v4"):
        if other.lower() != b.lower():
            continue
        key = v4_pool_key(pid)
        if key and a.lower() in (key["c0"].lower(), key["c1"].lower()):
            return {"key": key, "zf": a.lower() == key["c0"].lower(), "liq": liq}
    return None


def native_ok():
    return bool(CFG.get("native_eth_routes", False))


_native_prefix = {"ts": 0.0, "legs": None}


def native_prefix():
    """Legs USDG->ETH and ETH->USDG through the deepest USDG/native-ETH v4 pool
    (cached 10 min). None if the router can't do native or no pool resolves."""
    if not native_ok():
        return None
    if time.time() - _native_prefix["ts"] < 600 and _native_prefix["legs"] is not None:
        return _native_prefix["legs"]
    best = None
    for other, liq, pid in dex_pairs(USDG, "v4"):
        if other.lower() != ZERO:
            continue
        key = v4_pool_key(pid)
        if key and int(key["c0"], 16) == 0 and key["c1"].lower() == USDG.lower() and (best is None or liq > best[0]):
            best = (liq, key)
    legs = None
    if best:
        key = best[1]
        legs = ([{"kind": 1, "key": key, "zf": False}],   # USDG (currency1) -> ETH
                [{"kind": 1, "key": key, "zf": True}])    # ETH -> USDG
    _native_prefix.update(ts=time.time(), legs=legs)
    return legs


def prefix_to(target, eth_price):
    """Legs USDG->target and target->USDG. V3 first; otherwise a single V4 pool
    quoted in USDG (e.g. FAMI), or in WETH behind the V3 USDG->WETH hop."""
    if target.lower() == ZERO:  # the pool is quoted in native ETH
        return native_prefix()
    v3 = v3_prefix_to(target, eth_price)
    if v3 is not None:
        return v3
    d = v4_direct(target, USDG)
    if d and d["liq"] >= 1000:
        return ([{"kind": 1, "key": d["key"], "zf": not d["zf"]}],
                [{"kind": 1, "key": d["key"], "zf": d["zf"]}])
    w = v4_direct(target, WETH)
    if w and w["liq"] >= 1000:
        uw = usdg_weth_fee()
        return ([{"kind": 0, "path": encode_path(USDG, uw, WETH)}, {"kind": 1, "key": w["key"], "zf": not w["zf"]}],
                [{"kind": 1, "key": w["key"], "zf": w["zf"]}, {"kind": 0, "path": encode_path(WETH, uw, USDG)}])
    # the intermediate itself only trades against native ETH: USDG -> ETH -> target
    n = v4_direct(target, ZERO) if native_ok() else None
    if n and n["liq"] >= 1000:
        np = native_prefix()
        if np:
            return (np[0] + [{"kind": 1, "key": n["key"], "zf": not n["zf"]}],
                    [{"kind": 1, "key": n["key"], "zf": n["zf"]}] + np[1])
    return None


def quote_check(legs_buy, legs_sell, amount_in):
    """(amount_out, impact, round_trip) measured on-chain: impact = how much
    worse our fill is than a $1 probe through the same route (independent of
    dexscreener's lagging price); round_trip = selling the output straight back."""
    probe_in = 10**USDG_DEC
    out, probe_out = quote_routes(legs_buy, [amount_in, probe_in])
    impact = 1 - (out / amount_in) / (probe_out / probe_in)
    back = quote_route(legs_sell, out)
    return out, impact, back / amount_in - 1


def discover_route(token):
    """Best USDG<->token route -> (legs_buy, legs_sell, desc, depth_usd).
    Candidates: V3 USDG-direct / WETH-hop / V3-intermediate, and the token's
    deepest V4 pools (with a V3 or V4 prefix from USDG to whatever the pool is
    quoted in, e.g. USDG->FAMI (v4) ->JINQIAN (v4)). Deepest quotable wins."""
    eth_price = token_info(WETH)["price"] or 0
    cands = []  # (depth, legs_buy, legs_sell, desc)

    def v3(pb, ps, depth, desc):
        cands.append((depth, [{"kind": 0, "path": pb}], [{"kind": 0, "path": ps}], desc))

    both = deepest_fees(token, [(USDG, 1.0, USDG_DEC), (WETH, eth_price, 18)])
    d, w = both[USDG], both[WETH]
    if d and d[0] >= 1000:
        v3(encode_path(USDG, d[1], token), encode_path(token, d[1], USDG), d[0], f"v3 USDG direct fee {d[1]}")
    if w and w[0] >= 1000:
        uw = usdg_weth_fee()
        v3(encode_path(USDG, uw, WETH, w[1], token), encode_path(token, w[1], WETH, uw, USDG),
           w[0], f"v3 WETH hop fee {w[1]}")

    if not cands:
        skip = {USDG.lower(), WETH.lower(), token.lower(), ZERO}
        for inter, _liq, _pid in dex_pairs(token, "v3"):
            if inter.lower() in skip:
                continue
            iprice = token_info(inter)["price"]
            if not iprice:
                continue
            idec = token_meta(inter)["decimals"]
            ti = deepest_fee(token, inter, iprice, idec)
            if not ti or ti[0] < 1000:
                continue
            prefix = v3_prefix_to(inter, eth_price)
            if prefix is None:
                continue
            pre_buy, pre_sell = prefix
            buy_path = pre_buy[0]["path"] + ti[1].to_bytes(3, "big").hex() + token[2:].lower()
            sell_path = "0x" + token[2:].lower() + ti[1].to_bytes(3, "big").hex() + pre_sell[0]["path"][2:]
            v3(buy_path, sell_path, ti[0], f"v3 via {inter[:10]}.. fee {ti[1]}")
            break

    for other, liq, pair_id in dex_pairs(token, "v4")[:3]:  # always compete with v3 by depth
        if other.lower() == ZERO and not native_ok():
            continue
        key = v4_pool_key(pair_id)
        if key is None or token.lower() not in (key["c0"].lower(), key["c1"].lower()):
            continue
        prefix = prefix_to(other, eth_price)
        if prefix is None:
            continue
        pre_buy, pre_sell = prefix
        zf_buy = other.lower() == key["c0"].lower()
        legs_buy = pre_buy + [{"kind": 1, "key": key, "zf": zf_buy}]
        legs_sell = [{"kind": 1, "key": key, "zf": not zf_buy}] + pre_sell
        hooked = " (hooked)" if int(key["hooks"], 16) else ""
        vs = "native ETH" if other.lower() == ZERO else other[:10] + ".."
        cands.append((liq, legs_buy, legs_sell, f"v4 pool vs {vs}{hooked}"))
        break

    for depth, lb, ls, desc in sorted(cands, key=lambda c: -c[0]):
        try:
            # probe BOTH directions: a pool can quote buys yet revert every sell
            # (one-sided liquidity), and we must be able to get out
            out = quote_route(lb, 5 * 10**USDG_DEC)
            if out > 0 and quote_route(ls, out) > 0:
                return lb, ls, desc, depth
        except Exception:
            continue
    raise RuntimeError("no routable Uniswap liquidity")


# ---------------------------------------------------------------- signing / sending

class TxPending(Exception):
    """A transaction was broadcast but its receipt could not be fetched in
    time. The caller must NOT retry blindly: it may have been mined."""

    def __init__(self, tx_hash):
        super().__init__(f"tx {tx_hash} sent, receipt not seen yet")
        self.tx_hash = tx_hash


def wait_receipt(tx_hash, seconds=90):
    """Poll for a receipt, tolerating transient RPC errors. None on timeout."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            rec = rpc("eth_getTransactionReceipt", [tx_hash], retries=1)
            if rec:
                return rec
        except Exception:
            pass
        time.sleep(0.3)
    return None


class Signer:
    def __init__(self, key):
        self.acct = Account.from_key(key)
        self.address = self.acct.address
        self.last_nonce = -1
        self.last_sent_ts = None

    def next_nonce(self):
        # a load-balanced RPC can lag on "pending"; never reuse a nonce we sent
        n = int(rpc("eth_getTransactionCount", [self.address, "pending"]), 16)
        return max(n, self.last_nonce + 1)

    def send(self, to, data, gas_mult=1.3):
        tx = {"from": self.address, "to": addr(to), "data": data}
        gas_hex, gp_hex, n_hex = rpc_batch([("eth_estimateGas", [tx]), ("eth_gasPrice", []),
                                            ("eth_getTransactionCount", [self.address, "pending"])])
        gas = int(gas_hex, 16)  # a revert surfaces here, before we pay
        gp = int(gp_hex, 16)
        nonce = max(int(n_hex, 16), self.last_nonce + 1)
        signed = self.acct.sign_transaction({
            "chainId": CHAIN_ID, "nonce": nonce, "to": addr(to), "data": data, "value": 0,
            "gas": int(gas * gas_mult), "gasPrice": int(gp * 1.5)})
        raw = signed.raw_transaction.hex()
        h = "0x" + signed.hash.hex().removeprefix("0x")  # known before sending: lets us recover a lost response
        self.last_sent_ts = time.time()
        try:
            rpc("eth_sendRawTransaction", ["0x" + raw.removeprefix("0x")], retries=1)
        except Exception as e:
            msg = str(e)
            # The node may have accepted the tx even though our response was lost; a
            # blind resend then fails with "nonce too low"/"already known". Check the
            # hash we computed before declaring failure.
            if any(k in msg for k in ("nonce too low", "already known", "already exists", "known transaction")) \
                    or isinstance(e, requests.RequestException):
                rec = wait_receipt(h, seconds=20)
                if rec is not None:
                    self.last_nonce = nonce
                    if int(rec["status"], 16) != 1:
                        raise RuntimeError(f"tx {h} reverted")
                    return rec
            if "nonce too low" in msg:
                self.last_nonce = int(rpc("eth_getTransactionCount", [self.address, "latest"]), 16) - 1
            raise
        self.last_nonce = nonce
        rec = wait_receipt(h)
        if rec is None:
            raise TxPending(h)
        if int(rec["status"], 16) != 1:
            raise RuntimeError(f"tx {h} reverted")
        return rec


SIGNER = None


def ensure_allowance(token, amount):
    cur = call(token, "allowance(address,address)", ["address", "address"],
               [SIGNER.address, addr(ROUTER)], ["uint256"])[0]
    # tokens decrement the allowance on spend, so a MAX approval never reads as
    # MAX again: treat anything above 1e30 raw as "effectively unlimited"
    need = min(amount, 10**30)
    if cur < need:
        SIGNER.send(token, calldata("approve(address,uint256)", ["address", "uint256"], [addr(ROUTER), MAX_UINT]))
        log(f"  approved {token_meta(token)['symbol']} for router")


def swap_tx(legs, amount_in, min_out):
    data = calldata(SWAP_SIG, [f"{LEG_T}[]", "uint256", "uint256", "address"],
                    [[leg_tuple(l) for l in legs], amount_in, min_out, SIGNER.address])
    return SIGNER.send(ROUTER, data)


def sell_simulates(token, legs_sell, raw):
    """Dry-run a real sell of `raw` from our wallet (eth_estimateGas executes the
    swap including token transfers, which the quoters do not) -> True/False."""
    try:
        q = quote_route(legs_sell, raw)
        data = calldata(SWAP_SIG, [f"{LEG_T}[]", "uint256", "uint256", "address"],
                        [[leg_tuple(l) for l in legs_sell], raw, int(q * 0.5), SIGNER.address])
        rpc("eth_estimateGas", [{"from": SIGNER.address, "to": addr(ROUTER), "data": data}])
        return True
    except Exception:
        return False


def received(rec, token):
    me = SIGNER.address.lower()
    return sum(int(lg["data"], 16) for lg in rec["logs"]
               if lg["address"].lower() == token.lower() and len(lg["topics"]) == 3
               and lg["topics"][0] == TRANSFER_TOPIC and "0x" + lg["topics"][2][-40:] == me)


# ---------------------------------------------------------------- state

STATE_FILE = DATA / "state.json"
STATE = {"positions": {}, "closed": [], "last_block": 0}
if STATE_FILE.exists():
    STATE.update(json.loads(STATE_FILE.read_text()))


def save_state():
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(STATE, indent=1))
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- watching

def pad(a):
    return "0x" + a[2:].lower().rjust(64, "0")


_SIZE_HINTS = ("limit", "range", "too large", "too many", "response size", "-32005",
               "internal server")  # public RPC answers big ranges with a bare internal error


def get_logs(topics, from_b, to_b):
    params = {"fromBlock": hex(from_b), "toBlock": hex(to_b), "topics": topics}
    try:
        return rpc("eth_getLogs", [params], retries=2)
    except (RuntimeError, requests.HTTPError) as e:
        if "429" in str(e) or not any(h in str(e).lower() for h in _SIZE_HINTS) or to_b - from_b < 4:
            raise
        mid = (from_b + to_b) // 2
        return get_logs(topics, from_b, mid) + get_logs(topics, mid + 1, to_b)


def transfer_logs(from_b, to_b):
    wl = [pad(a) for a in WALLETS]
    logs = get_logs([TRANSFER_TOPIC, wl], from_b, to_b) + get_logs([TRANSFER_TOPIC, None, wl], from_b, to_b)
    seen, out = set(), []
    for lg in logs:
        k = (lg["transactionHash"], lg["logIndex"])
        if k not in seen and len(lg["topics"]) == 3:
            seen.add(k)
            out.append(lg)
    return out


def wallet_events(logs):
    """Group Transfer logs by (tx, watched wallet) -> ins/outs per wallet."""
    by_tx = {}
    for lg in logs:
        by_tx.setdefault(lg["transactionHash"], []).append(lg)
    events = []
    for tx, lgs in sorted(by_tx.items(), key=lambda kv: int(kv[1][0]["blockNumber"], 16)):
        parts = set()
        for lg in lgs:
            parts.add("0x" + lg["topics"][1][-40:])
            parts.add("0x" + lg["topics"][2][-40:])
        for w in parts & WALLETS.keys():
            ins, outs, cp_in, cp_out = {}, {}, set(), set()
            for lg in lgs:
                tok = lg["address"].lower()
                s, r = "0x" + lg["topics"][1][-40:], "0x" + lg["topics"][2][-40:]
                amt = int(lg["data"], 16) if lg["data"] not in ("0x", "") else 0
                if s == w:
                    outs[tok] = outs.get(tok, 0) + amt
                    cp_out.add(r)
                if r == w:
                    ins[tok] = ins.get(tok, 0) + amt
                    cp_in.add(s)
            events.append({"tx": tx, "block": int(lgs[0]["blockNumber"], 16), "wallet": w,
                           "ins": ins, "outs": outs, "cp_in": cp_in, "cp_out": cp_out})
    return events


def is_funding(tok):
    """Stablecoins / gas tokens: never a token to copy, and their leg of a swap
    is the quote side."""
    if token_meta(tok)["symbol"].upper() in FUNDING_SYMBOLS:
        return True
    info = token_info(tok)
    return info["liquidity"] > 500_000 and info["price"] and 0.98 < info["price"] < 1.02


def is_stock_token(tok):
    """Robinhood's tokenized equities/ETFs all carry the on-chain name suffix
    '• Robinhood Token' (NVDA, SPY, GLD, DJT, ...)."""
    return "robinhood token" in (token_meta(tok).get("name") or "").lower()


# ---------------------------------------------------------------- trading

def fmt_usd(x):
    return f"${x:,.2f}"


_gas = {"eth": None, "ts": 0.0, "alerted": 0.0}


def gas_eth(fresh=False):
    """Wallet ETH, cached 60s. Alerts (once per 10 min) when it can't cover exits."""
    if fresh or time.time() - _gas["ts"] > 60:
        try:
            _gas["eth"] = int(rpc("eth_getBalance", [SIGNER.address, "latest"]), 16) / 1e18
            _gas["ts"] = time.time()
        except Exception:
            pass
    eth = _gas["eth"] if _gas["eth"] is not None else 0.0
    if eth < CFG.get("critical_gas_eth", 0.0008) and time.time() - _gas["alerted"] > 600:
        _gas["alerted"] = time.time()
        log(f"  [ALERT] wallet has only {eth:.5f} ETH — sells will fail until you top up gas "
            f"(a sell costs ~0.0003 ETH; 0.05 ETH covers ~150 txs)")
    return eth


def paper_cash():
    if "paper_cash" not in STATE:
        STATE["paper_cash"] = float(CFG.get("paper_cash_usd", 0))
    return STATE["paper_cash"]


def open_position(tok):
    return STATE["positions"].get(tok.lower())


def recently_closed(tok):
    cutoff = time.time() - CFG.get("reentry_cooldown_hours", 24) * 3600
    return any(c["token"] == tok.lower() and c["closed_at"] > cutoff for c in STATE["closed"])


RECENT_LEADER_BUYS = {}  # tok.lower() -> list of dicts: {"wallet", "label", "ts", "tx", "usd"}


def handle_buy_signal(ev, tok, raw):
    """A watched wallet received `raw` of `tok` in a swap. Copy it if new."""
    meta = token_meta(tok)
    t_detect = time.time()
    sig = {"ts": round(t_detect, 3), "tx": ev["tx"], "block": ev["block"], "wallet": ev["wallet"],
           "label": WALLETS[ev["wallet"]], "token": tok, "symbol": meta["symbol"],
           "amount": raw / 10**meta["decimals"]}

    def skip(why, quiet=False):
        sig["outcome"] = f"skip: {why}"
        append_jsonl("signals.jsonl", sig)
        if not quiet:
            log(f"  [skip] {sig['label']} bought {meta['symbol']}: {why}")

    if open_position(tok):
        return skip("already holding", quiet=True)
    excl = {x.lower() for x in CFG.get("exclude_tokens", [])}
    if tok in excl or meta["symbol"].lower() in excl:
        return skip("excluded by config", quiet=True)
    if CFG.get("skip_stock_tokens", True) and is_stock_token(tok):
        return skip("Robinhood stock token", quiet=True)
    if recently_closed(tok):
        return skip("re-entry cooldown")
    try:
        prev = balance_of(tok, ev["wallet"], hex(ev["block"] - 1))
    except Exception:
        prev = 0  # can't read history: assume new (open-position dedupe still applies)
    if prev > 0:
        return skip(f"wallet already held {prev / 10**meta['decimals']:,.4g}")
    t_origin = block_time(ev["block"])
    age = t_detect - t_origin
    sig["signal_age_s"] = round(age, 2)
    if age > CFG.get("max_signal_age_s", 120):
        return skip(f"signal {age:.0f}s old")
    if len(STATE["positions"]) >= CFG.get("max_positions", 20):
        return skip("max positions")
    info = token_info(tok, fresh=True)
    if not info["price"]:
        return skip("no dexscreener price")
    origin_usd = sig["amount"] * info["price"]
    sig["origin_usd"] = round(origin_usd, 2)
    if origin_usd < CFG.get("min_origin_usd", 0):
        return skip(f"origin buy only {fmt_usd(origin_usd)}")
    if info["liquidity"] < CFG.get("min_liquidity_usd", 0):
        return skip(f"liquidity {fmt_usd(info['liquidity'])} below min")
    if CFG.get("honeypot_check", True):
        hp = honeypot_reason(info)
        if hp:
            return skip(hp)
    min_age_m = float(CFG.get("min_token_age_minutes", 0))
    if min_age_m > 0:
        if info.get("pair_created_at") is None:
            return skip(f"unknown pair launch time (cannot verify age >= {min_age_m:.0f}m)")
        age_m = (time.time() - info["pair_created_at"]) / 60.0
        if age_m < min_age_m:
            return skip(f"token age {age_m:.1f}m < min {min_age_m:.0f}m (fresh launch)")
    if info["liquidity"] < CFG.get("thin_liquidity_usd", 50000):
        # small pool: insist that OTHER people have actually sold recently
        need = CFG.get("thin_min_sells_24h", 5)
        if info["sells24"] < need:
            return skip(f"thin pool ({fmt_usd(info['liquidity'])}) with only {info['sells24']} sells in 24h")
    try:
        legs_buy, legs_sell, desc, depth = discover_route(tok)
    except Exception as e:
        return skip(f"routing: {e}")

    amount_in = int(CFG["buy_usd"] * 10**USDG_DEC)
    try:
        quote, impact, round_trip = quote_check(legs_buy, legs_sell, amount_in)
    except Exception as e:
        return skip(f"quote failed: {e}")
    min_out = int(quote * (1 - SLIPPAGE))
    sig.update(impact=round(impact, 4), round_trip=round(round_trip, 4))
    if impact > CFG.get("max_price_impact_pct", 10) / 100:
        return skip(f"price impact {impact:.1%} for {fmt_usd(CFG['buy_usd'])}")
    if round_trip < -CFG.get("max_round_trip_loss_pct", 15) / 100:
        return skip(f"round trip {round_trip:.1%} (thin pool or tax token)")

    t_route = time.time()  # route discovered + quoted
    t0 = t_route
    lat = None

    # Consensus (multi-leader resonance) check
    consensus_cfg = CFG.get("consensus", {})
    if consensus_cfg.get("enabled", False):
        min_leaders = consensus_cfg.get("min_leaders", 2)
        window_s = consensus_cfg.get("window_seconds", 180)
        min_leader_usd = consensus_cfg.get("min_leader_usd", CFG.get("min_origin_usd", 50))
        now_ts = time.time()

        buys = RECENT_LEADER_BUYS.setdefault(tok.lower(), [])
        if not any(b["tx"] == ev["tx"] and b["wallet"] == ev["wallet"] for b in buys):
            buys.append({
                "wallet": ev["wallet"],
                "label": sig["label"],
                "ts": now_ts,
                "usd": origin_usd,
                "tx": ev["tx"]
            })
        # Prune expired or under-sized buys
        buys = [b for b in buys if (now_ts - b["ts"]) <= window_s and b["usd"] >= min_leader_usd]
        RECENT_LEADER_BUYS[tok.lower()] = buys

        distinct_leaders = {b["wallet"]: b["label"] for b in buys}
        n_leaders = len(distinct_leaders)
        if n_leaders < min_leaders:
            leaders_str = ", ".join(distinct_leaders.values())
            sig["consensus"] = f"{n_leaders}/{min_leaders}"
            return skip(f"consensus pending: {n_leaders}/{min_leaders} leaders ({leaders_str}) in last {window_s}s")
        else:
            leaders_str = ", ".join(distinct_leaders.values())
            log(f"  [consensus] {n_leaders}/{min_leaders} leaders bought {meta['symbol']} ({leaders_str})! Triggering consensus copy!")
            sig["consensus"] = f"{n_leaders}/{min_leaders} (confirmed)"

    if not CFG["live"]:
        if paper_cash() < CFG["buy_usd"]:
            return skip(f"insufficient paper cash ({fmt_usd(paper_cash())})")
        STATE["paper_cash"] = paper_cash() - CFG["buy_usd"]
    if CFG["live"]:
        if gas_eth() < CFG.get("min_gas_eth", 0.003):
            return skip(f"low gas ({gas_eth():.4f} ETH): reserving it for exits — top up ETH")
        if balance_of(USDG, SIGNER.address) < amount_in:
            return skip("insufficient USDG")
        try:
            rec = swap_tx(legs_buy, amount_in, min_out)
        except Exception as e:
            return skip(f"buy tx failed: {e}")
        got = received(rec, tok)
        tx_hash = rec["transactionHash"]
    else:
        got, tx_hash = quote, None

    # THE BUY IS DONE: record it before anything else can fail. (Once, a crash in
    # the bookkeeping below made the same signal re-fire nine times.)
    consensus_buys = RECENT_LEADER_BUYS.get(tok.lower(), [])
    consensus_origins = list(set([b["wallet"] for b in consensus_buys] + [ev["wallet"]]))
    consensus_labels = list(set([b["label"] for b in consensus_buys] + [WALLETS.get(ev["wallet"], "origin")]))

    pos = {"token": tok, "symbol": meta["symbol"], "decimals": meta["decimals"], "sell_simulated": None,
           "origin": ev["wallet"], "origin_label": WALLETS[ev["wallet"]],
           "origins": consensus_origins, "origin_labels": consensus_labels,
           "signal_tx": ev["tx"],
           "bought_at": time.time(), "buy_usd": CFG["buy_usd"], "buy_tx": tx_hash,
           "initial_raw": got, "remaining_raw": got, "legs_sell": legs_sell, "route": desc,
           "stages_done": [], "origin_exiting": False, "origin_done": False,
           "usdg_out": 0.0, "sells": [], "retry_after": 0, "paper": not CFG["live"],
           "signal_age_s": sig["signal_age_s"], "latency": None,
           "peak_ret_pct": 0.0, "breakeven_active": False, "_last_price_check": 0.0}
    STATE["positions"][tok] = pos
    save_state()

    if CFG["live"]:
        try:
            fill_block = int(rec["blockNumber"], 16)
            t_mined = block_time(fill_block)
            sent = SIGNER.last_sent_ts or t_route
            lat = {"origin_ts": t_origin, "detect_ts": round(t_detect, 3), "route_ts": round(t_route, 3),
                   "sent_ts": round(sent, 3), "mined_ts": t_mined,
                   "origin_block": ev["block"], "fill_block": fill_block,
                   "detect": round(t_detect - t_origin, 2), "route": round(t_route - t_detect, 2),
                   "send": round(sent - t_route, 2), "mine": round(max(0.0, t_mined - sent), 2),
                   "total": round(t_mined - t_origin, 2), "blocks_behind": fill_block - ev["block"]}
            sig["latency"] = pos["latency"] = lat
        except Exception as e:
            log(f"  [warn] latency bookkeeping failed for {meta['symbol']}: {str(e)[:100]}")
        try:
            ensure_allowance(tok, MAX_UINT)  # pre-approve so exits are a single tx
        except Exception as e:
            log(f"  [warn] pre-approve failed for {meta['symbol']}: {e}")
        pos["sell_simulated"] = sell_simulates(tok, legs_sell, got)
        if not pos["sell_simulated"]:
            log(f"  [ALERT] {meta['symbol']}: a sell of what we just bought does NOT simulate — "
                f"possible honeypot; exits will keep retrying")
        save_state()
    sig["outcome"] = "bought"
    append_jsonl("signals.jsonl", sig)
    append_jsonl("trades.jsonl", {"ts": time.time(), "side": "buy", "token": tok, "symbol": meta["symbol"],
                                  "usd": CFG["buy_usd"], "raw": got, "tx": tx_hash, "paper": pos["paper"],
                                  "origin": sig["label"], "route": desc})
    if lat:
        lat_s = (f"latency {lat['total']:.1f}s = detect {lat['detect']:.1f} + route {lat['route']:.1f} "
                 f"+ send {lat['send']:.1f} + mine {lat['mine']:.1f} ({lat['blocks_behind']} blocks behind)")
    else:
        lat_s = f"signal age {age:.1f}s, route {t_route - t_detect:.1f}s"
    log(f"  [BUY{'' if CFG['live'] else ' paper'}] {meta['symbol']} {fmt_usd(CFG['buy_usd'])} "
        f"<- {sig['label']} bought {fmt_usd(origin_usd)} | {desc} | {lat_s}")


def handle_sell_event(ev, tok):
    pos = open_position(tok)
    if not pos or pos.get("origin_exiting"):
        return
    is_origin = (pos.get("origin") == ev["wallet"]) or (ev["wallet"] in pos.get("origins", []))
    if is_origin:
        pos["origin_exiting"] = True
        save_state()
        seller = WALLETS.get(ev["wallet"], pos.get("origin_label", "origin"))
        log(f"  [origin exit] {seller} is selling {pos['symbol']} -> releasing final tranche")


_done_signals = {}  # (tx, wallet) -> ts; belt-and-braces against re-processing a window


def process_events(events):
    now = time.time()
    for ev in events:
        key = (ev["tx"], ev["wallet"])
        if key in _done_signals:
            continue
        _done_signals[key] = now
        try:
            process_event(ev)
        except Exception as e:
            log(f"  [warn] signal {ev['tx'][:12]}.. from {WALLETS.get(ev['wallet'], '?')} failed: {str(e)[:160]}")
    if len(_done_signals) > 5000:
        for k in [k for k, t in _done_signals.items() if now - t > 3600]:
            _done_signals.pop(k, None)


def process_event(ev):
    buys = [t for t in ev["ins"] if not is_funding(t)]
    sells = [t for t in ev["outs"] if not is_funding(t)]
    two_sided = bool(ev["ins"]) and bool(ev["outs"])
    for t in buys:
        # a one-sided inbound from a plain wallet is an airdrop/transfer, not a fill
        if not two_sided and not any(is_contract(c) for c in ev["cp_in"]):
            continue
        handle_buy_signal(ev, t, ev["ins"][t])
    for t in sells:
        if two_sided or any(is_contract(c) for c in ev["cp_out"]):
            handle_sell_event(ev, t)


def sell(pos, raw, why):
    tok = pos["token"]
    if CFG["live"] and why == "origin exit":
        raw = balance_of(tok, SIGNER.address)  # sweep everything incl. dust
    raw = min(raw, pos["remaining_raw"]) if not (CFG["live"] and why == "origin exit") else raw
    if raw <= 0:
        return True
    legs, quote = best_sell_legs(pos, raw)
    base = CFG.get("sell_slippage_pct", 6) / 100
    bound = min(base * 1.5 ** pos.get("sell_failures", 0), CFG.get("sell_slippage_max_pct", 25) / 100)
    if pos.get("sell_failures"):
        log(f"  [sell] {pos['symbol']}: widening slippage bound to {bound:.0%} after {pos['sell_failures']} failure(s)")
    min_out = int(quote * (1 - bound))
    if CFG["live"]:
        ensure_allowance(tok, raw)
        try:
            rec = swap_tx(legs, raw, min_out)
        except TxPending as e:
            pos["pending_tx"] = {"hash": e.tx_hash, "raw": raw, "why": why, "ts": time.time()}
            save_state()
            log(f"  [pending] {pos['symbol']} sell tx {e.tx_hash[:12]}.. broadcast, receipt not seen yet; "
                f"will reconcile")
            raise
        got, tx_hash = received(rec, USDG), rec["transactionHash"]
    else:
        got, tx_hash = quote, None
    usd = got / 10**USDG_DEC
    if not CFG["live"]:
        STATE["paper_cash"] = paper_cash() + usd
    pos["sell_failures"] = 0
    pos["retry_after"] = 0
    record_sale(pos, raw, usd, why, tx_hash)
    log(f"  [SELL{'' if CFG['live'] else ' paper'}] {pos['symbol']} {why}: "
        f"{raw / 10**pos['decimals']:,.4g} -> {fmt_usd(usd)}")
    return True


def best_sell_legs(pos, raw):
    """Quote the route stored at buy time and a fresh discovery; sells are not
    latency-critical, so take whichever pays more (a deeper pool may have
    appeared, or the buy went through a thin one)."""
    best = None
    try:
        best = (pos["legs_sell"], quote_route(pos["legs_sell"], raw))
    except Exception:
        pass
    try:
        _lb, ls, desc, _d = discover_route(pos["token"])
        q = quote_route(ls, raw)
        if best is None or q > best[1]:
            if best is not None:
                log(f"  [route] {pos['symbol']} sell via fresh route ({desc}) pays "
                    f"{q / best[1] - 1:+.1%} more than the stored one")
            best = (ls, q)
    except Exception:
        pass
    if best is None:
        raise RuntimeError("no sell route quotes")
    return best


CMD_DIR = DATA / "commands"


def process_commands():
    """Execute requests dropped by the dashboard (data/commands/*.json):
    {"action": "sell", "token": "0x..", "pct": 100}. The bot is the only
    process that signs and the only writer of state.json, so the dashboard
    never needs the key and nothing races."""
    if not CMD_DIR.exists():
        return
    for f in sorted(CMD_DIR.glob("*.json")):
        try:
            cmd = json.loads(f.read_text())
        except ValueError:
            f.unlink()
            continue
        f.unlink()
        result = {"ts": time.time(), "cmd": cmd}
        try:
            if cmd.get("action") == "book_sale":
                book_sale(cmd["token"], cmd["tx"], cmd.get("why", "recovered"))
                result["ok"] = True
                append_jsonl("commands_done.jsonl", result)
                continue
            if cmd.get("action") == "adopt":
                adopt_position(cmd["token"], float(cmd["usd"]), cmd.get("bought_at"))
                result["ok"] = True
                append_jsonl("commands_done.jsonl", result)
                continue
            if cmd.get("action") != "sell":
                raise ValueError(f"unknown action {cmd.get('action')!r}")
            pos = open_position(cmd["token"])
            if not pos:
                raise ValueError("no open position")
            pct = int(cmd.get("pct", 100))
            if pos["remaining_raw"] <= pos["initial_raw"] * 0.001:
                raise ValueError("nothing left to sell (already fully exited; closing)")
            raw = pos["remaining_raw"] if pct >= 100 else int(pos["initial_raw"] * pct / 100)
            log(f"  [dashboard] sell {pct}% of {pos['symbol']} requested")
            sell(pos, raw, "origin exit" if pct >= 100 else f"manual {pct}%")
            if pct >= 100:
                pos["origin_done"] = True
                for i in range(len(CFG["exits"])):
                    if i not in pos["stages_done"]:
                        pos["stages_done"].append(i)
            save_state()
            result["ok"] = True
        except Exception as e:
            result.update(ok=False, error=str(e)[:300])
            log(f"  [dashboard] sell failed: {e}")
        append_jsonl("commands_done.jsonl", result)


def stage_seconds(st):
    return st["after_minutes"] * 60 if "after_minutes" in st else st["after_hours"] * 3600


def stage_label(st):
    return f"+{st['after_minutes']:g}m" if "after_minutes" in st else f"+{st['after_hours']:g}h"


def record_sale(pos, raw, usd, why, tx_hash):
    pos["remaining_raw"] = max(0, pos["remaining_raw"] - raw)
    pos["usdg_out"] += usd
    pos["sells"].append({"ts": time.time(), "why": why, "raw": raw, "usd": usd, "tx": tx_hash})
    append_jsonl("trades.jsonl", {"ts": time.time(), "side": "sell", "token": pos["token"], "symbol": pos["symbol"],
                                  "usd": usd, "raw": raw, "tx": tx_hash, "paper": pos["paper"], "why": why})


def mark_stage_for(pos, why):
    """Keep stage bookkeeping consistent when a sale is recorded after the fact."""
    for i, st in enumerate(CFG["exits"]):
        if why.startswith(stage_label(st) + " ") and i not in pos["stages_done"]:
            pos["stages_done"].append(i)
    if why == "origin exit":
        pos["origin_done"] = True


def reconcile_position(pos):
    """Live only. (1) Settle a sell whose receipt we missed. (2) If the wallet
    holds less than the position says and no sale explains it, find the
    proceeds on-chain (USDG into our wallet in a tx that moved this token out)
    and record them; otherwise just sync the amount so exits use real numbers."""
    tok = pos["token"]
    pend = pos.get("pending_tx")
    if pend:
        rec = None
        try:
            rec = rpc("eth_getTransactionReceipt", [pend["hash"]], retries=1)
        except Exception:
            pass
        if rec:
            pos["pending_tx"] = None
            if int(rec["status"], 16) == 1:
                usd = received(rec, USDG) / 10**USDG_DEC
                record_sale(pos, pend["raw"], usd, pend["why"], rec["transactionHash"])
                mark_stage_for(pos, pend["why"])
                log(f"  [reconciled] {pos['symbol']} {pend['why']} confirmed: {fmt_usd(usd)}")
            else:
                log(f"  [reconciled] {pos['symbol']} pending sell reverted; will retry")
            save_state()
        elif time.time() - pend["ts"] > 600:
            pos["pending_tx"] = None  # dropped from the mempool
            save_state()
        return
    bal = balance_of(tok, SIGNER.address)
    if bal >= pos["remaining_raw"] * 0.99:
        return  # fine
    # unexplained shortfall (possibly the whole bag): look for our own sale on-chain
    missing = pos["remaining_raw"] - bal
    found = find_unrecorded_sale(pos, {s["tx"] for s in pos["sells"] if s.get("tx")})
    if found:
        usd, tx_hash, why = found
        record_sale(pos, missing, usd, why, tx_hash)
        mark_stage_for(pos, why)
        log(f"  [reconciled] {pos['symbol']}: found unrecorded sale {tx_hash[:12]}.. for {fmt_usd(usd)}; "
            f"position synced to wallet ({bal / 10**pos['decimals']:,.4g})")
    else:
        if bal == 0:
            return  # nothing found: the external-sale check will close it as sold outside the bot
        pos["remaining_raw"] = bal
        log(f"  [reconciled] {pos['symbol']}: wallet holds {bal / 10**pos['decimals']:,.4g}, less than recorded; "
            f"synced (proceeds unknown, pnl will understate)")
    save_state()


def find_unrecorded_sale(pos, known_txs):
    """USDG Transfer logs into our wallet (address-indexed, cheap on the public
    RPC) -> the most recent tx that also moved pos.token OUT of our wallet."""
    me = pad(SIGNER.address)
    flt = {"fromBlock": "0x0", "toBlock": "latest", "address": USDG, "topics": [TRANSFER_TOPIC, None, me]}
    logs = None
    for url in dict.fromkeys([RPC_URL, CFG.get("fallback_rpc") or CFG["rpc"]]):
        try:
            logs = rpc("eth_getLogs", [flt], retries=1, url=url)
            break
        except Exception:
            continue
    if not logs:
        return None
    for lg in sorted(logs, key=lambda l: -int(l["blockNumber"], 16))[:15]:
        tx = lg["transactionHash"]
        if tx in known_txs:
            continue
        try:
            rec = rpc("eth_getTransactionReceipt", [tx])
        except Exception:
            continue
        if not rec:
            continue
        moved_token_out = any(l["address"].lower() == pos["token"] and len(l["topics"]) == 3
                              and l["topics"][1] == me for l in rec["logs"])
        if moved_token_out and int(rec["blockNumber"], 16) > 0:
            usd = received(rec, USDG) / 10**USDG_DEC
            elapsed = time.time() - pos["bought_at"]
            why = "recovered"
            for i, st in enumerate(CFG["exits"]):
                if i not in pos["stages_done"] and elapsed >= stage_seconds(st):
                    why = f"{stage_label(st)} {st['pct']}% (recovered)"
                    break
            return usd, tx, why
    return None


def write_off_if_dead(pos, now):
    """A pool whose liquidity was pulled quotes nothing forever. Once the token
    has shown zero liquidity for 30+ minutes and the bag is unsellable, close it
    as a loss so the exit loop stops retrying it every hour."""
    try:
        info = token_info(pos["token"], fresh=True)
    except Exception:
        return False
    if (info.get("liquidity") or 0) > 0:
        return False
    pos["dead_since"] = pos.get("dead_since") or pos.get("bought_at", now)
    if now - pos["dead_since"] < 1800:
        return False
    pos.update(closed_at=now, pnl_usd=pos["usdg_out"] - pos["buy_usd"], note="pool drained (rugged); written off")
    STATE["closed"].append(pos)
    STATE["positions"].pop(pos["token"], None)
    save_state()
    log(f"  [closed] {pos['symbol']}: pool drained, no liquidity left — written off at {fmt_usd(pos['pnl_usd'])}")
    return True


def close_if_done(pos, now):
    """Move a position to closed once nothing is left to sell (all tranches
    done, rounding dust, or sold out via origin exit / dashboard)."""
    all_timed = len(pos["stages_done"]) == len(CFG["exits"])
    dust = pos["remaining_raw"] <= pos["initial_raw"] * 0.001
    if not (pos["remaining_raw"] <= 0 or dust or (all_timed and pos["origin_done"])):
        return False
    if pos.get("pending_tx"):
        return False
    pos["closed_at"] = now
    pos["pnl_usd"] = pos["usdg_out"] - pos["buy_usd"]
    STATE["closed"].append(pos)
    STATE["positions"].pop(pos["token"], None)
    save_state()
    log(f"  [closed] {pos['symbol']} pnl {fmt_usd(pos['pnl_usd'])}")
    return True


def book_sale(token, tx_hash, why="recovered"):
    """Record a sale that happened on-chain but was never booked (open or
    closed position), from its receipt: USDG received and tokens moved out."""
    tok = token.lower()
    rec = rpc("eth_getTransactionReceipt", [tx_hash])
    if not rec or int(rec["status"], 16) != 1:
        raise ValueError("tx not found or reverted")
    me = SIGNER.address.lower()
    usd = received(rec, USDG) / 10**USDG_DEC
    raw = sum(int(l["data"], 16) for l in rec["logs"] if l["address"].lower() == tok and len(l["topics"]) == 3
              and l["topics"][0] == TRANSFER_TOPIC and "0x" + l["topics"][1][-40:] == me)
    pos = open_position(tok) or next((c for c in reversed(STATE["closed"]) if c["token"] == tok), None)
    if pos is None:
        raise ValueError("no position for that token")
    if any(x.get("tx") == tx_hash for x in pos["sells"]):
        raise ValueError("already booked")
    record_sale(pos, min(raw, pos["remaining_raw"]) if pos["remaining_raw"] else 0, usd, why, tx_hash)
    if "closed_at" in pos:
        pos["pnl_usd"] = pos["usdg_out"] - pos["buy_usd"]
        pos["note"] = (pos.get("note") or "") + f"; booked {tx_hash[:10]} later"
    save_state()
    log(f"  [booked] {pos['symbol']} sale {tx_hash[:12]}.. {fmt_usd(usd)} ({why}); pnl now {fmt_usd(pos['usdg_out'] - pos['buy_usd'])}")


def adopt_position(token, usd, bought_at=None):
    """Register tokens the wallet holds but the bot never recorded (e.g. after a
    crash between fill and bookkeeping) so the normal exits handle them."""
    tok = token.lower()
    if open_position(tok):
        raise ValueError("already an open position")
    meta = token_meta(tok)
    bal = balance_of(tok, SIGNER.address)
    if bal == 0:
        raise ValueError("wallet holds none of it")
    _lb, legs_sell, desc, _d = discover_route(tok)
    pos = {"token": tok, "symbol": meta["symbol"], "decimals": meta["decimals"], "sell_simulated": None,
           "origin": "", "origin_label": "adopted", "signal_tx": None,
           "bought_at": float(bought_at or time.time()), "buy_usd": usd, "buy_tx": None,
           "initial_raw": bal, "remaining_raw": bal, "legs_sell": legs_sell, "route": desc,
           "stages_done": [], "origin_exiting": False, "origin_done": False,
           "usdg_out": 0.0, "sells": [], "retry_after": 0, "paper": False, "signal_age_s": None,
           "latency": None, "note": "adopted from wallet",
           "peak_ret_pct": 0.0, "breakeven_active": False, "_last_price_check": 0.0}
    STATE["positions"][tok] = pos
    save_state()
    log(f"  [adopted] {meta['symbol']}: {bal / 10**meta['decimals']:,.4g} tokens, cost basis {fmt_usd(usd)}, "
        f"exits run on the normal schedule")


def check_position_risk(pos, now):
    """Monitor live floating return and execute risk controls:
    1. Hard Stop-Loss: Exit if floating return <= hard_stop_loss_pct (default -45%).
    2. Break-Even Stop: Once peak profit reaches >= breakeven_trigger_pct (default +50%),
       lift stop line to breakeven_stop_pct (default 0.0%). If price drops back, exit.
    """
    rc = CFG.get("risk_control", {})
    if not rc.get("enabled", True):
        return False
    if pos["remaining_raw"] <= pos["initial_raw"] * 0.001:
        return False

    interval = float(rc.get("price_check_interval", 3.0))
    if now - pos.get("_last_price_check", 0) < interval:
        return False
    pos["_last_price_check"] = now

    # Real-time quote in USDG
    quote = None
    try:
        quote = quote_route(pos["legs_sell"], pos["remaining_raw"])
    except Exception:
        try:
            legs, quote = best_sell_legs(pos, pos["remaining_raw"])
            pos["legs_sell"] = legs
        except Exception as e:
            if write_off_if_dead(pos, now):
                return True
            return False

    if quote is None or quote <= 0:
        return False

    cur_usd = quote / 10**USDG_DEC
    cost_usd = pos["buy_usd"] * (pos["remaining_raw"] / pos["initial_raw"]) if pos.get("initial_raw") else pos["buy_usd"]
    if cost_usd <= 0:
        return False

    ret_pct = (cur_usd / cost_usd - 1.0) * 100.0

    # Dynamic peak profit tracking
    be_trigger = float(rc.get("breakeven_trigger_pct", 50.0))
    be_stop = float(rc.get("breakeven_stop_pct", 0.0))
    if "peak_ret_pct" not in pos or ret_pct > pos.get("peak_ret_pct", -100.0):
        pos["peak_ret_pct"] = round(ret_pct, 1)
        if pos["peak_ret_pct"] >= be_trigger and not pos.get("breakeven_active"):
            pos["breakeven_active"] = True
            log(f"  [risk] {pos['symbol']} peak profit hit {pos['peak_ret_pct']:+.1f}% >= +{be_trigger:.0f}%: "
                f"BREAK-EVEN STOP ACTIVATED at {be_stop:+.1f}%")
            save_state()

    # Rule 1: Break-even stop (if armed)
    if pos.get("breakeven_active") and ret_pct <= be_stop:
        log(f"  [risk] {pos['symbol']} TRIGGER BREAK-EVEN STOP: cur {ret_pct:+.1f}% <= {be_stop:+.1f}% "
            f"(peak was {pos.get('peak_ret_pct', 0.0):+.1f}%)")
        sell(pos, pos["remaining_raw"], f"breakeven stop (peak {pos.get('peak_ret_pct', 0.0):+.1f}%)")
        pos["origin_done"] = True
        save_state()
        return True

    # Rule 2: Hard stop-loss (if not under break-even protection)
    hard_sl = float(rc.get("hard_stop_loss_pct", -45.0))
    if not pos.get("breakeven_active") and ret_pct <= hard_sl:
        log(f"  [risk] {pos['symbol']} TRIGGER HARD STOP-LOSS: cur {ret_pct:+.1f}% <= {hard_sl:+.1f}% "
            f"(basis {fmt_usd(cost_usd)}, cur {fmt_usd(cur_usd)})")
        sell(pos, pos["remaining_raw"], f"hard stop loss ({ret_pct:+.1f}%)")
        pos["origin_done"] = True
        save_state()
        return True

    return False


def run_exits():
    now = time.time()
    for tok, pos in list(STATE["positions"].items()):
        if close_if_done(pos, now):
            continue
        # reconcile BEFORE any retry back-off: a "failed" sell may in fact have
        # gone through (lost response), and that must be booked promptly
        if CFG["live"] and now - pos.get("_reconciled_at", 0) > 10:
            pos["_reconciled_at"] = now
            try:
                reconcile_position(pos)
            except Exception as e:
                log(f"  [warn] reconcile {pos['symbol']}: {str(e)[:120]}")
            if close_if_done(pos, now):
                continue
        if now < pos.get("retry_after", 0):
            continue
        elapsed = now - pos["bought_at"]
        try:
            if CFG["live"]:
                if pos.get("pending_tx"):
                    continue  # don't send another sell while one is unresolved
            if CFG["live"] and pos["remaining_raw"] > 0 and balance_of(tok, SIGNER.address) <= pos["initial_raw"] * 0.001:
                log(f"  [closed] {pos['symbol']}: wallet no longer holds it (sold outside the bot)")
                pos.update(closed_at=now, remaining_raw=0, pnl_usd=pos["usdg_out"] - pos["buy_usd"],
                           note="sold externally")
                STATE["closed"].append(pos)
                del STATE["positions"][tok]
                save_state()
                continue
            if check_position_risk(pos, now):
                close_if_done(pos, now)
                continue
            for i, st in enumerate(CFG["exits"]):
                if i in pos["stages_done"] or elapsed < stage_seconds(st):
                    continue
                sell(pos, int(pos["initial_raw"] * st["pct"] / 100), f"{stage_label(st)} {st['pct']}%")
                pos["stages_done"].append(i)
                save_state()
            if pos["origin_exiting"] and not pos["origin_done"]:
                sell(pos, pos["remaining_raw"], "origin exit")
                pos["origin_done"] = True
                save_state()
            # every timed stage is done and the schedule adds up to a full exit, yet
            # something is left (e.g. the schedule changed while the position was open):
            # finish the job instead of waiting on the origin wallet
            if (len(pos["stages_done"]) == len(CFG["exits"]) and sum(st["pct"] for st in CFG["exits"]) >= 100
                    and pos["remaining_raw"] > pos["initial_raw"] * 0.001 and not pos["origin_done"]):
                sell(pos, pos["remaining_raw"], "remainder")
                pos["origin_done"] = True
                save_state()
        except TxPending:
            continue
        except Exception as e:
            pos["sell_failures"] = pos.get("sell_failures", 0) + 1
            msg = str(e)
            if "slippage" in msg or "reverted" in msg:
                # price moving fast: re-quote quickly with a wider bound (see sell()); never park a live position
                wait = 5 if pos["sell_failures"] <= 3 else 60
            elif "no sell route" in msg or "quote reverted" in msg:
                if write_off_if_dead(pos, now):
                    continue
                wait = min(CFG.get("sell_retry_seconds", 300) * 2 ** (pos["sell_failures"] - 1), 3600)
            else:
                wait = min(CFG.get("sell_retry_seconds", 300) * 2 ** (pos["sell_failures"] - 1), 3600)
            pos["retry_after"] = now + wait
            save_state()
            log(f"  [warn] sell {pos['symbol']} failed ({pos['sell_failures']}x), retry in {wait // 60}m: {str(e)[:160]}")
            continue
        close_if_done(pos, now)


# ---------------------------------------------------------------- poll tick

_wl_pad = None


def poll_once(cursor, url=None):
    """Scan blocks cursor+1..head in fixed windows (Alchemy free tier allows 10
    blocks per eth_getLogs; the chain makes ~10 blocks/s). Each window is one
    batched HTTP request; STATE["last_block"] advances after each window, so a
    failure mid-scan resumes exactly where it stopped and nothing is skipped.
    `url` forces a specific RPC (the websocket backstop sweeps the public one)."""
    global _wl_pad
    if _wl_pad is None:
        _wl_pad = [pad(a) for a in WALLETS]
    last = cursor
    # scan to one block behind the reported head: a load-balanced RPC can answer
    # the log query from a node that has not seen the newest block yet
    head = int(rpc("eth_blockNumber", [], url=url), 16) - 1
    # Alchemy's free/PAYG tiers cap eth_getLogs at 10 blocks; the public RPC and most
    # other providers don't, so scan wider windows there (fewer requests, less lag)
    chunk = CFG.get("log_chunk_blocks", 10) if "alchemy" in (url or rpc_url()) else CFG.get("log_chunk_blocks_open", 50)
    max_lag = CFG.get("max_catchup_blocks", 600)
    if head - last > max_lag:
        # after a stall the old blocks are past max_signal_age anyway: jump
        log(f"  [watch] {head - last} blocks behind, skipping ahead to the last {max_lag}")
        last = head - max_lag
    while last < head:
        to = min(last + chunk, head)
        rng = {"fromBlock": hex(last + 1), "toBlock": hex(to)}
        l1, l2 = rpc_batch([
            ("eth_getLogs", [{**rng, "topics": [TRANSFER_TOPIC, _wl_pad]}]),
            ("eth_getLogs", [{**rng, "topics": [TRANSFER_TOPIC, None, _wl_pad]}])], url=url)
        uniq = {(lg["transactionHash"], lg["logIndex"]): lg for lg in l1 + l2 if len(lg["topics"]) == 3}
        if uniq:
            process_events(wallet_events(list(uniq.values())))
        last = to
        STATE["last_block"] = last
    return last


# ---------------------------------------------------------------- websocket detection

class WsFeed(threading.Thread):
    """Subscribes to Transfer logs touching the watched wallets over a websocket
    (Alchemy: wss://<network>.g.alchemy.com/v2/<key>). Logs land in `queue`
    with their arrival time; the main loop groups them per tx after a short
    grace period. Reconnects forever; `healthy()` says whether the main loop
    may rely on it."""

    def __init__(self, url):
        super().__init__(daemon=True)
        self.url = url
        self.queue = []
        self.lock = threading.Lock()
        self.connected = False
        self.last_msg = 0.0
        self.last_block = 0
        self.subs = 0
        self.stop = threading.Event()

    def healthy(self):
        return self.connected and self.subs >= 2 and time.time() - self.last_msg < CFG.get("ws_stale_seconds", 90)

    def run(self):
        import websocket  # websocket-client
        wl = [pad(a) for a in WALLETS]
        backoff = 1
        while not self.stop.is_set():
            try:
                ws = websocket.create_connection(self.url, timeout=20, suppress_origin=True)
                self.connected = True
                self.subs = 0
                for i, topics in enumerate(([TRANSFER_TOPIC, wl], [TRANSFER_TOPIC, None, wl])):
                    ws.send(json.dumps({"jsonrpc": "2.0", "id": i + 1, "method": "eth_subscribe",
                                        "params": ["logs", {"topics": topics}]}))
                ws.settimeout(30)
                last_ping = time.time()
                self.last_msg = time.time()
                while not self.stop.is_set():
                    try:
                        msg = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        if time.time() - last_ping > 25:
                            ws.ping()
                            last_ping = time.time()
                        continue
                    if not msg:
                        continue
                    self.last_msg = time.time()
                    m = json.loads(msg)
                    if "id" in m and m.get("result") and not m.get("method"):
                        self.subs += 1
                        if self.subs == 2:
                            log(f"  [ws] subscribed to Transfer logs for {len(WALLETS)} wallets")
                            backoff = 1
                        continue
                    if m.get("error"):
                        raise RuntimeError(f"subscription error: {m['error']}")
                    if m.get("method") == "eth_subscription":
                        lg = m["params"]["result"]
                        if lg.get("removed"):
                            continue
                        with self.lock:
                            self.queue.append((time.time(), lg))
                        self.last_block = max(self.last_block, int(lg["blockNumber"], 16))
            except Exception as e:
                self.connected = False
                self.subs = 0
                log(f"  [ws] disconnected ({str(e)[:100]}); reconnecting in {backoff}s (polling covers the gap)")
                append_jsonl("rpc_events.jsonl", {"ts": time.time(), "event": "ws_disconnect", "error": str(e)[:200]})
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                try:
                    ws.close()
                except Exception:
                    pass

    def drain(self, grace=0.2):
        """Logs that arrived at least `grace` seconds ago (so a tx's several
        Transfer logs are grouped), oldest first."""
        cutoff = time.time() - grace
        with self.lock:
            ready = [lg for ts, lg in self.queue if ts <= cutoff]
            self.queue = [(ts, lg) for ts, lg in self.queue if ts > cutoff]
        return ready


_ws_lag = {"ts": 0.0}


def ws_url_for(rpc_http):
    explicit = ENV.get("WS_URL") or CFG.get("ws_url")
    if explicit:
        return explicit
    if "alchemy.com" in rpc_http:
        return rpc_http.replace("https://", "wss://", 1)
    return None


# ---------------------------------------------------------------- commands

def cmd_run():
    global SIGNER
    live = CFG["live"]
    if live:
        if not ROUTER:
            sys.exit("config.router is empty: deploy contracts/src/CopyRouter.sol first (see README)")
        key = ENV.get("PRIVATE_KEY")
        if not key:
            sys.exit("PRIVATE_KEY missing from .env")
        SIGNER = Signer(key)
        while True:
            try:
                eth = int(rpc("eth_getBalance", [SIGNER.address, "latest"]), 16) / 1e18
                usdg = balance_of(USDG, SIGNER.address) / 10**USDG_DEC
                break
            except Exception as e:
                log(f"  [warn] startup RPC check failed, retrying in 5s: {str(e)[:120]}")
                time.sleep(5)
        log(f"LIVE wallet {SIGNER.address}: {usdg:,.2f} USDG, {eth:.5f} ETH gas "
            f"(~{int(eth / 0.0003)} txs at today's prices)")
        if eth < CFG.get("min_gas_eth", 0.003):
            log(f"  [ALERT] gas below the {CFG.get('min_gas_eth', 0.003)} ETH reserve: no NEW buys until topped up; "
                f"exits will still be attempted")
        # positions simulated in paper mode were never bought: archive them so
        # the live loop doesn't try to sell tokens the wallet doesn't hold
        paper = [t for t, pos in STATE["positions"].items() if pos.get("paper")]
        for t in paper:
            pos = STATE["positions"].pop(t)
            pos.update(closed_at=time.time(), pnl_usd=0.0, note="paper position archived at live start")
            STATE["closed"].append(pos)
        if paper:
            save_state()
            log(f"archived {len(paper)} paper position(s) from the dry run")
        while True:
            try:
                ensure_allowance(USDG, MAX_UINT)
                break
            except Exception as e:
                log(f"  [warn] allowance check failed, retrying in 5s: {str(e)[:120]}")
                time.sleep(5)
    else:
        log(f"PAPER mode (config.live = false): simulated fills at on-chain quotes, nothing is sent"
            + (f"; bankroll {fmt_usd(paper_cash())}" if CFG.get("paper_cash_usd") else ""))

    while True:
        try:
            last = int(rpc("eth_blockNumber", []), 16)
            break
        except Exception as e:
            log(f"  [warn] can't read the chain head yet, retrying in 5s: {str(e)[:120]}")
            time.sleep(5)
    STATE["last_block"] = last
    log(f"watching {len(WALLETS)} wallets on Robinhood Chain from block {last}, "
        f"buy {fmt_usd(CFG['buy_usd'])}/signal, exits {CFG['exits']} + remainder on origin exit")
    poll = CFG.get("poll_seconds", 1)
    throttled, last_throttle_log = 0, 0.0
    feed = None
    if CFG.get("ws", False):
        wsu = ws_url_for(RPC_URL)
        if wsu:
            feed = WsFeed(wsu)
            feed.start()
            log(f"websocket detection ON ({wsu.split('/v2/')[0]}...), backstop sweep every "
                f"{CFG.get('ws_backstop_seconds', 20)}s on {FALLBACK_RPC.split('//')[1]}")
        else:
            log("websocket detection requested but no wss URL (set WS_URL or use an Alchemy RPC_URL); polling")
    last_sweep = time.time()
    last_exits = 0.0
    last_beat = time.time()
    while True:
        # heartbeat: proves the loop is alive and shows how far behind the chain we are
        if time.time() - last_beat >= CFG.get("heartbeat_seconds", 120):
            last_beat = time.time()
            try:
                head = int(rpc("eth_blockNumber", []), 16)
                mode = "ws" if (feed is not None and feed.healthy()) else ("alchemy" if rpc_url() == RPC_URL else "public-rpc")
                log(f"  [beat] {head - STATE['last_block']} blocks behind head via {mode}; "
                    f"{len(STATE['positions'])} open; {'gas %.4f ETH' % gas_eth() if CFG['live'] else 'paper'}")
            except Exception as e:
                log(f"  [beat] head check failed: {str(e)[:80]}")
        try:
            if feed is not None and feed.healthy():
                # fast path: events straight off the socket, grouped per tx
                time.sleep(0.1)
                logs = feed.drain()
                if logs:
                    newest = max(int(lg["blockNumber"], 16) for lg in logs)
                    try:  # how far behind the block's own timestamp did the socket deliver? (sampled)
                        if time.time() - _ws_lag["ts"] > 30:
                            _ws_lag["ts"] = time.time()
                            append_jsonl("ws_lag.jsonl", {"ts": time.time(), "block": newest,
                                                          "lag_s": round(time.time() - 0.2 - block_time(newest), 2)})
                    except Exception:
                        pass
                    uniq = {(lg["transactionHash"], lg["logIndex"]): lg for lg in logs if len(lg["topics"]) == 3}
                    process_events(wallet_events(list(uniq.values())))
                    STATE["last_block"] = max(STATE["last_block"], newest - 1)
                # backstop: sweep everything since the cursor on the free public RPC
                if time.time() - last_sweep >= CFG.get("ws_backstop_seconds", 20):
                    last_sweep = time.time()
                    poll_once(STATE["last_block"], url=FALLBACK_RPC)
                if time.time() - last_exits >= 1.0:
                    last_exits = time.time()
                    process_commands()
                    run_exits()
                continue
            time.sleep(poll if rpc_url() == RPC_URL else max(poll, CFG.get("fallback_poll_seconds", 3.0)))
            check_wallets_reload()
            check_config_reload()
            last = poll_once(STATE["last_block"])
            process_commands()
            run_exits()
        except KeyboardInterrupt:
            save_state()
            raise
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", 0) in (401, 403) and RPC_URL != FALLBACK_RPC:
                continue  # primary rejected the key; failover accounting already logged it
            if getattr(e.response, "status_code", 0) == 429:
                # public RPC throttling: nothing is lost (cursor did not advance);
                # ease off for a tick and only mention it once a minute
                throttled += 1
                if time.time() - last_throttle_log > 60:
                    log(f"  [rpc] throttled x{throttled} in the last minute (HTTP 429) — nothing skipped; "
                        f"raise poll_seconds if this keeps happening")
                    last_throttle_log, throttled = time.time(), 0
                time.sleep(1)
            else:
                log(f"  [warn] poll failed, retrying: {e}")
        except Exception as e:
            if "beyond current head" not in str(e):  # node lag: silently retry next tick
                log(f"  [warn] poll failed, retrying: {e}")


def cmd_status():
    total = 0.0
    print(f"Open positions ({len(STATE['positions'])}):")
    for pos in STATE["positions"].values():
        px = token_info(pos["token"])["price"] or 0
        held = pos["remaining_raw"] / 10**pos["decimals"]
        value = held * px
        pnl = value + pos["usdg_out"] - pos["buy_usd"]
        total += pnl
        age_h = (time.time() - pos["bought_at"]) / 3600
        flags = ""
        if pos.get("sell_simulated") is False:
            flags += " SELL-SIM-FAILED"
        if pos.get("breakeven_active"):
            flags += f" [BREAKEVEN-ARMED: peak {pos.get('peak_ret_pct', 0.0):+.0f}%]"
        elif pos.get("peak_ret_pct") is not None and pos.get("peak_ret_pct") > 0:
            flags += f" [peak {pos['peak_ret_pct']:+.0f}%]"
        if pos.get("retry_after", 0) > time.time():
            flags += f" sell-retry-in-{int(pos['retry_after'] - time.time())}s"
        print(f"  {pos['symbol']:<10} {fmt_usd(pos['buy_usd']):>8} in | held {fmt_usd(value):>9} "
              f"+ sold {fmt_usd(pos['usdg_out']):>8} | pnl {pnl:+8.2f} | {age_h:5.1f}h | "
              f"stages {pos['stages_done']} origin_exit={pos['origin_exiting']} | from {pos['origin_label']}"
              f"{' (paper)' if pos['paper'] else ''}{flags}")
        print(f"             {pos['token']}   https://robinhoodchain.blockscout.com/token/{pos['token']}")
    print(f"Closed ({len(STATE['closed'])}):")
    for pos in STATE["closed"][-20:]:
        total += pos["pnl_usd"]
        print(f"  {pos['symbol']:<10} pnl {pos['pnl_usd']:+8.2f} | from {pos['origin_label']}"
              f"{' (paper)' if pos['paper'] else ''} | {pos['token']}")
    print(f"Total pnl: {total:+.2f} USDG")
    if not CFG["live"] and CFG.get("paper_cash_usd"):
        held = sum((pos["remaining_raw"] / 10**pos["decimals"]) * (token_info(pos["token"])["price"] or 0)
                   for pos in STATE["positions"].values())
        print(f"Paper bankroll: cash {fmt_usd(paper_cash())} + positions {fmt_usd(held)} = "
              f"{fmt_usd(paper_cash() + held)} (started {fmt_usd(CFG['paper_cash_usd'])})")


def cmd_route(token):
    info = token_info(token, fresh=True)
    meta = token_meta(token)
    print(f"{meta['symbol']} price ${info['price']} liquidity {fmt_usd(info['liquidity'])} "
          f"buys/sells 24h {info['buys24']}/{info['sells24']}")
    t0 = time.time()
    lb, ls, desc, depth = discover_route(token)
    amount_in = int(CFG["buy_usd"] * 10**USDG_DEC)
    out, impact, rt = quote_check(lb, ls, amount_in)
    back = quote_route(ls, out)
    print(f"route: {desc} (depth {fmt_usd(depth)}) found in {time.time() - t0:.1f}s")
    print(f"  price impact of {fmt_usd(CFG['buy_usd'])} vs a $1 probe: {impact:+.2%}   (bot skips above "
          f"{CFG.get('max_price_impact_pct', 10)}%; round trip floor -{CFG.get('max_round_trip_loss_pct', 15)}%)")
    print(f"  buy  {fmt_usd(CFG['buy_usd'])} -> {out / 10**meta['decimals']:,.6g} {meta['symbol']}"
          f"  (implied ${CFG['buy_usd'] / (out / 10**meta['decimals']):.6g}, "
          f"{(CFG['buy_usd'] / (out / 10**meta['decimals'])) / info['price'] - 1:+.2%} vs market)")
    print(f"  sell back -> {fmt_usd(back / 10**USDG_DEC)} (round trip {back / amount_in - 1:+.2%})")
    print("  legs_buy:", json.dumps(lb))


def cmd_sell(what, pct=100):
    """Sell part or all of an open position now (live), recording it like any
    other exit. `what` is a token address or symbol."""
    global SIGNER
    match = [p for p in STATE["positions"].values()
             if p["token"] == what.lower() or p["symbol"].lower() == what.lower()]
    if not match:
        sys.exit(f"no open position matching {what!r}; open: {[p['symbol'] for p in STATE['positions'].values()]}")
    pos = match[0]
    if not CFG["live"]:
        sys.exit("config.live is false: nothing to sell on-chain")
    SIGNER = Signer(ENV["PRIVATE_KEY"])
    bal = balance_of(pos["token"], SIGNER.address)
    raw = bal if pct >= 100 else int(pos["remaining_raw"] * pct / 100)
    legs, quote = best_sell_legs(pos, raw)
    print(f"{pos['symbol']}: selling {raw / 10**pos['decimals']:,.4g} ({pct}%) -> ~{fmt_usd(quote / 10**USDG_DEC)} USDG "
          f"(min {fmt_usd(quote * (1 - SLIPPAGE) / 10**USDG_DEC)} after {SLIPPAGE:.0%} slippage)")
    if input("send? [y/N] ").strip().lower() != "y":
        print("aborted")
        return
    ensure_allowance(pos["token"], raw)
    rec = swap_tx(legs, raw, int(quote * (1 - SLIPPAGE)))
    got = received(rec, USDG) / 10**USDG_DEC
    pos["remaining_raw"] = max(0, pos["remaining_raw"] - raw) if pct < 100 else 0
    pos["usdg_out"] += got
    pos["sells"].append({"ts": time.time(), "why": f"manual {pct}%", "raw": raw, "usd": got, "tx": rec["transactionHash"]})
    append_jsonl("trades.jsonl", {"ts": time.time(), "side": "sell", "token": pos["token"], "symbol": pos["symbol"],
                                  "usd": got, "raw": raw, "tx": rec["transactionHash"], "paper": False, "why": f"manual {pct}%"})
    if pos["remaining_raw"] == 0:
        pos.update(closed_at=time.time(), pnl_usd=pos["usdg_out"] - pos["buy_usd"], note="manual sell")
        STATE["closed"].append(pos)
        del STATE["positions"][pos["token"]]
    save_state()
    print(f"sold for {fmt_usd(got)} USDG, tx {rec['transactionHash']}")


def cmd_holdings(wallet):
    """Current non-dust ERC-20 holdings of a wallet (from recent Transfer logs)."""
    head = int(rpc("eth_blockNumber", []), 16)
    span = CFG.get("holdings_scan_blocks", 3_000_000)
    seen = set()
    for topics in ([TRANSFER_TOPIC, None, pad(wallet)],):
        for lg in get_logs(topics, head - span, head):
            seen.add(lg["address"].lower())
    rows = []
    for tok in seen:
        try:
            bal = balance_of(tok, wallet)
        except Exception:
            continue
        if bal == 0:
            continue
        meta, info = token_meta(tok), token_info(tok)
        amt = bal / 10**meta["decimals"]
        rows.append((amt * (info["price"] or 0), meta["symbol"], amt, tok))
        time.sleep(0.2)
    for usd, sym, amt, tok in sorted(rows, reverse=True):
        print(f"  {sym:<10} {amt:>16,.4g}  {fmt_usd(usd):>12}  {tok}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    args = sys.argv[1:]
    if not args:
        try:
            cmd_run()
        except KeyboardInterrupt:
            save_state()
            print("\nstopped; state saved", flush=True)
            sys.exit(0)
    elif args[0] == "status":
        cmd_status()
    elif args[0] == "route":
        cmd_route(args[1])
    elif args[0] == "holdings":
        cmd_holdings(args[1])
    elif args[0] == "sell":
        cmd_sell(args[1], int(args[2]) if len(args) > 2 else 100)
    elif args[0] == "adopt":
        # bot.py adopt <token> <usd_spent> [unix_ts]: handed to the running bot via a command file
        CMD_DIR.mkdir(parents=True, exist_ok=True)
        (CMD_DIR / f"adopt_{args[1].lower()}.json").write_text(json.dumps(
            {"action": "adopt", "token": args[1].lower(), "usd": float(args[2]),
             "bought_at": float(args[3]) if len(args) > 3 else None, "ts": time.time()}))
        print("adopt request written; the running bot picks it up on its next tick")
    else:
        sys.exit(__doc__)
