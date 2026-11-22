"""
pm_consistency_probe.py  --  #40 PROBE: linked markets whose prices contradict each other
=========================================================================================
Not a strategy test yet -- a free look (no AI, $0) at HOW OFTEN and HOW BIG these gaps are,
and how long they last. Runs every hour inside the scanners workflow and appends to cp_log.csv.

Three kinds of "these prices can't all be right":
  1. ONE-WINNER events (Polymarket "neg-risk" groups: exactly one answer wins)
       buy YES on every answer:  if it costs < $1 per set  -> guaranteed profit
       buy NO  on every answer:  pays (n-1) per set; if it costs < n-1 -> guaranteed profit
  2. DATE LADDERS  ("X by March", "X by June", ...): "by June" must be >= "by March"
       buy YES(later) + NO(earlier): always pays at least $1 -> profit if it costs < $1
  3. NUMBER LADDERS ("above 3%", "above 4%", ...): "above 4%" must be <= "above 3%"
       buy YES(lower) + NO(higher): always pays at least $1 -> profit if it costs < $1
Costs are REAL: 20 shares per leg bought level by level from the live order book, + fees.
"""
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import pm_rewards_scanner as rs
from pm_euro_paper import fee_rate_of
from pm_longshot_test import is_sport

GAMMA, BOOK = "https://gamma-api.polymarket.com", "https://clob.polymarket.com/book"
N = 20                        # shares per leg (so a "set" pays about $20)
MIN_EDGE = 0.01               # report gaps of at least 1c per $1 after real costs
LOG = "cp_log.csv"
FIELDS = ["time", "kind", "event_id", "event", "legs", "top_cost", "real_cost", "pays",
          "profit_per_set", "profit_pct", "detail"]
MONTH = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\b20\d\d\b", re.I)
NUM = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*(k|m|b|t|%)?", re.I)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def buy_cost(token, shares, fee_rate):
    """$ to buy `shares` at the real asks + fee. None if the book is too thin."""
    bk = rs.get_json(BOOK, {"token_id": token}) or {}
    asks = sorted((float(a["price"]), float(a["size"])) for a in bk.get("asks", []) if f(a.get("size")))
    got, spend = 0.0, 0.0
    for p, s in asks:
        take = min(s, shares - got)
        spend += take * p * (1 + fee_rate * (1 - p))      # fee = rate * p * (1-p) per share
        got += take
        if got >= shares - 1e-9:
            return spend
    return None


def leg_tokens(m):
    t = [str(x) for x in rs.as_list(m.get("clobTokenIds"))]
    return (t[0], t[1]) if len(t) == 2 else (None, None)


def live(m):
    return (m.get("active") and not m.get("closed") and m.get("acceptingOrders", True)
            and f(m.get("bestAsk")) is not None and f(m.get("bestBid")) is not None)


def number_of(title):
    m = NUM.search(str(title or "").replace(",", ""))
    if not m:
        return None
    v = float(m.group(1))
    return v * {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}.get((m.group(2) or "").lower(), 1)


# ------------------------------------------------------------------- the three checks
def one_winner(ev, ms):
    """YES on all / NO on all in a one-winner group (top of book first, real book only if close)."""
    out, n = [], len(ms)
    yes_top = sum(f(m["bestAsk"]) for m in ms)
    no_top = sum(1 - f(m["bestBid"]) for m in ms)
    for kind, top, pays, side in (("all-YES", yes_top, 1.0, 0), ("all-NO", no_top, n - 1.0, 1)):
        if top > pays - MIN_EDGE / 2:                     # not even close at the top of the book
            continue
        real = 0.0
        for m in ms:
            c = buy_cost(leg_tokens(m)[side], N, fee_rate_of(m))
            if c is None:
                real = None
                break
            real += c
        if real is None:
            out.append((kind, n, top, None, pays, "book too thin for 20 shares on every answer"))
            continue
        out.append((kind, n, top, real / N, pays, ""))
    return out


def ladder(ms, key, increasing_is_more_likely):
    """Adjacent pairs where the 'must be more likely' market is priced BELOW the other one."""
    out = []
    rows = sorted(((k, m) for m in ms if (k := key(m)) is not None), key=lambda r: r[0])   # 19 Nov fix: ties
    for (k1, a), (k2, b) in zip(rows, rows[1:]):
        if k1 == k2:
            continue
        lo, hi = (a, b) if increasing_is_more_likely else (b, a)   # hi must be >= lo
        top = f(hi["bestAsk"]) + (1 - f(lo["bestBid"]))           # YES(hi) + NO(lo)
        if top > 1 - MIN_EDGE / 2:
            continue
        c1 = buy_cost(leg_tokens(hi)[0], N, fee_rate_of(hi))
        c2 = buy_cost(leg_tokens(lo)[1], N, fee_rate_of(lo))
        real = None if c1 is None or c2 is None else (c1 + c2) / N
        out.append((2, top, real, 1.0, f"YES '{hi.get('groupItemTitle')}' + NO '{lo.get('groupItemTitle')}'"
                    + ("" if real is not None else " (book too thin)")))
    return out


