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
v2 FIX (19 Nov, after 10 bets): v1 picked markets by the LAST TRADE (could be days old) and paid
  any price. v2 decides on TODAY's price (middle of the live order book, like the backtest's
  "traded in the last 12 h") and SKIPS a bet if NO costs more than 5c above the fair price
  (the backtest assumed 2c and was stress-tested at 5c). Only v2 bets count in the verdict.
v3 FIX (22 Nov, before any v2 result): 34 of v2's 45 bets were on TWO elections (Spain, Taiwan),
  listed as separate markets, so "one per event" did not stop it. v3 allows at most 3 bets that
  END ON THE SAME DAY, and the luck test treats bets ending the same day as ONE group.
  v2 KEEPS betting side by side as a comparison (separate record); only v3 counts in the verdict.
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
MAX_EXTRA = 0.05
VERSIONS = [("v2", None), ("v3", 3)]   # v2 = no daily cap (comparison), v3 = max 3 per end day (official)
BETS = "lsp_bets.csv"
FIELDS = ["placed", "market_id", "event", "cat", "question", "end", "vol", "yes_last", "yes_mid",
          "no_backtest", "no_paid", "fee", "shares", "status", "payout", "pnl", "settled", "v"]


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
    out, why, seen = [], {}, set()
    # 24 Nov fix: Polymarket stops listing after ~2,100 results ("offset too large"), so the
    # 24-hour window is read in 4-hour slices -- each slice stays far below that limit.
    pages = []
    for k in range(6):
        lo, hi = t + timedelta(days=6.5, hours=4 * k), t + timedelta(days=6.5, hours=4 * (k + 1))
        for page in range(21):
            got = rs.get_json(f"{GAMMA}/markets", {"closed": "false", "active": "true", "limit": 100,
                                                   "offset": page * 100, "end_date_min": iso(lo),
                                                   "end_date_max": iso(hi)})
            if not isinstance(got, list) or not got:
                break
            pages.append(got)
            if len(got) < 100:
                break
        else:
            why["slice too big (some missed)"] = why.get("slice too big (some missed)", 0) + 1
    for got in pages:
        for m in got:
            if m.get("id") in seen:
                continue
            seen.add(m.get("id"))
            outs = [str(x).lower() for x in rs.as_list(m.get("outcomes"))]
            toks = [str(x) for x in rs.as_list(m.get("clobTokenIds"))]
            try:
                last = float(m.get("lastTradePrice"))
            except (TypeError, ValueError):
                last = None
            try:
                bid, ask = float(m.get("bestBid")), float(m.get("bestAsk"))
                mid = (bid + ask) / 2 if 0 < bid < ask < 1 else None
            except (TypeError, ValueError):
                mid = None
            if outs != ["yes", "no"] or len(toks) != 2:
                r = "not yes/no"
            elif is_sport(m):
                r = "sports"
            elif not m.get("enableOrderBook", True) or not m.get("acceptingOrders", True):
                r = "not trading"
            elif mid is None:
                r = "no live order book"
            elif not LO <= mid <= HI:
                r = "YES not 30-60c today"
            else:
                r = "ok"
                out.append((m, toks, last, mid))
            why[r] = why.get(r, 0) + 1
    return out, why


def score(rows, title, by_day):
    """Prints one version's record. by_day: luck test groups bets ending the same day."""
    rows = [dict(b, event=str(b["end"])[:10]) if by_day else dict(b) for b in rows]
    done = [b for b in rows if b["status"] in ("won", "lost")]
    for b in done:
        b["pnl"] = float(b["pnl"])
    print(f"\n{title}   open {sum(b['status'] == 'open' for b in rows)}   settled {len(done)}")
    if rows:
        extra = sum(float(b["no_paid"]) - float(b["no_backtest"]) for b in rows) / len(rows)
        print(f"  real NO price vs today's fair price: {extra * 100:+.1f}c on average (bets above +5c are skipped)")
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
    return rows


def report(bets):
    old = [b for b in bets if not b.get("v")]
    if old:
        done = [b for b in old if b["status"] in ("won", "lost")]
        print(f"  ({len(old)} v1 bets from before the 19 Nov price fix: {len(done)} settled, "
              f"${sum(float(b['pnl']) for b in done):+.2f} -- NOT scored)")
    score([b for b in bets if b.get("v") == "v3"], "#39b v3 (OFFICIAL: max 3 bets ending the same day)", True)
    score([b for b in bets if b.get("v") == "v2"], "#39b v2 (comparison only: no daily cap, so one election "
          "can fill it -- less trustworthy)", False)


def main():
    rs.HARD_DEADLINE[0] = time.time() + 200
    bets = settle(read())
    cands, why = candidates()
    print(f"#39b markets ending in ~7 days: {sum(why.values())}   (why: {why})")
    prices = {}                                            # one order-book read per market, shared by v2 and v3
    for version, cap in VERSIONS:
        mine = [b for b in bets if b.get("v") == version]
        events = {b["event"] for b in mine}
        n_open = sum(b["status"] == "open" for b in mine)
        per_day = {}
        for b in mine:
            per_day[str(b["end"])[:10]] = per_day.get(str(b["end"])[:10], 0) + 1
        new = skipped = 0
        for m, toks, last, ymid in cands:
            day = str(m.get("endDate"))[:10]
            ev = str((m.get("events") or [{}])[0].get("id") or f"m{m.get('id')}")
            if ev in events or (cap and per_day.get(day, 0) >= cap):
                continue
            if n_open >= MAX_OPEN:
                print(f"  {version}: max open bets reached")
                break
            if m.get("id") not in prices:
                prices[m.get("id")] = taker_cost(toks[1], fee_rate_of(m))
            avg, f, cost = prices[m.get("id")]
            q = str(m.get("question"))[:110]
            fair = 1 - ymid
            if cost is None or cost >= 0.99 or cost > fair + MAX_EXTRA:
                skipped += 1
                continue
            bets.append({"placed": iso(now()), "market_id": str(m.get("id")), "event": ev,
                         "cat": cat_of(q), "question": q, "end": m.get("endDate"),
                         "vol": round(float(m.get("volumeNum") or 0)), "yes_last": last,
                         "yes_mid": round(ymid, 4), "no_backtest": round(fair, 4),
                         "no_paid": round(cost, 4), "fee": round(f, 4), "shares": round(STAKE / cost, 4),
                         "status": "open", "payout": "", "pnl": "", "settled": "", "v": version})
            events.add(ev)
            per_day[day] = per_day.get(day, 0) + 1
            n_open += 1
            new += 1
            print(f"  {version} BET NO ${STAKE:.0f} at {cost * 100:.1f}c  (fair {fair * 100:.0f}c): {q}")
        print(f"  {version}: new bets {new}   skipped as too thin / too expensive: {skipped}")
    write(bets)
    report(read())


if __name__ == "__main__":
    sys.exit(main())
