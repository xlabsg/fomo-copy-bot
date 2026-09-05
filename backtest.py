#!/usr/bin/env python3
"""Exit-schedule backtest over real price paths (rebuilt from pool swap events).

  python backtest.py [--hours 24] [--paper|--live]
  HORIZONS=1,2,3,5,10 python backtest.py --hours 4     # custom exit times (minutes)

Scores every 1-, 2- and 3-tranche schedule over the horizons below, using each
trade's own fill price, after a 3% exit cost per tranche. Only trades old
enough to have the full horizon are used, so every schedule sees the same trades.
"""
import itertools, statistics, sys, time
import stats

import os
HORIZONS = [int(x) for x in os.environ.get("HORIZONS", "5,10,15,60,240,1440").split(",")]  # minutes
EXIT = 0.03


def load_trades(inst, hours):
    cfg, positions, _ = stats.load(inst)
    out = []
    for pos in positions:
        if not pos.get("initial_raw") or not pos.get("legs_sell"):
            continue
        if pos["bought_at"] + hours * 3600 > time.time():
            continue
        try:
            sp = stats.swap_path(pos, hours)
        except Exception as e:
            print(f"   (skip {pos['symbol']}: {str(e)[:60]})", flush=True)
            continue
        path = sp["path"]; t_buy = pos["bought_at"]
        after = [x for x in path if x[0] >= t_buy - 5]
        if not after:
            continue
        tokens = pos["initial_raw"] / 10 ** pos["decimals"]; entry = pos["buy_usd"] / tokens
        upq = entry / after[0][1]
        prices = {}
        for m in HORIZONS:
            t = t_buy + m * 60; last = after[0][1]
            for ts, p, _ in path:
                if ts <= t: last = p
                else: break
            prices[m] = last * upq / entry  # multiple of our fill
        out.append((pos["symbol"], pos.get("origin_label", "?"), prices))
    return out


def score(trades, sched, size=100.0):
    pnls = [size * sum(f * (pr[m] * (1 - EXIT) - 1) for m, f in sched) for _s, _o, pr in trades]
    return sum(pnls), statistics.median(pnls), sum(p > 0 for p in pnls) / len(pnls), min(pnls), max(pnls)


def schedules():
    for m in HORIZONS:
        yield f"100% @{lab(m)}", [(m, 1.0)]
    for a, b in itertools.combinations(HORIZONS, 2):
        yield f"50% @{lab(a)}, 50% @{lab(b)}", [(a, .5), (b, .5)]
        yield f"75% @{lab(a)}, 25% @{lab(b)}", [(a, .75), (b, .25)]
        yield f"25% @{lab(a)}, 75% @{lab(b)}", [(a, .25), (b, .75)]
    for a, b, c in itertools.combinations(HORIZONS, 3):
        yield f"34% @{lab(a)}, 33% @{lab(b)}, 33% @{lab(c)}", [(a, .34), (b, .33), (c, .33)]
        yield f"50% @{lab(a)}, 25% @{lab(b)}, 25% @{lab(c)}", [(a, .5), (b, .25), (c, .25)]


def lab(m):
    return f"{m}m" if m < 60 else f"{m // 60}h" if m < 1440 else "24h"


def main():
    args = sys.argv[1:]
    hours = int(args[args.index("--hours") + 1]) if "--hours" in args else 24
    insts = (["PAPER"] if "--paper" in args else ["PAPER-B"] if "--paper-b" in args else ["LIVE"] if "--live" in args
             else [i for i in ("PAPER", "PAPER-B", "LIVE") if stats.INSTANCES[i].exists()])
    allt = []
    for inst in insts:
        tr = load_trades(inst, hours)
        print(f"{inst}: {len(tr)} trades with a full {hours}h path", flush=True)
        allt += tr
    if not allt:
        return
    print(f"\nCOMBINED {len(allt)} trades, $100 each, {EXIT:.0%} exit cost per tranche")
    print(f"   {'schedule':<36}{'total':>10}{'median':>9}{'win%':>6}{'worst':>8}{'best':>8}")
    rows = [(name, *score(allt, s)) for name, s in schedules()]
    for name, tot, med, wr, lo, hi in sorted(rows, key=lambda r: -r[1])[:15]:
        print(f"   {name:<36}{tot:>+10,.0f}{med:>+9.2f}{wr * 100:>5.0f}%{lo:>+8.0f}{hi:>+8.0f}")
    print("   ...")
    for name, tot, med, wr, lo, hi in sorted(rows, key=lambda r: -r[1])[-5:]:
        print(f"   {name:<36}{tot:>+10,.0f}{med:>+9.2f}{wr * 100:>5.0f}%{lo:>+8.0f}{hi:>+8.0f}")
    if 5 in HORIZONS and 10 in HORIZONS:
        cur = score(allt, [(5, .75), (10, .25)])
        print(f"\n   current 75% @5m, 25% @10m: total {cur[0]:+,.0f}  median {cur[1]:+.2f}  win {cur[2] * 100:.0f}%")
    print("\n   by median (robust to one moonshot):")
    for name, tot, med, wr, lo, hi in sorted(rows, key=lambda r: -r[2])[:8]:
        print(f"   {name:<36}{tot:>+10,.0f}{med:>+9.2f}{wr * 100:>5.0f}%")
    # hold-time curve
    print("\n   average multiple of fill at each horizon:", "  ".join(f"{lab(m)} {statistics.mean(pr[m] for _s,_o,pr in allt):.2f}x" for m in HORIZONS))


if __name__ == "__main__":
    main()
