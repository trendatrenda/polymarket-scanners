"""
pm_longshot_test.py  --  #39 "longshot bias" backtest (no AI, costs $0 in API credit)
=======================================================================================
IDEA (from a Polymarket study + a public Kalshi bot's live results): people overpay for
cheap YES tickets on unlikely events. If so, buying NO when YES is cheap should win a bit
more often than its price says.

THE RULE (locked BEFORE looking at any result):
  * closed Yes/No markets that ended in the last ~12 months, NOT sports
  * entry moment = 7 days before the market's SCHEDULED end (known in advance: no peeking)
  * the market must still have been open then, and have traded in the 12 h before
  * YES price at entry between 10c and 30c  ->  buy NO at (1 - YES price)
  * REAL COSTS: + 2c for the buy/sell gap (we only know traded prices, not the old order
    book, so we assume we paid 2c worse) + the market's own fee if it had one
  * hold to the end; $20 per bet
PASS only if ALL of:
  1. average profit per bet > 0 after costs, over at least 300 bets
  2. the luck test: the 95% range (bootstrap, resampled BY EVENT, so 10 related markets
     count like one) stays above 0
  3. both halves of the year are profitable on their own (not one lucky stretch)
  4. still profitable with a 3c gap instead of 2c (not balanced on a knife edge)
Everything else printed (other price ranges, categories, 3/14 days) is EXPLORATION only --
looking at many slices and picking the best one is how false hope is made.

Writes lb_results.txt and lb_bets.csv. Runs on GitHub (this computer can't reach Polymarket).
"""
import csv
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import pm_rewards_scanner as rs

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
# RUN 2 (16 Nov, locked BEFORE running): the 30-60c slice that looked good in run 1's
# exploration, tested on the YEAR BEFORE (markets run 1 never saw). Extra check: it must
# survive a 5c gap, because thin markets' "last traded price" can be far from a real ask.
DAYS_FROM, DAYS_BACK = 365, 730
ENTRY_DAYS, LO, HI, GAP, GAP2, STAKE = 7, 0.30, 0.60, 0.02, 0.05, 20.0
MIN_BETS = 150
MAX_MARKETS = 9000                   # price-history calls are the slow part (~0.2 s each)
RUN_SECONDS = 100 * 60
OUT_TXT, OUT_CSV = "lb_results.txt", "lb_bets.csv"

SPORT = re.compile(r"\bvs\.?\b|\bwin (the|on)\b.*(game|match)|\b(nba|nfl|nhl|mlb|mls|ufc|epl|wnba|ncaa|"
                   r"premier league|champions league|la liga|serie a|bundesliga|ligue 1|grand prix|f1|tennis|"
                   r"golf|cricket|o/u|spread|touchdown|exact score|both teams|halftime|first half|corners|"
                   r"goalscorer|to score|map \d|game \d|set \d|round \d|esports?|super bowl|world series|world cup|fifa|"
                   r"transfer window|sign with|play for|win group|"
                   r"stanley cup|playoffs?|mvp|tournament|grand slam|open winner)\b", re.I)
CATS = [("crypto", r"bitcoin|btc|ethereum|\beth\b|solana|xrp|doge|crypto|token|coin\b"),
        ("politics", r"elect|president|senate|congress|governor|mayor|minister|parliament|vote|"
                     r"trump|biden|democrat|republican|party|poll|nominee|cabinet|impeach"),
        ("world", r"russia|ukrain|iran|israel|gaza|china|taiwan|war|ceasefire|strike|nato|military"),
        ("economy", r"fed\b|rate|inflation|cpi|gdp|jobs|unemployment|recession|tariff|s&p|nasdaq|stock")]

LINES = []


def say(s=""):
    print(s, flush=True)
    LINES.append(s)


def ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def cat_of(q):
    for name, pat in CATS:
        if re.search(pat, q, re.I):
            return name
    return "other"


def is_sport(m):
    return bool(m.get("sportsMarketType") or m.get("gameStartTime") or m.get("gameId")
                or SPORT.search(str(m.get("question") or "")))


def fee_rate(m):
    if not m.get("feesEnabled"):
        return 0.0
    fs = m.get("feeSchedule")
    try:
        fs = json.loads(fs) if isinstance(fs, str) else (fs or {})
        return float(fs.get("rate") or 0.0)
    except (ValueError, AttributeError, TypeError):
        return 0.05


