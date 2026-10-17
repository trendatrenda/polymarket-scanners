"""
pm_mm_paper.py  --  Polymarket idea #34, STEP 2: PAPER MARKET-MAKING for rewards
================================================================================
Step 1 showed some reward pots are big and nearly empty -- because those markets
swing wildly and makers get "picked off". This paper bot answers the real question:
do the REWARDS pay more than the LOSSES from being filled before price moves?
Pretend orders only: NO wallet, NO keys, NO real orders.

    python pm_mm_paper.py --every 30      run every 30 minutes until Ctrl+C
    python pm_mm_paper.py --summary       results so far

THE LOCKED RULES (agreed before the first run)
  Markets   the 20 best rewarded markets by step-1 estimate (relaxed quote): $100
            meets the minimum size, 2+ days left, other makers already there.
            Once joined, a market is kept while it stays eligible; when it drops
            out we stop quoting but HOLD what we own until it resolves.
  Quotes    every cycle, around the middle price (Polymarket's: ignoring tiny orders):
            buy "Yes" at (middle - half the max spread) and buy "No" at
            (1 - middle - half the max spread), rounded down to a whole cent;
            ~$100 worth each side. If we already hold 100+ more "Yes" than "No"
            shares, stop bidding "Yes" (and the reverse) -- no endless pile-up.
  Fills     from REAL trades since the last cycle: our "Yes" bid counts as filled
            only if the price traded BELOW it (fully through our price); same
            for "No". Fills right at our price are NOT counted (we may be last in
            the queue). Through-fills are the dangerous ones, so this is honest.
  Rewards   our share of the pot = our score / (our score + others' score now),
            Polymarket's formula incl. the 1/3 penalty when only one side is quoted;
            credited for the time since the last cycle, at most 60 minutes (orders
            left alone while the Mac sleeps would drift out of the reward zone).
  Value     what we hold, at today's middle price; at resolution, the real payout.
  PASS after 14+ days, all three:
     1. rewards + trading profit/loss > 0
     2. positive in BOTH weeks (first 7 days / the rest)
     3. still positive if rewards were only HALF (competition may be bigger)
  Not counted: maker rebates on fills (a small extra) and the $1 minimum payout.
"""
import argparse
import csv
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import pm_rewards_scanner as rs

HERE = Path(__file__).resolve().parent
STATE = HERE / "pm_mm_state.json"
CYCLES_CSV = HERE / "pm_mm_log.csv"
FILLS_CSV = HERE / "pm_mm_fills.csv"
GAMMA_ONE = "https://gamma-api.polymarket.com/markets/{}"
TRADES = "https://data-api.polymarket.com/v2/trades"
N_MARKETS = 20
BUDGET = 100.0
INV_CAP = 100.0           # shares
MAX_CREDIT_S = 3600
MIN_DAYS_LEFT = 2
C = 3.0
CYCLE_FIELDS = ["time", "active", "holding", "rewards_total", "pnl_total", "net_total",
                "pnl_if_sold_now", "net_if_sold_now",
                "market_days", "fills_total", "rewards_half_net"]
FILL_FIELDS = ["time", "market_id", "question", "side", "price", "shares", "mid_now"]


# ---------------------------------------------------------------------------
def now_ts():
    return int(datetime.now(timezone.utc).timestamp())


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"started": now_ts(), "last_cycle": None, "markets": {}, "rewards": 0.0,
            "market_days": 0.0, "fills": 0, "realized": 0.0}


def save_state(s):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(STATE)


