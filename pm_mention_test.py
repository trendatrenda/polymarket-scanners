"""
pm_mention_test.py  --  #41 "mention markets" backtest (no AI, $0)
====================================================================
Markets like: Will Trump say "tariff" during his speech?  Dozens every week.
QUESTION: does the market overprice "yes, the word gets said"?

THE RULE (locked BEFORE looking at any result):
  * closed Yes/No mention markets ("say/says/mention ... <quoted word>") of the last 2 years
  * entry = 24 hours before the market's SCHEDULED end (known in advance: no peeking)
  * the market must still have been open then and traded in the 12 h before
  * YES between 10c and 60c  ->  buy NO at (1 - YES) + 2c gap + fee, $20, hold to the end
PASS only if ALL of:
  1. at least 300 bets
  2. profit > 0 after costs
  3. luck range (resampled BY EVENT -- 20 words of one speech count like one bet) above 0
  4. BOTH years profitable on their own (the older year works as an out-of-sample check)
  5. still profitable with a 4c gap (mention markets are thin)
Everything else (other prices, 6 h / 72 h, who is speaking) is EXPLORATION only.
Writes mt_results.txt and mt_bets.csv. Runs on GitHub with the "backtest" workflow.
"""
import csv
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import pm_rewards_scanner as rs
import pm_longshot_test as L

GAMMA = L.GAMMA
DAYS_BACK = 730
HOURS, LO, HI, GAP, GAP2 = 24, 0.10, 0.60, 0.02, 0.04
MIN_BETS = 300
OUT_TXT, OUT_CSV = "mt_results.txt", "mt_bets.csv"
MENTION = re.compile(r"\b(say|says|said|mention|mentions|utter)\b.*[\"“”']", re.I)
NOT_MENTION = re.compile(r"\btweets?\b|\bposts?\b|how many|\d+\s*(times|or more)", re.I)
SPEAKERS = [("Trump", r"trump"), ("Vance", r"vance"), ("Powell / Fed", r"powell|fed chair|fomc"),
            ("Press secretary", r"leavitt|press secretary|briefing"), ("Musk", r"musk|elon")]


def speaker(q):
    for name, pat in SPEAKERS:
        if re.search(pat, q, re.I):
            return name
    return "other"


