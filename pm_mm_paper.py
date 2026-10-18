"""
pm_mm_paper.py  --  Polymarket idea #34, STEP 2: PAPER MARKET-MAKING for rewards
================================================================================
Do the liquidity REWARDS pay more than the LOSSES from being filled before the price
moves? Pretend orders only: NO wallet, NO keys, NO real orders.

    python pm_hourly.py                one scanner + market-maker cycle (what GitHub runs)
    python pm_mm_paper.py              one market-maker cycle on its own
    python pm_mm_paper.py --summary    results so far

THE LOCKED RULES (version 2, 18 Oct 2026 -- written to be PESSIMISTIC wherever unsure)
  Markets   the 20 best rewarded markets by step-1 estimate (relaxed quote): $100
            meets the minimum size, 2+ days left, other makers already there.
            Once joined, a market keeps its seat while eligible (also while paused).
  Quotes    every cycle (every 2 hours on GitHub; 4 when the minutes guard slows down),
            around Polymarket's middle price
            (ignoring tiny orders): buy "Yes" at (middle - half the max spread) and
            buy "No" at (1 - middle - half the max spread), rounded DOWN to a whole
            cent; ~$100 in total for both sides. Stop bidding a side once we hold
            100+ more shares of it than of the other side.
  Windows   each quote lives from one cycle to the next (one WINDOW). A window is
            checked only once the trade data (judged on the busiest markets) has caught
            up past its end (+2 min) and 10 min have passed -- every window exactly
            once. If a lookup fails it waits; with 3+ waiting windows we pause quoting
            there (the seat is kept, and paused time counts as NOT checkable). A window
            containing an order match that can't be judged (rows missing) is NOT
            checkable: worst case at once. A market is settled only once all its
            windows are checked. Longest-waiting markets are checked first; checks stop
            6 min after the run started. Pauses / 6 h+ waits make the run RED.
  Fills     judged on every individual MAKER fill, at its own price (the taker's
            row shows an average price, which hides sweeps). Two scorebooks:
              STRICT  only maker fills strictly BELOW our bid fill us;
              TOUCH   fills AT our price count too (we may have been first in line).
            Fill size = those fills added up, capped by what our order had left.
            If the book at the window's end has REACHED or CROSSED our bid (an ask at or
            below it), the rest of our order is filled and the window earns no rewards;
            if that book can't be read, the window is given up (worst case). In STRICT,
            fills at our price also count when the same match traded below it.
            The test passes only if BOTH scorebooks pass.
  Rewards   our share = our score / (our score + others' score), Polymarket's formula
            with the 1/3 one-sided penalty (only for middle prices 10-90%). Credited
            for at most 60 minutes per window (so with 2-hour cycles, only half the
            time earns anything -- on purpose, it can only understate). Per window:
            the SMALLER pot and the LARGER competitor score of its start and end;
            paid only until the first fill; scaled down to the LOWEST score our same
            orders would have at the end price or any price traded before the first
            fill. A market that left the rewarded list earns $0 for it.
  Never checkable (24 h after its end, a match in it can't be judged, its end book
            can't be read, or it is over 6 h long because runs stopped): $0 rewards,
            and its Yes side and No side are each booked as "fully filled" or "not
            filled", whichever is WORSE (never a profit); counted as unverified.
  Value     as if SOLD NOW: a Yes+No pair = $1; the rest (incl. worst-case items) sold
            TOGETHER into the real bids, level by level, minus a 7.2% taker fee rate
            (the highest); no bids = $0.
            Settled only on a final 1 / 0 / 50-50 result (not while disputed).
  PASS after 14+ days, ALL of these, in BOTH scorebooks (sold-now values):
     1. rewards + trading profit/loss > 0
     2. positive in BOTH weeks (first 7 days / the rest; day 7 read from every
        cleanly valued run within 6 h of it, taking the least favourable)
     3. still positive if rewards were only HALF (competition may be bigger)
     and no more than 2% of quoting time not checkable -- otherwise UNRELIABLE.
  Not counted: maker rebates on fills (a small extra) and the $1 minimum payout.
  Known limits: the reward share is an estimate from snapshots -- only a tiny real
  test can confirm it. Version-1 results (16-18 Oct) are archived as *_v1_* files.
"""
import argparse
import csv
import itertools
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pm_rewards_scanner as rs
import pm_trades_api as ta

