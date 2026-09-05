#!/usr/bin/env python3
"""rh-copybot Telegram notifier — a SEPARATE process. The bot never talks to
Telegram; this follows the bot's journal and state files and relays them, so a
slow or dead Telegram can't cost the bot a millisecond.

  stream:   BUY / SELL / closed / ALERT / reconciliation / RPC failover lines,
            batched (max one message per 2s), skips are left out by default
  commands: /status  /open  /closed  /stats  /logs  /skips on|off
            /sell SYMBOL  ->  /confirm   (handed to the bot via a command file)
            /help

Setup: create a bot with @BotFather, put TELEGRAM_BOT_TOKEN=... in .env, start
this service, then send the bot `/start <code>` with the pairing code printed
in this service's log. Only that chat is ever answered.
"""

import html
import json
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dash  # noqa: E402  (read-only snapshot + formatting helpers)

LIVE_DIR = HERE
STATE_FILE = HERE / "data" / "telegram.json"
KEEP = ("[BUY", "[SELL", "[closed]", "[ALERT]", "[reconciled]", "[booked]", "[adopted]", "[pending]",
        "[rpc] primary RPC failing", "[dashboard]", "[origin exit]", "LIVE wallet", "[warn] sell")
SKIP_MARK = "[skip]"


def read_env():
    env = {}
    p = HERE / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update(os.environ)
    return env


ENV = read_env()
TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}"
# Small VPSes often have half-working IPv6; Telegram resolves to v6 first and
# requests hang. Force IPv4 for everything this process does.
import urllib3.util.connection as _conn  # noqa: E402
_conn.HAS_IPV6 = False
_s = requests.Session()


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"chat_id": ENV.get("TELEGRAM_CHAT_ID") or None, "skips": False, "offset": 0}


def save_state(st):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(st))


# ---------------------------------------------------------------- telegram

def send(chat_id, text, pre=False):
    """Send one message; long text is split at 3900 chars. Never raises."""
    chunks = []
    while text:
        chunks.append(text[:3900])
        text = text[3900:]
    for c in chunks:
        body = f"<pre>{html.escape(c)}</pre>" if pre else html.escape(c)
        for attempt in range(6):
            try:
                r = _s.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": body, "parse_mode": "HTML",
                                                       "disable_web_page_preview": True}, timeout=15)
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("Retry-After", "2")))
                    continue
                if r.status_code != 200:
                    log(f"send failed: http {r.status_code} {r.text[:120]}")
                break
            except requests.RequestException as e:
                log(f"send attempt {attempt + 1} failed: {str(e)[:100]}")
                time.sleep(2 * (attempt + 1))
        else:
            log("send GAVE UP after 6 attempts; message dropped")


def updates(offset):
    r = _s.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 25}, timeout=35)
    return r.json().get("result", [])


# ---------------------------------------------------------------- views (mobile width)

def view_status():
    snap = dash.snapshot()
    out = []
    for inst in snap["inst"]:
        if inst["live"]:
            eq = (inst["cash"] or 0) + inst["positions_value"]
            out.append(f"LIVE  cash {dash.money(inst['cash'])} + pos {dash.money(inst['positions_value'])} = {dash.money(eq)}")
            if inst.get("gas") is not None:
                out.append(f"      gas {inst['gas']:.4f} ETH" + ("  LOW!" if inst["gas"] < inst["min_gas"] else ""))
        else:
            eq = (inst["cash"] or 0) + inst["positions_value"]
            out.append(f"PAPER {dash.money(eq)} of {dash.money(inst['start'])}")
        st = inst.get("stats") or {}
        total = inst["realized"] + inst["unreal"]
        out.append(f"      pnl {dash.money(total, True)}  (real {dash.money(inst['realized'], True)}, open {dash.money(inst['unreal'], True)})")
        if st.get("n"):
            out.append(f"      {st['wins']}W/{st['losses']}L {st['win_rate']:.0%}  avg +{st['avg_win']:.0f}/{st['avg_loss']:.0f}  pf {st['profit_factor']:.2f}")
        out.append(f"      open {len(inst['rows'])}  ${inst['buy_usd']}/buy")
        out.append("")
    return "\n".join(out).rstrip()


def view_open(which=None):
    snap = dash.snapshot()
    out = []
    for inst in snap["inst"]:
        if which and inst["name"] != which:
            continue
        out.append(f"{inst['name']} open ({len(inst['rows'])})")
        for r in inst["rows"]:
            pct = "?" if r["unknown"] else f"{r['pnl'] / r['in'] * 100:+.0f}%"
            held = "?" if r["unknown"] else dash.money(r["held"])
            age = f"{r['age_h'] * 60:.0f}m" if r["age_h"] < 1 else f"{r['age_h']:.1f}h"
            out.append(f" {r['sym'][:9]:<9} {held:>7} {pct:>5} {age:>5} {r['stages']} {r['from'][:10]}"
                       + (f" {r['flag']}" if r["flag"] else ""))
        out.append("")
    return "\n".join(out).rstrip() or "nothing open"


def view_closed(n=12):
    snap = dash.snapshot()
    out = []
    for inst in snap["inst"]:
        cl = inst["closed"][:n]
        out.append(f"{inst['name']} last closed")
        for c in cl:
            out.append(f" {c['symbol'][:9]:<9} {c.get('pnl_usd', 0):+8.2f}  {c.get('origin_label', '')[:10]}")
        out.append("")
    return "\n".join(out).rstrip()


