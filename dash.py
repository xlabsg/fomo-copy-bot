#!/usr/bin/env python3
"""rh-copybot dashboard: live + paper performance in one auto-refreshing terminal screen.

  python dash.py            full-screen, refreshes every 15s
  python dash.py --once     print one snapshot and exit

Keys: up/down pick a position, o opens it on dexscreener, f opens the origin
wallet's fomo profile, s sells it (asks to confirm; the request goes to the
bot, which executes it on its next tick), r refresh now, q quit.
"""

import curses
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

_HERE = Path(__file__).resolve().parent  # works in ~/Documents and in /opt on the droplet
INSTANCES = [
    ("LIVE", _HERE),
    ("PAPER", _HERE.parent / "rh-copybot-paper"),
    ("PAPER-B", _HERE.parent / "rh-copybot-paper2"),  # separate wallet cohort; skipped if absent
]
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
RPC = "https://rpc.mainnet.chain.robinhood.com"
REFRESH = 15
_s = requests.Session()
_s.headers["User-Agent"] = "rh-copybot-dash/0.1"


# ---------------------------------------------------------------- data

def prices(tokens):
    """dexscreener prices for many tokens, 30 per call -> {addr_lower: price}."""
    out = {}
    toks = list({t.lower() for t in tokens})
    for i in range(0, len(toks), 30):
        try:
            r = _s.get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(toks[i:i + 30]), timeout=15)
            pairs = [p for p in (r.json().get("pairs") or []) if p.get("chainId") == "robinhood"]
        except Exception:
            continue
        best = {}
        for p in pairs:
            liq = (p.get("liquidity") or {}).get("usd") or 0
            base = (p.get("baseToken") or {}).get("address", "").lower()
            quote = (p.get("quoteToken") or {}).get("address", "").lower()
            px = None
            if base in toks and p.get("priceUsd"):
                a, px = base, float(p["priceUsd"])
            elif quote in toks and p.get("priceUsd") and p.get("priceNative"):
                try:
                    a, px = quote, float(p["priceUsd"]) / float(p["priceNative"])
                except (ValueError, ZeroDivisionError):
                    px = None
            if px is not None and (a not in best or liq > best[a][0]):
                best[a] = (liq, px)
        out.update({a: v[1] for a, v in best.items()})
    for t in toks:  # anything the batch missed: one direct lookup
        if t not in out:
            try:
                r = _s.get(f"https://api.dexscreener.com/latest/dex/tokens/{t}", timeout=10)
                cands = [(((p.get("liquidity") or {}).get("usd") or 0), float(p["priceUsd"]))
                         for p in (r.json().get("pairs") or [])
                         if p.get("chainId") == "robinhood" and p.get("priceUsd")
                         and (p.get("baseToken") or {}).get("address", "").lower() == t]
                if cands:
                    out[t] = max(cands)[1]
            except Exception:
                pass
    return out


_last_cash = {}