# ---------------------------------------------------------------------------- 1. markets
def closed_markets(stop):
    """Closed binary non-sports markets, one month at a time (keeps paging short)."""
    now = datetime.now(timezone.utc)
    out, seen, why = [], set(), {}
    for k in range(DAYS_FROM // 30, DAYS_BACK // 30 + 1):
        hi, lo = now - timedelta(days=30 * k), now - timedelta(days=30 * (k + 1))
        for page in range(300):
            if time.time() > stop:
                break
            got = rs.get_json(f"{GAMMA}/markets", {"closed": "true", "limit": 100, "offset": page * 100,
                                                   "end_date_min": lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                   "end_date_max": hi.strftime("%Y-%m-%dT%H:%M:%SZ")})
            if not isinstance(got, list) or not got:
                break
            new = 0
            for m in got:
                mid = m.get("id")
                if mid in seen:
                    continue
                new += 1
                seen.add(mid)
                outs = [str(x).lower() for x in rs.as_list(m.get("outcomes"))]
                px = [str(x) for x in rs.as_list(m.get("outcomePrices"))]
                toks = [str(x) for x in rs.as_list(m.get("clobTokenIds"))]
                end, closed, start = ts(m.get("endDate")), ts(m.get("closedTime")), ts(m.get("startDate")
                                                                                       or m.get("createdAt"))
                if outs != ["yes", "no"] or len(toks) != 2:
                    r = "not yes/no"
                elif is_sport(m):
                    r = "sports"
                elif px not in (["1", "0"], ["0", "1"]):
                    r = "not cleanly resolved"
                elif not end or not start or end - start < (ENTRY_DAYS + 1) * 86400:
                    r = "too short to enter 7 days before"
                elif closed and closed < end - ENTRY_DAYS * 86400:
                    r = "closed before the entry moment"
                else:
                    r = "ok"
                    ev = (m.get("events") or [{}])[0].get("id") or f"m{mid}"
                    out.append({"id": mid, "q": str(m.get("question"))[:120], "event": str(ev),
                                "end": end, "yes_won": 1 if px[0] == "1" else 0, "tok": toks[0],
                                "fee": fee_rate(m), "neg": bool(m.get("negRisk")),
                                "vol": float(m.get("volumeNum") or 0), "cat": cat_of(str(m.get("question")))})
                why[r] = why.get(r, 0) + 1
            if new == 0:                           # 16 Nov fix: Polymarket gives max 100 per page, so
                break                              # keep paging until a page brings nothing new
    return out, why


def price_at(tok, t):
    """Last traded YES price in the 12 h before moment t (None if nothing traded)."""
    h = rs.get_json(f"{CLOB}/prices-history", {"market": tok, "startTs": int(t - 12 * 3600),
                                               "endTs": int(t), "fidelity": 60})
    pts = (h or {}).get("history") if isinstance(h, dict) else None
    if not pts:
        return None
    p = [x for x in pts if isinstance(x, dict) and x.get("t") is not None and x["t"] <= t]
    return float(p[-1]["p"]) if p else None


# ---------------------------------------------------------------------------- 2. scoring
def bet(yes_px, yes_won, fee, gap, side="no"):
    """Profit in $ of a $20 bet. side 'no' buys NO at (1 - yes) + gap + fee."""
    p = (1 - yes_px) if side == "no" else yes_px
    cost = min(0.999, p + gap + fee * p * (1 - p))
    won = (yes_won == 0) if side == "no" else (yes_won == 1)
    return (STAKE / cost - STAKE) if won else -STAKE


def boot(rows, key="pnl", n=2000):
    """95% range of the average profit per bet, resampling whole EVENTS."""
    by = {}
    for r in rows:
        by.setdefault(r["event"], []).append(r[key])
    evs = list(by.values())
    if len(evs) < 5:
        return None, None
    rnd = random.Random(7)
    means = []
    for _ in range(n):
        pick = [x for _ in evs for x in rnd.choice(evs)]
        means.append(sum(pick) / len(pick))
    means.sort()
    return means[int(0.025 * n)], means[int(0.975 * n)]


def line(name, rows, key="pnl"):
    if not rows:
        say(f"  {name:38s}   0 bets")
        return
    v = [r[key] for r in rows]
    lo, hi = boot(rows, key)
    rng = "" if lo is None else f"  luck range {lo / STAKE:+.1%} .. {hi / STAKE:+.1%}"
    say(f"  {name:38s} {len(v):5d} bets  win {sum(x > 0 for x in v) / len(v):5.1%}  "
        f"avg {sum(v) / len(v) / STAKE:+6.1%} per bet  (${sum(v):+9.0f}){rng}")


def main():
    t0 = time.time()
    stop = t0 + RUN_SECONDS
    rs.HARD_DEADLINE[0] = stop + 60
    mk, why = closed_markets(t0 + 25 * 60)
    say(f"#39 LONGSHOT TEST, RUN 2 (older year, 30-60c rule)   run {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    say(f"closed markets read: {sum(why.values())}   kept: {len(mk)}   (why not: {why})")
    mk.sort(key=lambda m: -m["end"])
    mk = mk[:MAX_MARKETS]
    rows, no_px = [], 0
    for i, m in enumerate(mk):
        if time.time() > stop:
            say(f"(out of time after {i} markets -- results use those)")
            break
        y7 = price_at(m["tok"], m["end"] - ENTRY_DAYS * 86400)
        if y7 is None:
            no_px += 1
            continue
        y3 = price_at(m["tok"], m["end"] - 3 * 86400) if 0.03 <= y7 <= 0.6 else None
        y14 = price_at(m["tok"], m["end"] - 14 * 86400) if 0.03 <= y7 <= 0.6 else None
        rows.append({**m, "y3": y3, "y7": y7, "y14": y14})
        if i % 500 == 0:
            print(f"  ... {i} markets priced ({int(time.time() - t0)} s)", flush=True)
    say(f"markets with a traded price 7 days before the end: {len(rows)}   (no trades then: {no_px})")

    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "event", "cat", "neg", "vol", "fee", "end", "y14", "y7", "y3",
                                          "yes_won", "q"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    # ---------------------------------------------------------------- locked main test
    main_rows = [dict(r, pnl=bet(r["y7"], r["yes_won"], r["fee"], GAP), pnl3=bet(r["y7"], r["yes_won"], r["fee"], GAP2))
                 for r in rows if LO <= r["y7"] <= HI]
    say("")
    say("=== MAIN TEST (locked): buy NO when YES is 30-60c, 7 days before the end, 2c gap + fees ===")
    line("all", main_rows)
    line("same bets with a 5c gap", main_rows, "pnl3")
    mid = sorted(r["end"] for r in main_rows)[len(main_rows) // 2] if main_rows else 0
    first, second = [r for r in main_rows if r["end"] < mid], [r for r in main_rows if r["end"] >= mid]
    line("older half of the year", first)
    line("newer half of the year", second)
    v = [r["pnl"] for r in main_rows]
    lo, _ = boot(main_rows) if main_rows else (None, None)
    checks = [(f"at least {MIN_BETS} bets", len(v) >= MIN_BETS),
              ("average profit > 0", bool(v) and sum(v) > 0),
              ("luck range above 0", lo is not None and lo > 0),
              ("both halves profitable", bool(first) and bool(second) and sum(r["pnl"] for r in first) > 0
               and sum(r["pnl"] for r in second) > 0),
              ("still profitable at 5c gap", bool(v) and sum(r["pnl3"] for r in main_rows) > 0)]
    for name, ok in checks:
        say(f"  [{'ok ' if ok else 'NO '}] {name}")
    say(f"VERDICT: {'PASS -- worth a forward paper test' if all(ok for _, ok in checks) else 'FAIL'}")

    # ---------------------------------------------------------------- exploration only
    say("")
    say("=== EXPLORATION ONLY (do not pick a winner from these: many slices = luck shows up) ===")
    for lo_, hi_ in ((0.03, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.45), (0.45, 0.60)):
        rr = [dict(r, pnl=bet(r["y7"], r["yes_won"], r["fee"], GAP)) for r in rows if lo_ <= r["y7"] < hi_]
        line(f"buy NO, YES {lo_:.0%}-{hi_:.0%}", rr)
    for d, k in ((3, "y3"), (14, "y14")):
        rr = [dict(r, pnl=bet(r[k], r["yes_won"], r["fee"], GAP)) for r in rows
              if r[k] is not None and LO <= r[k] <= HI]
        line(f"main rule but {d} days before the end", rr)
    for c in ("politics", "crypto", "world", "economy", "other"):
        line(f"main rule, category {c}", [r for r in main_rows if r["cat"] == c])
    line("main rule, single-question events", [r for r in main_rows if not r["neg"]])
    line("main rule, many-candidate events", [r for r in main_rows if r["neg"]])
    line("main rule, volume under $50k", [r for r in main_rows if r["vol"] < 50_000])
    line("main rule, volume $50k+", [r for r in main_rows if r["vol"] >= 50_000])
    yes_rows = [dict(r, pnl=bet(r["y7"], r["yes_won"], r["fee"], GAP, "yes")) for r in rows if LO <= r["y7"] <= HI]
    line("the OPPOSITE: buy YES at 30-60c", yes_rows)
    say(f"\nrequests: {rs.STATS}   time: {int(time.time() - t0)} s")
    open(OUT_TXT, "w").write("\n".join(LINES) + "\n")


if __name__ == "__main__":
    sys.exit(main())