HERE = Path(__file__).resolve().parent
STATE = HERE / "pm_mm_state.json"
CYCLES_CSV = HERE / "pm_mm_log.csv"
FILLS_CSV = HERE / "pm_mm_fills.csv"
GAMMA_ONE = "https://gamma-api.polymarket.com/markets/{}"
N_MARKETS = 20
BUDGET = 100.0
INV_CAP = 100.0           # shares
MAX_CREDIT_S = 3600
MIN_DAYS_LEFT = 2
VERSION = 2
LAG_S = 600               # a window is checked only once it ended at least this long ago
FRESH_MARGIN_S = 120      # ... and the trade feed has caught up this far past its end
MAX_BACKLOG = 3           # this many waiting windows -> pause quoting that market
GIVE_UP_H = 24            # a window still uncheckable this long after it ended is given up
DEADLINE_S = 360          # stop checking windows 6 min after the PROCESS started (they wait)
SETTLE_DEADLINE_S = 450   # ... and stop result lookups after 7.5 min (GitHub kills at 10)
STALE_S = 7200            # trade data 2 h+ behind the clock (busiest markets) = unhealthy
SELL_FEE = 0.072          # taker fee rate when cashing out (the highest rate, to be safe)
BOOKS = ("strict", "touch")
CYCLE_FIELDS = ["time", "seconds", "valued_ok", "active", "paused", "holding", "windows_waiting",
                "lookups_failed", "not_checked_out_of_time", "windows_checked", "windows_given_up",
                "market_days", "unverified_days", "paused_days"]
for _b in BOOKS:
    CYCLE_FIELDS += [f"{_b}_rewards", f"{_b}_pnl_sold", f"{_b}_net", f"{_b}_half_net", f"{_b}_fills"]
FILL_FIELDS = ["time", "book", "market_id", "question", "side", "price", "shares", "trade_time"]


# ---------------------------------------------------------------------------
def now_ts():
    return int(datetime.now(timezone.utc).timestamp())


def new_book():
    return {"yes_sh": 0.0, "no_sh": 0.0, "cost": 0.0, "rewards": 0.0, "fills": 0, "pnl": None}


def fresh_state():
    return {"version": VERSION, "started": now_ts(), "markets": {}, "market_days": 0.0,
            "unverified": 0, "unverified_days": 0.0, "paused_days": 0.0,
            "totals": {b: {"rewards": 0.0, "realized": 0.0, "fills": 0} for b in BOOKS}}


def archive_old():
    tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    for p in (STATE, CYCLES_CSV, FILLS_CSV):
        if p.exists():
            p.replace(p.with_name(f"{p.stem}_v1_{tag}{p.suffix}"))


def load_state():
    if not STATE.exists():
        if CYCLES_CSV.exists() or FILLS_CSV.exists():   # logs without a state: start clean
            archive_old()
            print("  Old logs archived as *_v1_*. The 14-day test starts now.")
        return fresh_state()
    try:
        s = json.loads(STATE.read_text())
    except json.JSONDecodeError:
        raise SystemExit(f"  {STATE.name} is damaged -- not overwriting it. Tell Claude.")
    if s.get("version") != VERSION or "totals" not in s:   # older, optimistic rules
        archive_old()
        print("  Old results archived as *_v1_*. The 14-day test restarts now.")
        return fresh_state()
    for k, v in fresh_state().items():                 # keys added later in development
        if k != "started":
            s.setdefault(k, v)
    return s


def save_state(s):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(STATE)


def append(path, fields, rows):
    """Add rows to a CSV crash-safely: the new file is written next to it and swapped in
    whole, so a run cut off mid-write can never leave a broken line behind."""
    old = []
    if path.exists() and path.stat().st_size > 0:
        with path.open(newline="") as f:
            old = list(csv.DictReader(f))
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(old + list(rows))
    tmp.replace(path)


# ---------------------------------------------------------------------------
def trades_between(mk, start, end, max_pages, deadline, vol_total):
    """(matches, bad_times) in (start, end], or None if unsure. See pm_trades_api.py."""
    return ta.trades_between(rs.get_json, mk["cid"], start, end, log=rs.log, pause=rs.PAUSE,
                             tokens=(mk["ty"], mk["tn"]), max_pages=max_pages,
                             allow_empty=vol_total == 0, deadline=deadline)


def sell_value(shares, bid_levels, rate=SELL_FEE):
    """Cash from selling `shares` into real bids, best price first, minus the taker fee
    (nothing left to sell into = $0)."""
    got, left = 0.0, shares
    for p, sz in sorted(bid_levels or [], reverse=True):
        take = min(left, sz)
        got += take * (p - rate * p * (1 - p))
        left -= take
        if left <= 1e-9:
            break
    return got


def book_bids(yes_book):
    """Bid levels for Yes, and for No (= 1 - the Yes asks, the same orders mirrored)."""
    yb = [[p, s] for p, s in rs.levels(yes_book, "bids") if s > 0]
    nb = [[round(1 - p, 4), s] for p, s in rs.levels(yes_book, "asks") if s > 0]
    return sorted(yb, reverse=True)[:15], sorted(nb, reverse=True)[:15]