def mention_markets(stop):
    now = datetime.now(timezone.utc)
    out, why, seen = [], {}, set()
    for k in range(DAYS_BACK // 30 + 1):
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
                seen.add(mid)
                new += 1
                q = str(m.get("question") or "")
                if not MENTION.search(q) or NOT_MENTION.search(q):
                    continue                                   # not a mention market: not counted
                outs = [str(x).lower() for x in rs.as_list(m.get("outcomes"))]
                px = [str(x) for x in rs.as_list(m.get("outcomePrices"))]
                toks = [str(x) for x in rs.as_list(m.get("clobTokenIds"))]
                end, closed = L.ts(m.get("endDate")), L.ts(m.get("closedTime"))
                if outs != ["yes", "no"] or len(toks) != 2:
                    r = "not yes/no"
                elif L.is_sport(m):
                    r = "sports"
                elif px not in (["1", "0"], ["0", "1"]):
                    r = "not cleanly resolved"
                elif not end:
                    r = "no end date"
                elif closed and closed < end - HOURS * 3600:
                    r = "closed before the entry moment"
                else:
                    r = "ok"
                    ev = (m.get("events") or [{}])[0].get("id") or f"m{mid}"
                    out.append({"id": mid, "q": q[:120], "event": str(ev), "end": end,
                                "yes_won": 1 if px[0] == "1" else 0, "tok": toks[0], "fee": L.fee_rate(m),
                                "who": speaker(q), "vol": float(m.get("volumeNum") or 0)})
                why[r] = why.get(r, 0) + 1
            if new == 0:
                break
    return out, why


def main():
    t0 = time.time()
    stop = t0 + 100 * 60
    rs.HARD_DEADLINE[0] = stop + 60
    mk, why = mention_markets(t0 + 30 * 60)
    L.say(f"#41 MENTION MARKETS TEST   run {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    L.say(f"mention markets found: {sum(why.values())}   kept: {len(mk)}   (why not: {why})")
    rows = []
    for i, m in enumerate(mk):
        if time.time() > stop:
            L.say(f"(out of time after {i} markets)")
            break
        y = L.price_at(m["tok"], m["end"] - HOURS * 3600)
        if y is None:
            continue
        rows.append({**m, "y24": y,
                     "y6": L.price_at(m["tok"], m["end"] - 6 * 3600) if 0.05 <= y <= 0.8 else None,
                     "y72": L.price_at(m["tok"], m["end"] - 72 * 3600) if 0.05 <= y <= 0.8 else None})
        if i % 500 == 0:
            print(f"  ... {i} priced ({int(time.time() - t0)} s)", flush=True)
    L.say(f"with a traded price 24 h before the end: {len(rows)}   events: {len({r['event'] for r in rows})}")
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "event", "who", "vol", "fee", "end", "y72", "y24", "y6",
                                          "yes_won", "q"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    main_rows = [dict(r, pnl=L.bet(r["y24"], r["yes_won"], r["fee"], GAP),
                      pnl2=L.bet(r["y24"], r["yes_won"], r["fee"], GAP2)) for r in rows if LO <= r["y24"] <= HI]
    cut = time.time() - 365 * 86400
    old, new = [r for r in main_rows if r["end"] < cut], [r for r in main_rows if r["end"] >= cut]
    L.say("")
    L.say("=== MAIN TEST (locked): buy NO when YES is 10-60c, 24 h before the end, 2c gap + fees ===")
    L.line("all", main_rows)
    L.line("same bets with a 4c gap", main_rows, "pnl2")
    L.line("older year", old)
    L.line("newer year", new)
    v = [r["pnl"] for r in main_rows]
    lo, _ = L.boot(main_rows) if main_rows else (None, None)
    checks = [(f"at least {MIN_BETS} bets", len(v) >= MIN_BETS),
              ("profit > 0", bool(v) and sum(v) > 0),
              ("luck range above 0", lo is not None and lo > 0),
              ("both years profitable", bool(old) and bool(new) and sum(r["pnl"] for r in old) > 0
               and sum(r["pnl"] for r in new) > 0),
              ("still profitable at 4c gap", bool(v) and sum(r["pnl2"] for r in main_rows) > 0)]
    for name, ok in checks:
        L.say(f"  [{'ok ' if ok else 'NO '}] {name}")
    L.say(f"VERDICT: {'PASS -- worth a forward paper test' if all(ok for _, ok in checks) else 'FAIL'}")

    L.say("")
    L.say("=== EXPLORATION ONLY (many slices = some look good by luck) ===")
    for a, b in ((0.03, 0.10), (0.10, 0.25), (0.25, 0.40), (0.40, 0.60), (0.60, 0.80), (0.80, 0.97)):
        L.line(f"buy NO, YES {a:.0%}-{b:.0%}", [dict(r, pnl=L.bet(r["y24"], r["yes_won"], r["fee"], GAP))
                                               for r in rows if a <= r["y24"] < b])
    for h, k in ((6, "y6"), (72, "y72")):
        L.line(f"main rule but {h} h before the end", [dict(r, pnl=L.bet(r[k], r["yes_won"], r["fee"], GAP))
                                                     for r in rows if r[k] is not None and LO <= r[k] <= HI])
    for name, _ in SPEAKERS + [("other", "")]:
        L.line(f"main rule, speaker {name}", [r for r in main_rows if r["who"] == name])
    L.line("main rule, volume under $20k", [r for r in main_rows if r["vol"] < 20_000])
    L.line("main rule, volume $20k+", [r for r in main_rows if r["vol"] >= 20_000])
    L.line("the OPPOSITE: buy YES at 10-60c", [dict(r, pnl=L.bet(r["y24"], r["yes_won"], r["fee"], GAP, "yes"))
                                              for r in rows if LO <= r["y24"] <= HI])
    L.say(f"\nrequests: {rs.STATS}   time: {int(time.time() - t0)} s")
    open(OUT_TXT, "w").write("\n".join(L.LINES) + "\n")


if __name__ == "__main__":
    sys.exit(main())