def append(path, fields, rows):
    if path.exists():                              # older file with fewer columns -> upgrade it
        with path.open(newline="") as f:
            rd = csv.DictReader(f)
            if rd.fieldnames and rd.fieldnames != fields:
                old = list(rd)
                with path.open("w", newline="") as g:
                    w = csv.DictWriter(g, fieldnames=fields, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(old)
    first = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if first:
            w.writeheader()
        w.writerows(rows)


def trades_since(cid, since):
    """Real taker trades after `since`, as (time, yes_price, taker_buys_yes, shares)."""
    out, cursor = [], None
    for _ in range(5):
        params = {"cursor": cursor} if cursor else {"condition": cid, "limit": 1000,
                                                    "taker_only": "true"}
        d = rs.get_json(TRADES, params)
        if isinstance(d, list):
            items, cursor = d, None
        elif isinstance(d, dict):
            items = d.get("data") or d.get("trades") or []
            pag = d.get("pagination") or {}
            cursor = pag.get("next_cursor") or pag.get("nextCursor") or d.get("next_cursor")
        else:
            break
        oldest = None
        for t in items:
            try:
                ts = int(float(t.get("timestamp") or t.get("time") or 0))
                ts = ts // 1000 if ts > 10 ** 12 else ts
                p, sh = float(t["price"]), float(t.get("size") or t.get("shares"))
                oi = int(t.get("outcome_index", t.get("outcomeIndex")))
                side = str(t.get("side") or "").upper()
            except (TypeError, ValueError, KeyError):
                continue
            oldest = ts if oldest is None else min(oldest, ts)
            if ts <= since or side not in ("BUY", "SELL") or oi not in (0, 1):
                continue
            yes_p = p if oi == 0 else 1 - p
            buys_yes = (side == "BUY") == (oi == 0)    # BUY Yes or SELL No = buying Yes
            out.append((ts, yes_p, buys_yes, sh))
        if not cursor or not items or (oldest is not None and oldest <= since):
            break
        time.sleep(rs.PAUSE)
    return out


def our_score(mid, v, min_size, yb, ys, nb, ns):
    """Our Qmin with Polymarket's formula (one-sided quotes get 1/3)."""
    q1 = rs.score((mid - yb) * 100, v) * ys if yb and ys >= min_size else 0.0
    q2 = rs.score(((1 - mid) - nb) * 100, v) * ns if nb and ns >= min_size else 0.0
    if 0.10 <= mid <= 0.90:
        return max(min(q1, q2), max(q1 / C, q2 / C))
    return min(q1, q2)


def resolution(mkt_id):
    m = rs.get_json(GAMMA_ONE.format(mkt_id))
    if not isinstance(m, dict) or not m.get("closed"):
        return None
    px = rs.as_list(m.get("outcomePrices"))
    try:
        y = float(px[0])
    except (IndexError, TypeError, ValueError):
        return None
    return 1 if y >= 0.99 else 0 if y <= 0.01 else None


def candidates():
    """Eligible rewarded markets with step-1 estimates, best first."""
    t0 = datetime.now(timezone.utc)
    mkts = rs.rewarded_markets()
    toks = [str(t) for m, _ in mkts for t in rs.as_list(m.get("clobTokenIds"))]
    books = rs.books_for(toks)
    out = []
    for m, pot in mkts:
        ty, tn = [str(t) for t in rs.as_list(m.get("clobTokenIds"))]
        e = rs.evaluate(m, pot, books.get(ty), books.get(tn))
        if not e:
            continue
        try:
            end = datetime.fromisoformat(str(m.get("endDate")).replace("Z", "+00:00"))
            days_left = (end - t0).total_seconds() / 86400
        except ValueError:
            continue
        shares = e["relaxed"][0]
        if days_left < MIN_DAYS_LEFT or e["others"][1] <= 0 or shares < e["min_size"]:
            continue
        out.append({"id": str(m.get("id")), "cid": m.get("conditionId"), "m": m, "pot": pot,
                    "e": e, "q": str(m.get("question") or "")[:90], "ty": ty, "tn": tn,
                    "est": e["relaxed"][1][1]})
    out.sort(key=lambda c: -c["est"])
    return out


# ---------------------------------------------------------------------------
def cycle():
    s = load_state()
    t = now_ts()
    elapsed = min(t - s["last_cycle"], MAX_CREDIT_S) if s["last_cycle"] else 0
    print(f"\n  [{datetime.now():%H:%M}] Paper market-maker cycle "
          f"({len(s['markets'])} markets tracked)...")
    new_fills = []

    # 1) fills + rewards for the period since the last cycle
    for mid_, mk in s["markets"].items():
        if mk.get("resolved") is not None:
            continue
        if mk.get("active") and mk.get("last_ts"):
            for ts, yp, buys_yes, sh in sorted(trades_since(mk["cid"], mk["last_ts"])):
                if not buys_yes and mk["yes_bid"] and yp < mk["yes_bid"] - 1e-9 and mk["yes_left"] > 0:
                    f = min(sh, mk["yes_left"])
                    mk["yes_left"] -= f
                    mk["yes_sh"] += f
                    mk["cost"] += f * mk["yes_bid"]
                    new_fills.append((mk, "YES", mk["yes_bid"], f))
                if buys_yes and mk["no_bid"] and (1 - yp) < mk["no_bid"] - 1e-9 and mk["no_left"] > 0:
                    f = min(sh, mk["no_left"])
                    mk["no_left"] -= f
                    mk["no_sh"] += f
                    mk["cost"] += f * mk["no_bid"]
                    new_fills.append((mk, "NO", mk["no_bid"], f))
            time.sleep(rs.PAUSE)
            r = mk.get("rate", 0.0) * elapsed / 86400
            mk["rewards"] += r
            s["rewards"] += r
        # resolved?
        res = resolution(mid_)
        if res is not None:
            payout = mk["yes_sh"] * res + mk["no_sh"] * (1 - res)
            mk["resolved"], mk["active"] = res, False
            mk["pnl"] = payout - mk["cost"]
            s["realized"] += mk["pnl"]
        time.sleep(rs.PAUSE)
    s["market_days"] += sum(1 for mk in s["markets"].values() if mk.get("active")) * elapsed / 86400
    s["fills"] += len(new_fills)

    # 2) choose markets, re-quote
    cands = candidates()
    by_id = {c["id"]: c for c in cands}
    for mid_, mk in s["markets"].items():               # dropped out -> stop quoting, hold
        if mk.get("active") and mid_ not in by_id:
            mk["active"], mk["yes_bid"], mk["no_bid"] = False, None, None
    active = [k for k, mk in s["markets"].items() if mk.get("active")]
    for c in cands:
        if len(active) >= N_MARKETS:
            break
        if c["id"] not in s["markets"]:
            s["markets"][c["id"]] = {"cid": c["cid"], "q": c["q"], "yes_sh": 0.0, "no_sh": 0.0,
                                     "cost": 0.0, "rewards": 0.0, "resolved": None,
                                     "joined": t, "fills": 0}
        if not s["markets"][c["id"]].get("active") and s["markets"][c["id"]]["resolved"] is None:
            s["markets"][c["id"]]["active"] = True
            active.append(c["id"])
    marks = {}
    for k in active:
        mk, c = s["markets"][k], by_id[k]
        e = c["e"]
        mid, v, min_size = e["mid"], e["v"], e["min_size"]
        marks[k] = mid
        d = v / 2 / 100
        yb = math.floor((mid - d) * 100 + 1e-9) / 100
        nb = math.floor(((1 - mid) - d) * 100 + 1e-9) / 100
        shares = BUDGET / max(yb + nb, 0.01)
        net = mk["yes_sh"] - mk["no_sh"]
        ys = 0.0 if net >= INV_CAP or yb <= 0 else shares
        ns = 0.0 if -net >= INV_CAP or nb <= 0 else shares
        ours = our_score(mid, v, min_size, yb, ys, nb, ns)
        others = e["others"][1]
        mk.update({"yes_bid": yb if ys else None, "no_bid": nb if ns else None,
                   "yes_left": ys, "no_left": ns, "last_ts": t, "mid": mid,
                   "bid_yes": e["yes_bid"], "bid_no": 1 - e["yes_ask"],
                   "rate": c["pot"] * ours / (ours + others) if ours + others > 0 else 0.0})
    for k, mk in s["markets"].items():                  # mark held-but-inactive markets
        if k not in marks and mk.get("resolved") is None and k in by_id:
            e_ = by_id[k]["e"]
            mk["mid"], mk["bid_yes"], mk["bid_no"] = e_["mid"], e_["yes_bid"], 1 - e_["yes_ask"]
    for mk, side, px, f in new_fills:
        mk["fills"] = mk.get("fills", 0) + 1

    # 3) value everything
    pnl = pnl_sold = s["realized"]
    for mk in s["markets"].values():
        if mk.get("resolved") is None:
            m = mk.get("mid")
            if m is not None:
                pnl += mk["yes_sh"] * m + mk["no_sh"] * (1 - m) - mk["cost"]
                # cash-out value: a Yes+No pair is always worth $1; the rest sells at the BID
                pairs = min(mk["yes_sh"], mk["no_sh"])
                by, bn = mk.get("bid_yes", m), mk.get("bid_no", 1 - m)
                pnl_sold += (pairs + (mk["yes_sh"] - pairs) * by + (mk["no_sh"] - pairs) * bn
                             - mk["cost"])
    net = s["rewards"] + pnl
    s["last_cycle"] = t
    save_state(s)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    append(FILLS_CSV, FILL_FIELDS, [{"time": stamp, "market_id": next(
        k for k, v_ in s["markets"].items() if v_ is mk), "question": mk["q"], "side": side,
        "price": px, "shares": round(f, 2), "mid_now": round(mk.get("mid") or 0, 3)}
        for mk, side, px, f in new_fills])
    append(CYCLES_CSV, CYCLE_FIELDS, [{
        "time": stamp, "active": len(active),
        "holding": sum(1 for mk in s["markets"].values()
                       if mk.get("resolved") is None and (mk["yes_sh"] or mk["no_sh"])),
        "rewards_total": round(s["rewards"], 2), "pnl_total": round(pnl, 2),
        "pnl_if_sold_now": round(pnl_sold, 2), "net_if_sold_now": round(s["rewards"] + pnl_sold, 2),
        "net_total": round(net, 2), "market_days": round(s["market_days"], 3),
        "fills_total": s["fills"], "rewards_half_net": round(s["rewards"] / 2 + pnl, 2)}])

    days = (t - s["started"]) / 86400
    W = 78
    print("=" * W)
    print(f" #34 STEP 2 PAPER MARKET-MAKER  |  {datetime.now():%Y-%m-%d %H:%M}  |  no real money")
    print("=" * W)
    print(f" Running {days:.1f} days   quoting in {len(active)} markets ($100 each side)   "
          f"new fills this cycle: {len(new_fills)}")
    print(f" Rewards earned:  ${s['rewards']:>8.2f}")
    print(f" Trading P/L:     ${pnl:>8.2f}   (what we hold, valued now, minus what it cost)")
    print(f"   if sold NOW at the best bids: ${pnl_sold:>8.2f}   -> net ${s['rewards'] + pnl_sold:.2f}"
          f" (the honest cash-out value)")
    print(f" NET:             ${net:>8.2f}   = ${net / s['market_days']:.2f} per $100 per day"
          if s["market_days"] > 0 else f" NET:             ${net:>8.2f}")
    print(f" Saved: {CYCLES_CSV.name}, {FILLS_CSV.name}.  Results: python pm_mm_paper.py --summary")
    print("=" * W)


def summary():
    if not STATE.exists():
        print("  Nothing yet -- run: python pm_mm_paper.py --every 30")
        return
    s = load_state()
    with CYCLES_CSV.open(newline="") as f:
        cyc = list(csv.DictReader(f))
    W = 78
    print("=" * W)
    print(" #34 STEP 2 -- PAPER MARKET-MAKING SUMMARY")
    print("=" * W)
    if not cyc:
        print("  No cycles yet.")
        return
    last = cyc[-1]
    t0 = s["started"]
    days = (datetime.strptime(last["time"], "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone.utc).timestamp() - t0) / 86400
    net, half = float(last["net_total"]), float(last["rewards_half_net"])
    md = float(last["market_days"]) or float("nan")
    print(f" {days:.1f} days, {len(cyc)} cycles, {len(s['markets'])} markets joined, "
          f"{last['fills_total']} fills")
    print(f" Rewards ${float(last['rewards_total']):.2f}   trading P/L ${float(last['pnl_total']):.2f}"
          f"   NET ${net:.2f}  (= ${net / md:.2f} per $100 per day)")
    if last.get("net_if_sold_now"):
        print(f" If everything held were sold NOW at the best bids: NET ${float(last['net_if_sold_now']):.2f}"
              f"  (trading P/L ${float(last['pnl_if_sold_now']):.2f})")
    rows = []
    for k, mk in s["markets"].items():
        if mk.get("resolved") is not None:
            pl = mk.get("pnl", 0.0)
        else:
            m = mk.get("mid") or 0
            pl = mk["yes_sh"] * m + mk["no_sh"] * (1 - m) - mk["cost"]
        rows.append((mk["rewards"] + pl, mk["rewards"], pl, mk.get("fills", 0), mk["q"]))
    print(f"\n {'net':>8}{'rewards':>9}{'P/L':>9}{'fills':>6}  market")
    srt = sorted(rows, reverse=True)
    show = srt if len(srt) <= 13 else srt[:8] + [None] + srt[-5:]
    for r in show:
        if r is None:
            print("   ...")
            continue
        print(f" {r[0]:>8.2f}{r[1]:>9.2f}{r[2]:>9.2f}{r[3]:>6}  {r[4][:44]}")
    # weeks
    wk = None
    for c in cyc:
        ct = datetime.strptime(c["time"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
        if ct - t0 >= 7 * 86400:
            wk = float(c["net_total"])
            break
    w1 = wk if wk is not None else net
    w2 = net - wk if wk is not None else float("nan")
    checks = [(f"1. Net profit > 0 (${net:.2f})", net > 0),
              (f"2. Both weeks positive (${w1:.2f} / " + ("not yet" if wk is None else f"${w2:.2f}") + ")",
               wk is not None and w1 > 0 and w2 > 0),
              (f"3. Still positive with HALF the rewards (${half:.2f})", half > 0)]
    print("\n VERDICT (locked rules, needs 14+ days)")
    for text, ok in checks:
        print(f"   [{'PASS' if ok else 'FAIL'}] {text}")
    n = sum(ok for _, ok in checks)
    status = "too early" if days < 14 else ("PASS" if n == 3 else "NOT a pass")
    print(f"\n   {n} of 3   ->  {status}  ({days:.1f} of 14 days)")
    print("=" * W)


def main():
    ap = argparse.ArgumentParser(description="#34 step 2: paper market-making (no real orders)")
    ap.add_argument("--every", type=float, default=0)
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args()
    if a.summary:
        summary()
        return
    if not a.every:
        cycle()
        return
    print(f"  Paper market-maker every {a.every:g} minutes. Ctrl+C to stop (progress is saved).")
    try:
        while True:
            start = time.time()
            try:
                cycle()
            except Exception as e:
                rs.log(f"mm cycle failed: {type(e).__name__}: {e}")
                print(f"  Cycle failed ({type(e).__name__}) -- trying again next time.")
            wait = max(0, a.every * 60 - (time.time() - start))
            print(f"  Next cycle at {datetime.fromtimestamp(time.time() + wait):%H:%M}.")
            time.sleep(wait)
    except KeyboardInterrupt:
        print("\n  Stopped. Progress saved.")


if __name__ == "__main__":
    main()