def resolution(mkt_id):
    """1 / 0 / 0.5 once the market is FINALLY settled; None otherwise (open, disputed,
    pending, or unclear)."""
    m = rs.get_json(GAMMA_ONE.format(mkt_id))
    if not isinstance(m, dict) or not m.get("closed"):
        return None
    status = m.get("umaResolutionStatus")
    if status is not None and str(status).lower() != "resolved":
        return None
    try:
        px = [float(x) for x in rs.as_list(m.get("outcomePrices"))]
    except (TypeError, ValueError):
        return None
    if len(px) != 2 or abs(px[0] + px[1] - 1) > 0.01:
        return None                                    # e.g. ["0","0"]: not a real result
    y = px[0]
    for final in (1.0, 0.0, 0.5):
        if abs(y - final) < 1e-6:
            return final
    return None


def worst_final(w, res):
    """A given-up window at settlement: its Yes side and its No side may each have been
    fully filled or not at all -- each side is counted at whichever is WORSE (so it can
    never add a profit)."""
    loss = 0.0
    if w["yb"]:
        loss += min(0.0, w["ys"] * res - w["ys"] * w["yb"])
    if w["nb"]:
        loss += min(0.0, w["ns"] * (1 - res) - w["ns"] * w["nb"])
    return loss


def side_worst(base, bids, extra):
    """The worst sold-now value of one side: `base` shares already held to sell, plus ANY
    subset of the worst-case items' fills on this side (each (shares, price paid)).
    Returns sale value minus the extra cost, minimised over the subsets (small counts:
    every subset; many: a floor that is never above the true worst)."""
    if not extra:
        return sell_value(base, bids)
    if len(extra) <= 14:
        best = None
        for pick in itertools.product((False, True), repeat=len(extra)):
            sh = base + sum(e[0] for e, p in zip(extra, pick) if p)
            c = sum(e[0] * e[1] for e, p in zip(extra, pick) if p)
            v = sell_value(sh, bids) - c
            best = v if best is None else min(best, v)
        return best
    # many items: a floor that is never above the true worst. Sale value is concave in
    # shares, so every extra share sells for at least the average price a of selling all
    # of them: value(S) >= f(base) + sum over S of (a*shares - cost) >= f(base) + sum of
    # min(0, a*shares - cost)
    f0 = sell_value(base, bids)
    tot = sum(e[0] for e in extra)
    a = (sell_value(base + tot, bids) - f0) / tot if tot else 0.0
    return f0 + sum(min(0.0, a * e[0] - e[0] * e[1]) for e in extra)


def sold_now(mk, b):
    """Profit/loss of one scorebook in one market if everything were sold right now.
    Everything to sell on one side is sold TOGETHER into the same bids (so the same
    buyers are never counted twice). Each worst-case item may have filled its Yes side,
    its No side, both or neither -- each side is taken at its WORST (never a profit)."""
    bk = mk["b"][b]
    ylv, nlv = mk.get("yb_lv") or [], mk.get("nb_lv") or []
    pairs = min(bk["yes_sh"], bk["no_sh"])            # a Yes+No pair always pays $1
    items = mk.get("worst", [])
    ex_y = [(w["ys"], w["yb"]) for w in items if w["yb"]]
    ex_n = [(w["ns"], w["nb"]) for w in items if w["nb"]]
    return (pairs + side_worst(bk["yes_sh"] - pairs, ylv, ex_y)
            + side_worst(bk["no_sh"] - pairs, nlv, ex_n) - bk["cost"])