def view_stats():
    snap = dash.snapshot()
    out = []
    for inst in snap["inst"]:
        st = inst.get("stats") or {}
        if not st.get("n"):
            continue
        out.append(f"{inst['name']}: {st['n']} closed, {st['win_rate']:.0%} win, pf {st['profit_factor']:.2f}")
        out.append(f" best {st['best'][0]} {st['best'][1]:+.0f}   worst {st['worst'][0]} {st['worst'][1]:+.0f}")
        out.append(" top wallets: " + ", ".join(f"{k[:10]} {e['pnl']:+.0f}" for k, e in st["top"][:4]))
        out.append(" worst:       " + ", ".join(f"{k[:10]} {e['pnl']:+.0f}" for k, e in st["bottom"][:3]))
        out.append("")
    return "\n".join(out).rstrip()


def view_logs(n=25):
    try:
        return subprocess.run(["journalctl", "-u", "rh-copybot", "-n", str(n), "-o", "cat", "--no-pager"],
                              capture_output=True, text=True, timeout=10).stdout.strip()[-3800:] or "(empty)"
    except Exception as e:
        return f"journalctl failed: {e}"


HELP = ("/status  balances + pnl\n/open  positions\n/closed  recent results\n/stats  wins/losses, wallets\n"
        "/logs  last 25 bot lines\n/skips on|off  include skipped signals in the stream\n"
        "/sell SYMBOL  then /confirm  (live bot sells 100%)")


# ---------------------------------------------------------------- command loop

def command_loop(st, code):
    pending_sell = {}
    while True:
        try:
            for u in updates(st["offset"]):
                st["offset"] = u["update_id"] + 1
                save_state(st)
                m = u.get("message") or {}
                text = (m.get("text") or "").strip()
                cid = (m.get("chat") or {}).get("id")
                if not text or cid is None:
                    continue
                if st.get("chat_id") is None:
                    if text.startswith("/start") and code in text:
                        st["chat_id"] = cid
                        save_state(st)
                        send(cid, "paired. this chat now receives the rh-copybot stream.\n\n" + HELP)
                        log(f"paired with chat {cid}")
                    else:
                        send(cid, "send /start <pairing code> (see the notifier log on the droplet)")
                    continue
                if cid != st["chat_id"]:
                    continue  # never answer anyone else
                cmd, _, arg = text.partition(" ")
                cmd = cmd.lower().split("@")[0]
                arg = arg.strip()
                if cmd in ("/status", "/start"):
                    send(cid, view_status(), pre=True)
                elif cmd == "/open":
                    send(cid, view_open(arg.upper() or None), pre=True)
                elif cmd == "/closed":
                    send(cid, view_closed(), pre=True)
                elif cmd == "/stats":
                    send(cid, view_stats(), pre=True)
                elif cmd == "/logs":
                    send(cid, view_logs(), pre=True)
                elif cmd == "/skips":
                    st["skips"] = arg.lower() == "on"
                    save_state(st)
                    send(cid, f"skipped signals in stream: {'on' if st['skips'] else 'off'}")
                elif cmd == "/sell":
                    sym = arg.upper()
                    rows = [r for inst in dash.snapshot()["inst"] if inst["live"] for r in inst["rows"]]
                    hit = [r for r in rows if r["sym"].upper() == sym or r["token"] == sym.lower()]
                    if not hit:
                        send(cid, f"no open live position {sym!r}. open: " + ", ".join(r["sym"] for r in rows))
                    else:
                        pending_sell = {"row": hit[0], "ts": time.time()}
                        send(cid, f"sell 100% of {hit[0]['sym']} (~{dash.money(hit[0]['held'])})?  /confirm within 60s")
                elif cmd == "/confirm":
                    if pending_sell and time.time() - pending_sell["ts"] < 60:
                        r = pending_sell["row"]
                        d = Path(r["dir"]) / "data/commands"
                        d.mkdir(parents=True, exist_ok=True)
                        (d / f"sell_{r['token']}.json").write_text(json.dumps(
                            {"action": "sell", "token": r["token"], "pct": 100, "ts": time.time()}))
                        send(cid, f"sell of {r['sym']} handed to the bot; watch the stream")
                        pending_sell = {}
                    else:
                        send(cid, "nothing to confirm (or it expired)")
                elif cmd == "/help":
                    send(cid, HELP)
                else:
                    send(cid, "unknown command\n" + HELP)
        except Exception as e:
            log(f"command loop: {str(e)[:120]}")
            time.sleep(3)


# ---------------------------------------------------------------- stream

def stream_loop(st):
    """Follow the live bot's journal; batch interesting lines into one message
    every 2s. Never blocks on Telegram: sending happens after the batch is cut."""
    proc = subprocess.Popen(["journalctl", "-fu", "rh-copybot", "-o", "cat", "-n", "0"],
                            stdout=subprocess.PIPE, text=True, bufsize=1)
    buf, last = [], time.time()
    lock = threading.Lock()

    def flush():
        nonlocal buf, last
        with lock:
            batch, buf = buf, []
        last = time.time()
        if batch and st.get("chat_id"):
            send(st["chat_id"], "\n".join(batch)[-3900:], pre=True)

    def reader():
        for line in proc.stdout:
            line = line.rstrip()
            if not line.strip():
                continue
            keep = any(k in line for k in KEEP) or (st.get("skips") and SKIP_MARK in line)
            if keep:
                with lock:
                    buf.append(line[:300])

    threading.Thread(target=reader, daemon=True).start()
    while True:
        time.sleep(0.5)
        if buf and time.time() - last >= 2:
            flush()


def main():
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN missing from .env")
    st = load_state()
    code = f"{random.randint(0, 999999):06d}"
    if st.get("chat_id") is None:
        log(f"not paired yet. In Telegram send the bot:  /start {code}")
    else:
        log(f"paired with chat {st['chat_id']}; streaming")
    threading.Thread(target=stream_loop, args=(st,), daemon=True).start()
    command_loop(st, code)


if __name__ == "__main__":
    main()
