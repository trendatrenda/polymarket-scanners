"""
pm_longshot_paper.py  --  #39b paper test of the 30-60c "nothing happens" rule, REAL order books
==================================================================================================
Backtest run 2 (older year, rule locked first) passed. A backtest only knows old TRADED prices,
so this checks what it really costs to buy NO in today's order books. No AI, costs $0.

THE RULE (locked 16 Nov, before any paper bet):
  * open Yes/No markets, NOT sports, scheduled to end in 6.5-7.5 days (every 2-hour run looks,
    so each market gets seen while it is "about 7 days before the end")
  * YES last traded price between 30c and 60c   (same measure the backtest used)
  * buy NO for $20 at the REAL asks, level by level, + the market's fee. Too thin -> no bet.
  * one bet per event; at most 80 open bets; hold to the end
VERDICT at 150 settled bets:
  PASS only if profit > 0 AND the luck range (resampled by event) stays above 0.
EARLY STOP at 50 bets: if NO really cost us 8c+ more than the backtest assumed on average,
  the backtest's edge can't survive -> FAIL early (printed each run).
"""
import csv
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pm_rewards_scanner as rs
from pm_euro_paper import taker_cost, mid_price, fee_rate_of
from pm_longshot_test import is_sport, cat_of, boot

GAMMA = "https://gamma-api.polymarket.com"
STAKE, LO, HI, MAX_OPEN, VERDICT_N, EARLY_N = 20.0, 0.30, 0.60, 80, 150, 50
BETS = "lsp_bets.csv"
FIELDS = ["placed", "market_id", "event", "cat", "question", "end", "vol", "yes_last", "yes_mid",
          "no_backtest", "no_paid", "fee", "shares", "status", "payout", "pnl", "settled"]


def now():
    return datetime.now(timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def read():
    if not os.path.exists(BETS):
        return []
    with open(BETS, newline="") as f:
        return list(csv.DictReader(f))


def write(rows):
    with open(BETS, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def settle(bets):
    for b in bets:
        if b["status"] != "open":
            continue
        m = rs.get_json(f"{GAMMA}/markets/{b['market_id']}")
        if not isinstance(m, dict) or not m.get("closed"):
            continue
        st = m.get("umaResolutionStatus")
        if st is not None and str(st).lower() != "resolved":
            continue
        try:
            px = [float(x) for x in rs.as_list(m.get("outcomePrices"))]
        except (TypeError, ValueError):
            continue
        if px not in ([1.0, 0.0], [0.0, 1.0]):
            continue
        won = px[1] == 1.0                                 # we hold NO
        pay = float(b["shares"]) if won else 0.0
        b.update(status="won" if won else "lost", payout=round(pay, 4), pnl=round(pay - STAKE, 4),
                 settled=iso(now()))
        time.sleep(0.1)
    return bets


def candidates():
    t = now()
    out, why = [], {}
    for page in range(30):
        got = rs.get_json(f"{GAMMA}/markets", {"closed": "false", "active": "true", "limit": 100,
                                               "offset": page * 100,
                                               "end_date_min": iso(t + timedelta(days=6.5)),
                                               "end_date_max": iso(t + timedelta(days=7.5))})
        if not isinstance(got, list) or not got:
            break
        for m in got:
            outs = [str(x).lower() for x in rs.as_list(m.get("outcomes"))]
            toks = [str(x) for x in rs.as_list(m.get("clobTokenIds"))]
            try:
                last = float(m.get("lastTradePrice"))
            except (TypeError, ValueError):
                last = None
            if outs != ["yes", "no"] or len(toks) != 2:
                r = "not yes/no"
            elif is_sport(m):
                r = "sports"
            elif not m.get("enableOrderBook", True) or not m.get("acceptingOrders", True):
                r = "not trading"
            elif last is None or last <= 0:
                r = "never traded"
            elif not LO <= last <= HI:
                r = "YES not 30-60c"
            else:
                r = "ok"
                out.append((m, toks, last))
            why[r] = why.get(r, 0) + 1
        if len(got) < 100:
            break
    return out, why


def report(bets):
    done = [b for b in bets if b["status"] in ("won", "lost")]
    for b in done:
        b["pnl"] = float(b["pnl"])
    print(f"\n#39b LONGSHOT PAPER   open {sum(b['status'] == 'open' for b in bets)}   settled {len(done)}")
    if bets:
        extra = [float(b["no_paid"]) - float(b["no_backtest"]) for b in bets]
        avg_extra = sum(extra) / len(extra)
        print(f"  real NO price vs backtest price: {avg_extra * 100:+.1f}c on average "
              f"(backtest assumed +2c; must stay under +8c)")
        if len(bets) >= EARLY_N and avg_extra >= 0.08:
            print("  EARLY VERDICT: FAIL -- real prices are too far from the backtest's")
    if done:
        tot = sum(b["pnl"] for b in done)
        lo, hi = boot(done) if len(done) >= 10 else (None, None)
        rng = "" if lo is None else f"   luck range {lo / STAKE:+.1%} .. {hi / STAKE:+.1%}"
        print(f"  won {sum(b['status'] == 'won' for b in done)}/{len(done)}   profit ${tot:+.2f} "
              f"({tot / (STAKE * len(done)):+.1%} per bet){rng}")
        if len(done) >= VERDICT_N:
            ok = tot > 0 and lo is not None and lo > 0
            print(f"  VERDICT ({VERDICT_N}+ settled): {'PASS' if ok else 'FAIL'}")
        else:
            print(f"  verdict at {VERDICT_N} settled bets")


def main():
    rs.HARD_DEADLINE[0] = time.time() + 200
    bets = settle(read())
    open_ev = {b["event"] for b in bets if b["status"] == "open"} | {b["event"] for b in bets}
    n_open = sum(b["status"] == "open" for b in bets)
    cands, why = candidates()
    print(f"#39b markets ending in ~7 days: {sum(why.values())}   (why: {why})")
    new = 0
    for m, toks, last in cands:
        ev = str((m.get("events") or [{}])[0].get("id") or f"m{m.get('id')}")
        if ev in open_ev:
            continue
        if n_open >= MAX_OPEN:
            print("  max open bets reached")
            break
        fee = fee_rate_of(m)
        avg, f, cost = taker_cost(toks[1], fee)
        q = str(m.get("question"))[:110]
        if cost is None or cost >= 0.99:
            print(f"  no bet (order book too thin): {q}")
            continue
        mid = mid_price(toks[0])
        bets.append({"placed": iso(now()), "market_id": str(m.get("id")), "event": ev,
                     "cat": cat_of(q), "question": q, "end": m.get("endDate"),
                     "vol": round(float(m.get("volumeNum") or 0)), "yes_last": last,
                     "yes_mid": round(mid, 4) if mid != "" else "", "no_backtest": round(1 - last, 4),
                     "no_paid": round(cost, 4), "fee": round(f, 4), "shares": round(STAKE / cost, 4),
                     "status": "open", "payout": "", "pnl": "", "settled": ""})
        open_ev.add(ev)
        n_open += 1
        new += 1
        print(f"  BET NO ${STAKE:.0f} at {cost * 100:.1f}c  (YES last {last * 100:.0f}c): {q}")
        time.sleep(0.1)
    write(bets)
    print(f"  new bets this run: {new}")
    report(read())


if __name__ == "__main__":
    sys.exit(main())