def candidates(pre=None):
    """Eligible rewarded markets with step-1 estimates, best first. `pre` = (markets,
    books) already read by the scanner in this same run (saves reading them twice)."""
    t0 = datetime.now(timezone.utc)
    if pre:
        mkts, books = pre
    else:
        mkts = rs.rewarded_markets()
        books = rs.books_for([str(t) for m, _ in mkts for t in rs.as_list(m.get("clobTokenIds"))])
    out = []
    for m, pot in mkts:
        ty, tn = [str(t) for t in rs.as_list(m.get("clobTokenIds"))]
        if books.get(ty) is None or books.get(tn) is None:
            continue                      # a book we couldn't read is NOT an empty book
        e = rs.evaluate(m, pot, books.get(ty), books.get(tn))
        if not e or not m.get("conditionId"):
            continue
        try:
            end = datetime.fromisoformat(str(m.get("endDate")).replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            days_left = (end - t0).total_seconds() / 86400
        except (ValueError, TypeError):
            continue
        if days_left < MIN_DAYS_LEFT or e["others"][1] <= 0 or e["relaxed"][0] < e["min_size"]:
            continue
        ylv, nlv = book_bids(books.get(ty))
        out.append({"id": str(m.get("id")), "cid": m.get("conditionId"), "pot": pot, "e": e,
                    "vol": rs.num(m.get("volume24hr")),
                    "vol_total": rs.num(m.get("volumeNum") or m.get("volume"), 1.0),
                    "q": str(m.get("question") or "")[:90], "ty": ty, "tn": tn,
                    "est": e["relaxed"][1][1], "yb_lv": ylv, "nb_lv": nlv})
    out.sort(key=lambda c: -c["est"])
    return out, len(mkts)


def refresh_prices(s, by_id, late=False):
    """Fresh bid levels for every unsettled market we hold (empty book / failed read = $0).
    Returns True only if every held market was valued from a book read THIS run (a $0
    from a failed read or from running late is pessimistic, but must not be used as the
    week-1 reference, or week 2 would look better)."""
    held = [(k, mk) for k, mk in s["markets"].items() if mk.get("resolved") is None
            and (mk.get("worst") or any(mk["b"][b]["yes_sh"] or mk["b"][b]["no_sh"] for b in BOOKS))]
    for k, mk in held:
        if k in by_id:
            mk["yb_lv"], mk["nb_lv"] = by_id[k]["yb_lv"], by_id[k]["nb_lv"]
    rest = [(k, mk) for k, mk in held if k not in by_id and not mk.get("closed")]
    for k, mk in held:
        if mk.get("closed") or (late and k not in by_id):
            mk["yb_lv"], mk["nb_lv"] = [], []     # closed / out of time: valued at $0
    if late:
        return not rest
    if not rest:
        return True
    books = rs.books_for([mk["ty"] for _, mk in rest])
    for k, mk in rest:
        mk["yb_lv"], mk["nb_lv"] = book_bids(books.get(mk["ty"]))
    return all(mk["ty"] in books for _, mk in rest)


# ---------------------------------------------------------------------------
def buy(bk, side, px, f):
    bk["yes_sh" if side == "YES" else "no_sh"] += f
    bk["cost"] += f * px
    bk["fills"] += 1


def window_rate(w):
    """Reward per day for this window: the SMALLER pot and the LARGER competitor score of
    the window's start and end ($0 if the market left the rewarded list)."""
    if w.get("mid_end") is None or not w["score0"]:
        return 0.0
    pot = min(w["pot0"], w.get("pot_end", w["pot0"]))
    others = max(w["others0"], w.get("others_end", w["others0"]))
    return pot * w["score0"] / (w["score0"] + others)


def check_window(bk, w, trades, touch):
    """Apply one checked window to one scorebook. Returns (fills, reward)."""
    fills, first_fill, prices = [], None, []
    yl, nl = w["ys"], w["ns"]
    eps = -1e-9 if touch else 1e-9            # touch: AT our price counts too
    for ts, fl, _tx, _complete in trades:
        hit = False
        # STRICT: fills AT our price count too when the same match also traded BELOW it
        # (then the whole queue at our price was used up, ours included)
        e_y = e_n = eps
        if touch or (w["yb"] and any(yp < w["yb"] - 1e-9 for yp, _ in fl)):
            e_y = -1e-9
        if touch or (w["nb"] and any((1 - yp) < w["nb"] - 1e-9 for yp, _ in fl)):
            e_n = -1e-9
        if w["yb"] and yl > 0:
            through = sum(sh for yp, sh in fl if yp < w["yb"] - e_y)       # Yes at/below bid
            if through > 0:
                f = min(through, yl)
                yl -= f
                buy(bk, "YES", w["yb"], f)
                fills.append(("YES", w["yb"], f, ts))
                hit = True
        if w["nb"] and nl > 0:
            through = sum(sh for yp, sh in fl if (1 - yp) < w["nb"] - e_n)  # No at/below bid
            if through > 0:
                f = min(through, nl)
                nl -= f
                buy(bk, "NO", w["nb"], f)
                fills.append(("NO", w["nb"], f, ts))
                hit = True
        if hit and first_fill is None:
            first_fill = ts
        if first_fill is None:
            prices += [yp for yp, _ in fl]     # prices traded BEFORE the first fill
    # the book at the window's end reached or CROSSED our bid (an ask at or below it --
    # which could not rest next to our bid -- e.g. the
    # other bids were cancelled rather than traded): a real resting bid would have been
    # filled -- the rest of our order is filled, and the window earns no rewards at all
    crossed = False
    if w["yb"] and yl > 0 and w.get("ask_end") is not None and w["ask_end"] <= w["yb"] + 1e-9:
        buy(bk, "YES", w["yb"], yl)
        fills.append(("YES", w["yb"], yl, w["end"]))
        crossed = True
    if w["nb"] and nl > 0 and w.get("bid_end") is not None and w["bid_end"] >= 1 - w["nb"] - 1e-9:
        buy(bk, "NO", w["nb"], nl)
        fills.append(("NO", w["nb"], nl, w["end"]))
        crossed = True
    if crossed:
        first_fill = w["start"]
    length = min(w["end"] - w["start"], MAX_CREDIT_S)
    paid = length if first_fill is None else max(0, min(length, first_fill - w["start"]))
    rate = window_rate(w)
    if rate > 0 and paid > 0:
        worst = min(rs.two_sided_score(m_, w["v"], w["min_size"], w["yb"], w["ys"],
                                       w["nb"], w["ns"]) for m_ in [w["mid_end"]] + prices)
        paid *= max(0.0, min(1.0, worst / w["score0"]))
    reward = rate * paid / 86400
    bk["rewards"] += reward
    return fills, reward


def give_up(mk, w):
    """A window never checkable: $0 rewards, and it is kept as a WORST-CASE item -- valued
    every cycle as the worse of 'Yes bid fully filled' / 'No bid fully filled', and settled
    as whichever of the two LOST more (applies to both scorebooks)."""
    mk.setdefault("worst", []).append({k: w[k] for k in ("yb", "ys", "nb", "ns")})


def cycle(pre=None, started=None):
    clock = started or time.time()        # deadlines count from the PROCESS start
    s = load_state()
    print(f"\n  [{datetime.now():%H:%M}] Paper market-maker cycle "
          f"({len(s['markets'])} markets tracked)...")
    cands, n_listed = candidates(pre)     # today's books are read BEFORE the clock time t
    by_id = {c["id"]: c for c in cands}
    t = now_ts()
    busy = [c["cid"] for c in sorted(cands, key=lambda c: -c["vol"])[:3]] or \
        [mk["cid"] for mk in s["markets"].values() if mk.get("resolved") is None][:3]
    fresh = ta.newest_trade_time(rs.get_json, busy, log=rs.log) if busy else None
    stale = fresh is not None and t - fresh > STALE_S
    cutoff = min(t - LAG_S, fresh - FRESH_MARGIN_S) if fresh else None
    given_up = checked_windows = 0
    gap = t - s["last_t"] if s.get("last_t") else 0
    fill_rows, failed, skipped = [], 0, 0

    # 1) close every live quote into a window (with the book at its end)
    gone = [mk["ty"] for k, mk in s["markets"].items()
            if mk.get("resolved") is None and mk.get("live") and k not in by_id]
    gone_books = rs.books_for(gone) if gone else {}
    no_book = 0
    for k, mk in s["markets"].items():
        if mk.get("resolved") is None and mk.get("live"):
            w = mk["live"]
            w["end"] = t
            c = by_id.get(k)
            w["mid_end"] = c["e"]["mid"] if c else None
            if c:
                w["pot_end"], w["others_end"] = c["pot"], c["e"]["others"][1]
                ylv, nlv = c["yb_lv"], c["nb_lv"]
            else:
                if mk["ty"] not in gone_books:
                    no_book += 1                  # crossing can't be checked: this window
                    w["no_end_book"] = True       # is given up (worst case), not guessed
                ylv, nlv = book_bids(gone_books.get(mk["ty"]))
            if w["end"] - w["start"] > 6 * 3600:
                w["too_long"] = True          # the runs stopped for a while: a window this
                                              # long can't be read reliably -> worst case
            w["bid_end"] = max((p for p, _ in ylv), default=None)        # best Yes bid
            w["ask_end"] = (1 - max(p for p, _ in nlv)) if nlv else None  # best Yes ask
            mk["queue"].append(w)
            mk["live"] = None

    # 2) check waiting windows -- longest-waiting markets first, ONE lookup per market
    order = sorted((kv for kv in s["markets"].items() if kv[1].get("resolved") is None),
                   key=lambda kv: min([w["end"] for w in kv[1]["queue"]] or [1e18]))
    for k, mk in order:
        for w in [w for w in mk["queue"] if w.get("too_long") or w.get("no_end_book")]:
            give_up(mk, w)                # not checkable: worst case at once, and it must not
            given_up += 1                 # hold up the windows after it
            s["unverified"] += 1
            s["unverified_days"] += min(w["end"] - w["start"], MAX_CREDIT_S) / 86400
            rs.log(f"market {k}: window ending {w['end']} not checkable "
                   f"({'too long' if w.get('too_long') else 'no end book'}) -- worst case")
            mk["queue"].remove(w)
        ready = [w for w in mk["queue"] if cutoff is not None and w["end"] <= cutoff]
        got = None
        if ready and time.time() - clock <= DEADLINE_S:
            deep = ready[0]["end"] < t - 12 * 3600          # close to the 24 h give-up:
            got = trades_between(mk, ready[0]["start"], ready[-1]["end"],   # read deeper
                                 max_pages=120 if deep else ta.MAX_PAGES,
                                 deadline=clock + DEADLINE_S + 60,
                                 vol_total=(by_id[k]["vol_total"] if k in by_id
                                            else mk.get("vol_total", 1)))
            time.sleep(rs.PAUSE)
            if got is None:
                failed += 1
        elif ready:
            skipped += 1                  # out of time this run: they wait
        if ready and got is not None:
            trades, bad = got
            for w in ready:
                if any(w["start"] < b <= w["end"] for b in bad):
                    # a match in this window can't be judged: THIS window only is not
                    # checkable -> worst case, $0 rewards, counted as unverified
                    give_up(mk, w)
                    given_up += 1
                    s["unverified"] += 1
                    s["unverified_days"] += min(w["end"] - w["start"], MAX_CREDIT_S) / 86400
                    rs.log(f"market {k}: a match that can't be judged in window ending "
                           f"{w['end']} -- worst case booked")
                    mk["queue"].remove(w)
                    continue
                sub = [x for x in trades if w["start"] < x[0] <= w["end"]]
                for b in BOOKS:
                    fills, reward = check_window(mk["b"][b], w, sub, touch=(b == "touch"))
                    s["totals"][b]["rewards"] += reward
                    s["totals"][b]["fills"] += len(fills)
                    for side, px, f, ts in fills:
                        fill_rows.append({"book": b, "market_id": k, "question": mk["q"],
                                          "side": side, "price": px, "shares": round(f, 2),
                                          "trade_time": datetime.fromtimestamp(ts, timezone.utc)
                                          .strftime("%Y-%m-%d %H:%M")})
                s["market_days"] += min(w["end"] - w["start"], MAX_CREDIT_S) / 86400
                checked_windows += 1
                mk["queue"].remove(w)
        # windows that can never be checked (24 h+): worst case, counted as unverified
        for w in [w for w in mk["queue"] if w["end"] < t - GIVE_UP_H * 3600]:
            give_up(mk, w)
            given_up += 1
            s["unverified"] += 1
            s["unverified_days"] += min(w["end"] - w["start"], MAX_CREDIT_S) / 86400
            rs.log(f"market {k}: window ending {w['end']} never checkable -- worst case booked")
            mk["queue"].remove(w)
        # 3) settle -- only once every window is checked, and only on a FINAL result.
        #    A market holding nothing with nothing waiting can't change any result: skip it.
        holds = mk.get("worst") or any(mk["b"][b]["yes_sh"] or mk["b"][b]["no_sh"] for b in BOOKS)
        if not holds and not mk["queue"]:
            mk["resolved"] = mk.get("resolved")       # stays unsettled; costs no request
            res = None
        elif time.time() - clock <= SETTLE_DEADLINE_S:
            res = resolution(k)
            time.sleep(rs.PAUSE)
        else:
            res = None                    # out of time: settle next run
        if res is not None:
            mk["closed"] = True
            if not mk["queue"]:
                mk["resolved"] = res
                for b in BOOKS:
                    bk = mk["b"][b]
                    bk["pnl"] = (bk["yes_sh"] * res + bk["no_sh"] * (1 - res) - bk["cost"]
                                 + sum(worst_final(w, res) for w in mk.get("worst", [])))
                    s["totals"][b]["realized"] += bk["pnl"]
        save_state(s)                     # progress is kept even if the run is cut off

    # 4) new quotes: markets we were in first (paused ones keep their seat), up to 20.
    #    No candidates at all (the listing failed) -> no quotes, and seats stay as they are.
    def backlog(mk):                      # windows that COULD have been checked but weren't
        return sum(1 for w in mk["queue"] if w["end"] < t)

    def can_quote(k):
        mk = s["markets"].get(k)
        return mk is None or (mk.get("resolved") is None and not mk.get("closed")
                              and backlog(mk) < MAX_BACKLOG)
    keep_seats = not cands
    was_in = [c for c in cands if c["id"] in s["markets"] and s["markets"][c["id"]].get("was_in")]
    rest = [c for c in cands if c not in was_in]
    chosen, paused, seats = [], [], 0
    for c in was_in + rest:
        if seats >= N_MARKETS:
            break
        if can_quote(c["id"]):
            chosen.append(c)
            seats += 1
        elif c in was_in and s["markets"][c["id"]].get("resolved") is None \
                and not s["markets"][c["id"]].get("closed"):
            paused.append(c["id"])
            seats += 1
    if not keep_seats:
        for mk in s["markets"].values():
            mk["was_in"] = False
        for k in paused:
            s["markets"][k]["was_in"] = True
    s["paused_days"] += len(paused) * gap / 86400
    s["last_t"] = t
    for c in chosen:
        mk = s["markets"].setdefault(c["id"], {
            "cid": c["cid"], "q": c["q"], "ty": c["ty"], "tn": c["tn"], "resolved": None,
            "joined": t, "queue": [], "live": None, "b": {b: new_book() for b in BOOKS}})
        mk["vol_total"] = c["vol_total"]
        e = c["e"]
        mid, v, min_size = e["mid"], e["v"], e["min_size"]
        yb, nb, shares, _ = rs.our_quote(mid, v, min_size, v / 2, BUDGET)
        net = max(mk["b"][b]["yes_sh"] - mk["b"][b]["no_sh"] for b in BOOKS)
        neg = max(mk["b"][b]["no_sh"] - mk["b"][b]["yes_sh"] for b in BOOKS)
        ys = 0.0 if net >= INV_CAP or yb <= 0 else shares
        ns = 0.0 if neg >= INV_CAP or nb <= 0 else shares
        ours = rs.two_sided_score(mid, v, min_size, yb, ys, nb, ns)
        mk["live"] = {"start": t, "yb": yb if ys else None, "nb": nb if ns else None,
                      "ys": ys, "ns": ns, "v": v, "min_size": min_size, "score0": ours,
                      "pot0": c["pot"], "others0": e["others"][1]}
        mk["was_in"] = True

    # 5) value everything at what it could be SOLD for now
    valued_ok = refresh_prices(s, by_id, late=time.time() - clock > SETTLE_DEADLINE_S + 60)
    open_mk = [mk for mk in s["markets"].values() if mk.get("resolved") is None]
    row = {"time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "active": len(chosen),
           "seconds": round(time.time() - clock), "windows_checked": checked_windows,
           "valued_ok": int(bool(valued_ok)),
           "windows_given_up": given_up,
           "paused": len(paused),
           "holding": sum(1 for mk in open_mk
                          if any(mk["b"][b]["yes_sh"] or mk["b"][b]["no_sh"] for b in BOOKS)),
           "windows_waiting": sum(len(mk["queue"]) for mk in open_mk),
           "lookups_failed": failed, "not_checked_out_of_time": skipped,
           "market_days": round(s["market_days"], 4),
           "unverified_days": round(s["unverified_days"], 4),
           "paused_days": round(s["paused_days"], 4)}
    for b in BOOKS:
        tot = s["totals"][b]
        pnl_sold = tot["realized"] + sum(sold_now(mk, b) for mk in open_mk)
        row.update({f"{b}_rewards": round(tot["rewards"], 2), f"{b}_pnl_sold": round(pnl_sold, 2),
                    f"{b}_net": round(tot["rewards"] + pnl_sold, 2),
                    f"{b}_half_net": round(tot["rewards"] / 2 + pnl_sold, 2),
                    f"{b}_fills": tot["fills"]})
    save_state(s)
    append(FILLS_CSV, FILL_FIELDS, [{"time": row["time"], **r} for r in fill_rows])
    append(CYCLES_CSV, CYCLE_FIELDS, [row])

    W = 78
    print("=" * W)
    print(f" #34 STEP 2 PAPER MARKET-MAKER  |  {datetime.now():%Y-%m-%d %H:%M}  |  no real money")
    print("=" * W)
    print(f" Running {(t - s['started']) / 86400:.1f} days   quoting in {len(chosen)} markets"
          f" ($100 each), {len(paused)} paused   new fills checked: {len(fill_rows)}")
    for b in BOOKS:
        print(f" {b.upper():<7} rewards ${row[f'{b}_rewards']:>7.2f}   P/L if sold now "
              f"${row[f'{b}_pnl_sold']:>8.2f}   NET ${row[f'{b}_net']:>8.2f}")
    if stale:
        print(f" WARNING: trade data is {(t - fresh) / 60:.0f} min behind the clock (unhealthy).")
    lost_share = given_up / (given_up + checked_windows) if given_up + checked_windows else 0.0
    if given_up:
        print(f" Not checkable this run: {given_up} window(s) of {given_up + checked_windows}"
              f" ({lost_share:.0%}) -- worst case booked (RED if over 10%).")
    if no_book:
        print(f" {no_book} order book(s) couldn't be read at a window's end -- those windows"
              f" are given up (worst case).")
    if failed or skipped or cutoff is None:
        print(f" WARNING: {failed} lookup(s) failed, {skipped} market(s) out of time"
              + (", trade feed freshness UNKNOWN (nothing checked)" if cutoff is None else "")
              + " -- those windows wait for the next run.")
    print(f" Saved: {CYCLES_CSV.name}, {FILLS_CSV.name}.  Results: python pm_mm_paper.py --summary")
    print("=" * W)
    # a broken run must show up RED on GitHub (results above are saved either way)
    old_wait = any(w["end"] < t - 6 * 3600 for mk in open_mk for w in mk["queue"])
    if paused or old_wait:
        print(f" WARNING: {len(paused)} market(s) paused, windows waiting 6 h+: {old_wait} (unhealthy).")
    if (n_listed == 0 or cutoff is None or stale or lost_share > 0.10 or paused or old_wait
            or rs.failure_share() > 0.2):
        rs.log(f"market maker: unhealthy run (listed {n_listed}, fresh {fresh}, stale {stale}, "
               f"given up {given_up}, {rs.failure_share():.0%} requests failed)")
        sys.exit(1)


def summary():
    if not STATE.exists():
        print("  Nothing yet -- run: python pm_hourly.py")
        return
    s = json.loads(STATE.read_text())
    cyc = []
    if CYCLES_CSV.exists():
        with CYCLES_CSV.open(newline="") as f:
            cyc = [c for c in csv.DictReader(f) if c.get("strict_net") not in (None, "")]
    W = 78
    print("=" * W)
    print(" #34 STEP 2 -- PAPER MARKET-MAKING SUMMARY (version 2, sold-now values)")
    print("=" * W)
    if not cyc or s.get("version") != VERSION or "totals" not in s:
        print("  No version-2 cycles yet.")
        return
    last = cyc[-1]
    t0 = s["started"]
    stamp = lambda c: datetime.strptime(c["time"], "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone.utc).timestamp()
    days = (stamp(last) - t0) / 86400
    md = float(last["market_days"])
    unv_d, pz = s.get("unverified_days", 0.0), s.get("paused_days", 0.0)
    total = md + unv_d + pz
    share = (unv_d + pz) / total if total else 1.0
    fails = sum(int(c.get("lookups_failed") or 0) for c in cyc)
    print(f" {days:.1f} days, {len(cyc)} cycles, {len(s['markets'])} markets joined, "
          f"{fails} failed lookups, {last['windows_waiting']} windows waiting")
    print(f" Not checkable: {s.get('unverified', 0)} window(s) given up (worst case booked) +"
          f" {pz * 24:.0f} market-hours paused = {share:.1%} of quoting time")
    # week 1 = the result at day 7, read ROBUSTLY: every cleanly valued run within 6 h of
    # day 7 counts; week 1 must be positive at its LOWEST and week 2 (final - week 1) at
    # its lowest too -- one odd reading can't make either week look better
    near = [c for c in cyc if abs(stamp(c) - t0 - 7 * 86400) <= 6 * 3600
            and str(c.get("valued_ok", "1")) == "1"]
    wk = near if near and days >= 7.25 else None
    all_ok = True
    for b in BOOKS:
        net, half = float(last[f"{b}_net"]), float(last[f"{b}_half_net"])
        w1s = [float(c[f"{b}_net"]) for c in wk] if wk else []
        w1 = min(w1s) if w1s else net
        w2 = net - max(w1s) if w1s else float("nan")
        checks = [(f"1. Net profit > 0 (${net:.2f})", net > 0),
                  (f"2. Both weeks positive (${w1:.2f} / " + (f"${w2:.2f})" if wk else "not yet)"),
                   wk is not None and w1 > 0 and w2 > 0),
                  (f"3. Still positive with HALF the rewards (${half:.2f})", half > 0)]
        print(f"\n {b.upper()} scorebook: rewards ${float(last[f'{b}_rewards']):.2f}, P/L if sold now"
              f" ${float(last[f'{b}_pnl_sold']):.2f}, {last[f'{b}_fills']} fills"
              + (f", ${net / md:.2f} per $100 per credited day" if md else ""))
        for text, ok in checks:
            print(f"   [{'PASS' if ok else 'FAIL'}] {text}")
        all_ok = all_ok and all(ok for _, ok in checks)
    rows = []
    for mk in s["markets"].values():
        bk = mk["b"]["strict"]
        pl = bk["pnl"] if mk.get("resolved") is not None else sold_now(mk, "strict")
        rows.append((bk["rewards"] + pl, bk["rewards"], pl, bk["fills"], mk["q"]))
    print(f"\n {'net':>8}{'rewards':>9}{'P/L':>9}{'fills':>6}  market (STRICT, P/L = if sold now)")
    srt = sorted(rows, reverse=True)
    for r in (srt if len(srt) <= 13 else srt[:8] + [None] + srt[-5:]):
        print("   ..." if r is None else f" {r[0]:>8.2f}{r[1]:>9.2f}{r[2]:>9.2f}{r[3]:>6}  {r[4][:44]}")
    waiting = sum(1 for mk in s["markets"].values() if mk.get("resolved") is None
                  for w in mk.get("queue", []) if w["end"] < s.get("last_t", 0))
    if days < 14:
        status = "too early"
    elif waiting or int(last.get("lookups_failed") or 0):
        status = f"PENDING ({waiting} window(s) not checked yet) -- look again after the next run"
    elif share > 0.02:
        status = "UNRELIABLE (more than 2% could not be checked) -- no verdict"
    else:
        status = "PASS" if all_ok else "NOT a pass"
    print(f"\n VERDICT (both scorebooks must pass, 14+ days): {status}  ({days:.1f} of 14 days)")
    try:
        import pm_minutes
        avg = pm_minutes.average_seconds()
        print(f" GitHub minutes this month: about {pm_minutes.used_this_month()} of"
              f" {pm_minutes.MONTH_LIMIT}" + (f"; a run takes {avg:.0f} s on average"
                                              + (" -- SLOW, tell Claude" if avg > 240 else "")
                                              if avg else ""))
    except Exception:
        pass
    print("=" * W)


def main():
    ap = argparse.ArgumentParser(description="#34 step 2: paper market-making (no real orders)")
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args()
    if a.summary:
        summary()
    else:
        cycle()


if __name__ == "__main__":
    main()