def events():
    for page in range(240):                    # 19 Nov fix: /events pages above 50 need gzip -> use 50
        got = rs.get_json(f"{GAMMA}/events", {"closed": "false", "active": "true", "limit": 50,
                                              "offset": page * 50})
        if not isinstance(got, list):
            if page == 0:
                raise SystemExit("PROBLEM: Polymarket's event list did not load (see rewards_logs)")
            return
        if not got:
            return
        yield from got
        if len(got) < 50:
            return


def check_event(ev, stats, found):
    ms_all = [m for m in (ev.get("markets") or []) if [str(x).lower() for x in rs.as_list(m.get("outcomes"))] == ["yes", "no"]]
    if len(ms_all) < 2 or any(is_sport(m) for m in ms_all):
        return
    ms = [m for m in ms_all if live(m)]
    title = str(ev.get("title"))[:90]
    if ev.get("negRisk") and ev.get("negRiskAugmented"):
        return                                       # answers can still be ADDED -> no sure thing
    if ev.get("negRisk") and len(ms) == len(ms_all):
        stats["one-winner"] += 1
        for kind, n, top, real, pays, note in one_winner(ev, ms):
            found.append([kind, ev.get("id"), title, n, top, real, pays, note])
    elif not ev.get("negRisk") and len(ms) >= 2:
        qs = [str(m.get("question", "")).lower() for m in ms]
        if (all(" by " in q for q in qs) and all(MONTH.search(str(m.get("groupItemTitle", ""))) for m in ms)
                and len({m.get("endDate") for m in ms}) == len(ms)):
            stats["date ladders"] += 1
            for legs, top, real, pays, note in ladder(ms, lambda m: m.get("endDate"), True):
                found.append(["date ladder", ev.get("id"), title, legs, top, real, pays, note])
        elif all(re.search(r"\b(above|over|more than|at least|or more|greater than|exceed)", q) for q in qs):
            stats["number ladders"] += 1
            for legs, top, real, pays, note in ladder(ms, lambda m: number_of(m.get("groupItemTitle")), False):
                found.append(["number ladder", ev.get("id"), title, legs, top, real, pays, note])


RETIRED = True   # 22 Nov: verdict after 65 hourly runs -- only 4 real gaps, all 1-5%, left untouched
                 # for 10-35 h (money locked for weeks/months + resolution risk). Not worth it.


def main():
    if RETIRED:
        print("#40 consistency probe: RETIRED 22 Nov (Polymarket is basically consistent; gaps of 1-5% "
              "that pros leave alone). Results stay in cp_log.csv.")
        return
    rs.HARD_DEADLINE[0] = time.time() + 210
    t0, found, stats = time.time(), [], {"events": 0, "one-winner": 0, "date ladders": 0, "number ladders": 0}
    for ev in events():
        if time.time() - t0 > 190:
            print("  (time budget used -- rest next hour)")
            break
        stats["events"] += 1
        try:
            check_event(ev, stats, found)
        except Exception as e:                            # one odd event must never stop the scan
            stats["skipped (error)"] = stats.get("skipped (error)", 0) + 1
            print(f"  skipped event {ev.get('id')}: {type(e).__name__}: {e}")

    t = now()
    rows = []
    for kind, eid, title, legs, top, real, pays, note in found:
        profit = None if real is None else pays - real
        rows.append({"time": t, "kind": kind, "event_id": eid, "event": title, "legs": legs,
                     "top_cost": round(top, 4), "real_cost": "" if real is None else round(real, 4),
                     "pays": pays, "profit_per_set": "" if profit is None else round(profit, 4),
                     "profit_pct": "" if profit is None else round(profit / real * 100, 2), "detail": note})
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)

    real_ones = [r for r in rows if r["profit_per_set"] != "" and r["profit_per_set"] >= MIN_EDGE]
    print(f"#40 CONSISTENCY PROBE  {t}   checked: {stats}")
    print(f"  close at the top of the book: {len(rows)}   still a real gap after 20-share fills + fees: {len(real_ones)}")
    for r in sorted(real_ones, key=lambda r: -r["profit_pct"])[:8]:
        print(f"   {r['kind']:13s} +{r['profit_pct']:.1f}%  ({r['legs']} legs, ${r['real_cost'] * N:.2f} -> "
              f"${r['pays'] * N:.2f})  {r['event'][:60]}  {r['detail'][:60]}")


if __name__ == "__main__":
    sys.exit(main())