def usdg_balance(wallet):
    data = "0x70a08231" + wallet[2:].lower().rjust(64, "0")
    for attempt in range(3):
        try:
            r = _s.post(RPC, json=[{"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                    "params": [{"to": USDG, "data": data}, "latest"]},
                                   {"jsonrpc": "2.0", "id": 2, "method": "eth_getBalance",
                                    "params": [wallet, "latest"]}], timeout=10).json()
            r = {x["id"]: x["result"] for x in r}
            _last_cash[wallet] = (int(r[1], 16) / 1e6, int(r[2], 16) / 1e18)
            return _last_cash[wallet]
        except Exception:
            time.sleep(1 + attempt)
    return _last_cash.get(wallet, (None, None))


def load(inst_dir):
    cfg = json.loads((inst_dir / "config.json").read_text()) if (inst_dir / "config.json").exists() else {}
    st = {"positions": {}, "closed": []}
    if (inst_dir / "data/state.json").exists():
        try:
            st.update(json.loads((inst_dir / "data/state.json").read_text()))
        except ValueError:
            pass
    sigs, n_all, n_bought = [], 0, 0
    f = inst_dir / "data/signals.jsonl"
    if f.exists():
        lines = f.read_text().splitlines()
        n_all = len(lines)
        n_bought = sum(1 for l in lines if '"outcome": "bought"' in l)
        for l in lines[-40:]:
            try:
                sigs.append(json.loads(l))
            except ValueError:
                pass
    return cfg, st, sigs, n_all, n_bought


def snapshot():
    """Gather everything the screen needs for both instances."""
    snap = {"ts": time.time(), "inst": []}
    all_tokens = set()
    loaded = []
    for name, d in INSTANCES:
        if not d.exists():
            continue
        cfg, st, sigs, n_all, n_bought = load(d)
        loaded.append((name, d, cfg, st, sigs, n_all, n_bought))
        all_tokens |= {p["token"] for p in st["positions"].values()}
    px = prices(all_tokens) if all_tokens else {}
    for name, d, cfg, st, sigs, n_all, n_bought in loaded:
        rows = []
        unreal = 0.0
        for pos in sorted(st["positions"].values(), key=lambda p: -p["bought_at"]):
            held = pos["remaining_raw"] / 10 ** pos["decimals"]
            price = px.get(pos["token"].lower())
            unknown = held > 0 and price is None
            value = held * (price or 0)
            pnl = value + pos["usdg_out"] - pos["buy_usd"]
            if not unknown:
                unreal += pnl
            stages = "".join("✓" if i in pos["stages_done"] else "·" for i in range(len(cfg.get("exits", []))))
            stages += "✓" if pos.get("origin_done") else ("!" if pos.get("origin_exiting") else "·")
            pending = (d / "data/commands" / f"sell_{pos['token']}.json").exists()
            rows.append({"sym": pos["symbol"], "in": pos["buy_usd"], "held": value, "sold": pos["usdg_out"],
                         "dir": str(d), "inst": name, "pending": pending,
                         "pnl": pnl, "age_h": (time.time() - pos["bought_at"]) / 3600, "stages": stages,
                         "from": pos["origin_label"], "token": pos["token"],
                         "unknown": unknown,
                         "flag": "sell requested" if pending else
                                 ("SELL-SIM-FAILED" if pos.get("sell_simulated") is False else
                                  ("retrying sell" if pos.get("retry_after", 0) > time.time() else
                                   ("no price" if unknown else "")))})
        closed = st["closed"]
        realized = sum(c.get("pnl_usd", 0) for c in closed)
        wins = sum(1 for c in closed if c.get("pnl_usd", 0) > 0)
        stats = trade_stats(closed, rows)
        live = cfg.get("live", False)
        cash, gas = None, None
        if live:
            # the deploy owner is the hot wallet; read it from the router's owner is overkill — use env-free RPC on config wallet if present
            wallet = cfg.get("wallet") or "0xBf4777C71D2dbE842c36621C446C6F2b64f87233"
            cash, gas = usdg_balance(wallet)
        else:
            cash = st.get("paper_cash", cfg.get("paper_cash_usd"))
        positions_value = sum(r["held"] for r in rows)
        snap["inst"].append({
            "name": name, "live": live, "buy_usd": cfg.get("buy_usd"), "rows": rows, "closed": closed[-8:][::-1],
            "n_closed": len(closed), "wins": wins, "realized": realized, "unreal": unreal,
            "deployed": sum(r["in"] - r["sold"] for r in rows), "cash": cash, "positions_value": positions_value,
            "start": cfg.get("paper_cash_usd"), "signals": sigs[-12:][::-1],
            "gas": gas, "min_gas": cfg.get("min_gas_eth", 0.003), "stats": stats,
            "n_sig": n_all, "n_bought": n_bought,
        })
    return snap


def trade_stats(closed, open_rows):
    """Win/loss breakdown of closed trades plus realized PnL per origin wallet
    (open positions count at their current mark)."""
    pnls = [c.get("pnl_usd", 0) for c in closed]
    w = [x for x in pnls if x > 0]
    l = [x for x in pnls if x <= 0]
    best = max(closed, key=lambda c: c.get("pnl_usd", 0), default=None)
    worst = min(closed, key=lambda c: c.get("pnl_usd", 0), default=None)
    by_wallet = {}
    for c in closed:
        k = c.get("origin_label", "?")
        e = by_wallet.setdefault(k, {"pnl": 0.0, "n": 0, "wins": 0, "open": 0})
        e["pnl"] += c.get("pnl_usd", 0)
        e["n"] += 1
        e["wins"] += c.get("pnl_usd", 0) > 0
    for r in open_rows:
        if r["unknown"]:
            continue
        e = by_wallet.setdefault(r["from"], {"pnl": 0.0, "n": 0, "wins": 0, "open": 0})
        e["pnl"] += r["pnl"]
        e["open"] += 1
    ranked = sorted(by_wallet.items(), key=lambda kv: -kv[1]["pnl"])
    return {
        "n": len(pnls), "wins": len(w), "losses": len(l),
        "win_rate": len(w) / len(pnls) if pnls else 0,
        "avg_win": sum(w) / len(w) if w else 0, "avg_loss": sum(l) / len(l) if l else 0,
        "profit_factor": (sum(w) / -sum(l)) if l and sum(l) < 0 else (float("inf") if w else 0),
        "best": (best["symbol"], best["pnl_usd"]) if best else None,
        "worst": (worst["symbol"], worst["pnl_usd"]) if worst else None,
        "open_up": sum(1 for r in open_rows if not r["unknown"] and r["pnl"] > 0),
        "open_down": sum(1 for r in open_rows if not r["unknown"] and r["pnl"] <= 0),
        "top": ranked[:5], "bottom": ranked[-5:][::-1] if len(ranked) > 5 else [],
    }


# ---------------------------------------------------------------- render

def money(x, signed=False):
    if x is None:
        return "   n/a"
    return f"{x:+,.2f}" if signed else f"{x:,.2f}"


def dex_url(token):
    return f"https://dexscreener.com/robinhood/{token}"


def fomo_url(handle):
    return f"https://fomo.family/profile/{handle}"


def lines_for(inst, width, selected=None):
    L = []
    hdr = f" {inst['name']}  "
    if inst["live"]:
        equity = (inst["cash"] or 0) + inst["positions_value"]
        hdr += f"wallet USDG {money(inst['cash'])} + positions {money(inst['positions_value'])} = {money(equity)}"
        if inst.get("gas") is not None:
            low = inst["gas"] < inst["min_gas"]
            hdr += f"   |  gas {inst['gas']:.4f} ETH" + ("  ⚠ LOW — top up, sells are failing" if low else "")
    else:
        equity = (inst["cash"] or 0) + inst["positions_value"]
        hdr += (f"bankroll {money(equity)} of {money(inst['start'])} start  "
                f"(cash {money(inst['cash'])} + positions {money(inst['positions_value'])})")
    L.append(("hdr", hdr))
    total = inst["realized"] + inst["unreal"]
    L.append(("kv", f"   pnl total {money(total, True)}   realized {money(inst['realized'], True)} on {inst['n_closed']} closed"
                    f" ({inst['wins']} wins)   unrealized {money(inst['unreal'], True)} on {len(inst['rows'])} open"
                    f"   ${inst['buy_usd']}/buy   signals {inst['n_sig']} → bought {inst['n_bought']}", total))
    st = inst.get("stats")
    if st and st["n"]:
        pf = "∞" if st["profit_factor"] == float("inf") else f"{st['profit_factor']:.2f}"
        L.append(("kv", f"   closed {st['n']}: {st['wins']} wins / {st['losses']} losses  ({st['win_rate']:.0%})   "
                        f"avg win {money(st['avg_win'], True)}  avg loss {money(st['avg_loss'], True)}  profit factor {pf}   "
                        f"best {st['best'][0]} {money(st['best'][1], True)}   worst {st['worst'][0]} {money(st['worst'][1], True)}   "
                        f"open: {st['open_up']} up / {st['open_down']} down", st["wins"] - st["losses"]))
    if st and st["top"]:
        def wal(kv):
            k, e = kv
            tag = f"{e['wins']}/{e['n']}" if e["n"] else "open"
            return f"{k[:12]} {money(e['pnl'], True)} ({tag})"
        L.append(("col", "   best wallets:  " + "   ".join(wal(kv) for kv in st["top"])))
        if st["bottom"]:
            L.append(("col", "   worst wallets: " + "   ".join(wal(kv) for kv in st["bottom"])))
    L.append(("col", f"   {'token':<10}{'in':>9}{'held':>10}{'sold':>10}{'pnl':>10}{'%':>8}  {'age':>6}  stages  "
                     f"{'fomo profile':<44}  dexscreener"))
    if not inst["rows"]:
        L.append(("dim", "   (no open positions)"))
    for r in inst["rows"]:
        pct = r["pnl"] / r["in"] * 100 if r["in"] else 0
        age = f"{r['age_h']:.1f}h" if r["age_h"] < 48 else f"{r['age_h'] / 24:.1f}d"
        held_s, pnl_s, pct_s = (("?", "?", "?") if r["unknown"]
                                else (money(r["held"]), money(r["pnl"], True), f"{pct:+.1f}%"))
        mark = " ▶" if r is selected else "  "
        line = (f"{mark} {r['sym'][:10]:<10}{money(r['in']):>9}{held_s:>10}{money(r['sold']):>10}"
                f"{pnl_s:>10}{pct_s:>8}  {age:>6}  {r['stages']:<6}  {fomo_url(r['from']):<44}"
                + f"  {dex_url(r['token'])}"
                + (f"  {r['flag']}" if r["flag"] else ""))
        L.append(("pnl", line, 0 if r["unknown"] else r["pnl"], r is selected))
    if inst["closed"]:
        L.append(("col", "   closed: " + "  ".join(f"{c['symbol'][:8]} {money(c.get('pnl_usd', 0), True)}" for c in inst["closed"])))
    return L


def signal_lines(snap):
    L = [("hdr", " RECENT SIGNALS")]
    merged = []
    for inst in snap["inst"]:
        for s in inst["signals"]:
            merged.append((s.get("ts", 0), inst["name"], s))
    for ts, name, s in sorted(merged, key=lambda x: x[0], reverse=True)[:14]:
        o = s.get("outcome", "")
        kind = "buy" if o == "bought" else ("skip" if o.startswith("skip") else o)
        why = o.replace("skip: ", "")
        L.append(("sig", f"   {time.strftime('%H:%M:%S', time.localtime(ts))}  {name:<5} {s.get('label', '')[:14]:<14} "
                         f"{s.get('symbol', '')[:10]:<10} {why[:70]}", kind))
    return L


def open_link(url):
    """Open in the local browser when there is one; otherwise hand the URL back
    to show on screen (e.g. running over SSH on the droplet)."""
    import shutil
    if sys.platform == "darwin" and shutil.which("open"):
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    if shutil.which("xdg-open") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    return False


def all_rows(snap):
    return [r for inst in snap["inst"] for r in inst["rows"]]


def draw(stdscr, snap, sel=None, notice=""):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    y = 0

    def put(text, attr=0):
        nonlocal y
        if y < h - 1:
            stdscr.addnstr(y, 0, text, w - 1, attr)
            y += 1

    title = (f" rh-copybot dashboard  {time.strftime('%H:%M:%S', time.localtime(snap['ts']))}  "
             f"↑↓ pick  o dexscreener  f fomo  s sell  r refresh  q quit")
    put(title.ljust(w - 1), curses.A_REVERSE)
    if notice:
        # prompts and confirmations get their own full line so they are never clipped
        put(f" >> {notice}".ljust(w - 1), curses.A_BOLD | curses.color_pair(3))
    for inst in snap["inst"]:
        put("")
        for item in lines_for(inst, w, sel):
            kind, text = item[0], item[1]
            if kind == "hdr":
                put(text, curses.A_BOLD | curses.color_pair(4))
            elif kind == "col":
                put(text, curses.A_DIM)
            elif kind == "dim":
                put(text, curses.A_DIM)
            elif kind in ("pnl", "kv"):
                v = item[2]
                attr = curses.color_pair(1 if v > 0 else 2 if v < 0 else 0)
                if len(item) > 3 and item[3]:
                    attr |= curses.A_REVERSE
                put(text, attr)
            else:
                put(text)
    put("")
    for item in signal_lines(snap):
        kind, text = item[0], item[1]
        if kind == "hdr":
            put(text, curses.A_BOLD | curses.color_pair(4))
        else:
            k = item[2]
            put(text, curses.color_pair(1) if k == "buy" else curses.A_DIM)
    stdscr.refresh()


class Refresher(threading.Thread):
    """Fetches snapshots in the background so the screen and keys stay responsive
    while dexscreener / RPC calls are in flight."""

    def __init__(self):
        super().__init__(daemon=True)
        self.snap = None
        self.wake = threading.Event()
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                self.snap = snapshot()
            except Exception:
                pass
            self.wake.wait(REFRESH)
            self.wake.clear()


def main_curses(stdscr):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_YELLOW)
    curses.init_pair(4, curses.COLOR_CYAN, -1)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    ref = Refresher()
    ref.start()
    st = {"shown": None, "sel": None, "notice": ""}  # sel = (inst, token) of the highlighted row

    def selected(snap):
        rows = all_rows(snap)
        for r in rows:
            if (r["inst"], r["token"]) == st["sel"]:
                return r
        return rows[0] if rows else None

    def redraw():
        if st["shown"] is not None:
            draw(stdscr, st["shown"], selected(st["shown"]), st["notice"])

    def handle_key(ch):
        shown = st["shown"]
        if ch in (ord("q"), ord("Q")):
            ref.stop.set()
            ref.wake.set()
            return "quit"
        if ch in (ord("r"), ord("R")):
            ref.wake.set()
        elif ch == curses.KEY_RESIZE:
            redraw()
        elif shown is not None and ch in (curses.KEY_UP, curses.KEY_DOWN, ord("j"), ord("k")):
            rows = all_rows(shown)
            if rows:
                cur = selected(shown)
                i = rows.index(cur) if cur in rows else 0
                i = (i + (1 if ch in (curses.KEY_DOWN, ord("j")) else -1)) % len(rows)
                st["sel"] = (rows[i]["inst"], rows[i]["token"])
                redraw()
        elif shown is not None and ch in (ord("o"), ord("O"), ord("f"), ord("F")):
            r = selected(shown)
            if r:
                fomo = ch in (ord("f"), ord("F"))
                url = fomo_url(r["from"]) if fomo else dex_url(r["token"])
                what = f"{r['from']}'s fomo profile" if fomo else f"{r['sym']} on dexscreener"
                st["notice"] = f"opened {what}" if open_link(url) else f"{what}: {url}   (cmd/ctrl-click the link, or copy it)"
                redraw()
        elif shown is not None and ch in (ord("s"), ord("S")):
            r = selected(shown)
            if r:
                st["sel"] = (r["inst"], r["token"])
                st["notice"] = (f"SELL 100% of {r['sym']} ({r['inst']}, ~{money(r['held'])} at last mark)?  "
                                f"press y to confirm, any other key to cancel")
                redraw()
                stdscr.nodelay(False)
                ans = stdscr.getch()
                stdscr.nodelay(True)
                if ans in (ord("y"), ord("Y")):
                    cmd_dir = Path(r["dir"]) / "data/commands"
                    cmd_dir.mkdir(parents=True, exist_ok=True)
                    (cmd_dir / f"sell_{r['token']}.json").write_text(json.dumps(
                        {"action": "sell", "token": r["token"], "pct": 100, "ts": time.time()}))
                    st["notice"] = f"sell of {r['sym']} handed to the {r['inst']} bot (executes on its next tick)"
                    ref.wake.set()
                else:
                    st["notice"] = "sell cancelled"
                redraw()
        return None

    while True:
        if ref.snap is None:
            stdscr.erase()
            stdscr.addnstr(0, 0, " rh-copybot dashboard   loading...", stdscr.getmaxyx()[1] - 1, curses.A_REVERSE)
            stdscr.refresh()
        elif ref.snap is not st["shown"]:
            st["shown"] = ref.snap
            redraw()
        ch = stdscr.getch()
        if ch == -1:
            curses.napms(150)
            continue
        try:
            if handle_key(ch) == "quit":
                return
        except Exception as e:  # a key handler must never take the screen down
            st["notice"] = f"error: {str(e)[:90]}"
            redraw()


def main_once():
    snap = snapshot()
    print(f"rh-copybot dashboard  {time.strftime('%H:%M:%S', time.localtime(snap['ts']))}")
    for inst in snap["inst"]:
        print()
        for item in lines_for(inst, 200):
            print(item[1])
    print()
    for item in signal_lines(snap):
        print(item[1])


if __name__ == "__main__":
    if "--once" in sys.argv:
        main_once()
    else:
        curses.wrapper(main_curses)
