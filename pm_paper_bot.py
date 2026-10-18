"""
pm_paper_bot.py  --  PAPER trading for #35b on Polymarket (the live test)
==========================================================================
Runs twice a day on GitHub. NO wallet, NO keys, NO real bets -- it only READS public
Polymarket data and writes down what it WOULD have done, at REAL prices.

THE STRATEGY (locked)
  #35b MENTION MARKETS, NOT Trump: an open "Will X say <word>?" market (Polymarket's
       "mention-markets" label) whose event and question don't mention Trump, "Yes"
       between 10% and 90% -> buy "No". (Backtest #35: "Yes" overpriced 3-5 points,
       but costs ate it; non-Trump looked best -- found AFTER the test, so it must prove
       itself here, on new markets. The MAKER line is the one that matters.)
       Runs twice a day, so bets land 0-12 h before the speech.
  #22, #26 and #32 are RETIRED (they failed honest backtests): no new bets. Their old
  rows stay in the file, untouched and not scored.

TWO PAPER BETS OF $10 PER CANDIDATE (one candidate per market, ever)
  TAKER  buy "No" now into the real ASKS, level by level, plus the taker fee (at least
         Polymarket's Mentions rate, or the highest rate of any label; if the fee is
         taken in shares instead, the smaller share count is used). If the book can't
         take $10, no taker bet.
  MAKER  a limit order to buy "No" 1 cent above the best bid (on the price grid,
         always below the ask), no fee. It fills ONLY from real MAKER fills priced
         strictly BELOW our limit after we placed it (each fill at its own price --
         the taker's row shows an average and hides sweeps). Fill size = those fills
         added up, capped at our order: a 1-share trade fills 1 share, not $10.
         Each run re-reads the last 6 hours too (trades can reach the data late),
         reads only up to where the trade data has caught up (judged on the busiest
         markets); every fill is counted once. A match with missing rows can't be
         judged: the bet is then settled on the WORSE of "fills seen" and "fully
         filled". If the trades can't be read at all, the bet waits -- and the run
         shows RED.
SETTLING
  Only on a FINAL result (1 / 0 / 50-50; not while disputed). A maker bet is settled
  only after one more successful check past the market's end, so last-minute fills
  can't be missed. Never filled = "expired" (no profit, no loss). Fills never
  checkable in 10 tries after the final result = "unverified"; no final result 14 days after
  the end date = "no result". Both are left out of the score and COUNTED, and a
  WORST-CASE line treats each of them as a full $10 loss.
SCORE
  Only finished bets. Careful: markets often finish EARLY when the answer is Yes
  (our losses), so early scores look worse than the final one will.

    python pm_paper_bot.py          (GitHub: about twice a day -- at most once per 11 h)
    python pm_paper_bot.py --force  (run now, even if it ran less than 11 h ago)
Saves pm_paper_bets.csv (+ pm_paper_fills.json: which fills were already counted).
"""
import csv
import json
import math
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pm_minutes
import pm_trades_api as ta

HERE = Path(__file__).resolve().parent
BETS = HERE / "pm_paper_bets.csv"
SEEN = HERE / "pm_paper_fills.json"
LOG = HERE / "pm_paper_log.txt"
GAMMA = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
CLOB = "https://clob.polymarket.com"
STAKE = 10.0
MAKER_IMPROVE = 0.01
R35 = {"lo": 0.10, "hi": 0.90, "exclude": "trump"}
# Taker fee = shares x rate x p x (1-p). Polymarket's published rates (docs, Oct 2026):
# Mentions 0.04, Sports 0.05, Politics/Finance/Tech 0.04, Crypto 0.07 (we use 0.072 to be
# safe), Geopolitics 0. Every #35b market is a MENTIONS market, so it pays at least 0.04
# (never 0, whatever its other labels say), or the highest label rate found; whole words.
FEE_RATES = [("crypto", 0.072), ("bitcoin", 0.072), ("ethereum", 0.072), ("solana", 0.072),
             ("sports", 0.05), ("nfl", 0.05), ("nba", 0.05), ("soccer", 0.05),
             ("politics", 0.04), ("election", 0.04), ("elections", 0.04), ("finance", 0.04),
             ("tech", 0.04), ("mentions", 0.04)]
MENTIONS_FEE = 0.04
FIELDS = ["strategy", "placed", "mode", "market_id", "question", "end", "yes_mid", "no_token",
          "price", "fee_per_share", "shares", "cost", "status", "filled_at",
          "settled", "payout", "profit", "failed_checks", "checked_to", "ended_seen",
          "fill_rule", "filled_sh", "error_since", "worst_case", "counted"]
FILL_RULE = "2"               # rows placed under these rules (18 Oct 2026 on); older rows
                              # are kept but never processed or scored
LAG_S = 180                   # trades can reach the data a little late ...
OVERLAP_S = 6 * 3600          # ... so each check re-reads the last 6 hours too
BUDGET_S = 240                # checks/settling stop 4 min after the PROCESS started
PLACE_DEADLINE_S = 300        # placing new bets stops 5 min after it started (GitHub kills at 6)
MAX_FAILED_CHECKS = 10
NO_RESULT_DAYS = 14
PAUSE = 0.25
FAILED = "lookup failed"      # a lookup that failed is NOT the same as "no fill"
STATS = {"calls": 0, "failed": 0, "unsure": 0, "row_errors": 0}
CLOCK = [time.time()]         # when this run started (set in main)
LAST_RUN = HERE / "pm_paper_last_run.txt"
MIN_GAP_H = 11                # the bot runs at most once per 11 hours (GitHub may start late)



def log(msg):
    with LOG.open("a") as f:
        f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def get_json(url, params=None):
    STATS["calls"] += 1
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(4):
        left = CLOCK[0] + 330 - time.time()        # no request starts past 5.5 min (the
        if left < 5:                               # step is killed at 6)
            break
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "Mozilla/5.0 (research)"})
            with urllib.request.urlopen(req, timeout=min(30, left)) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            log(f"HTTP {e.code} for {full}: {e.read().decode(errors='replace')[:200]}")
            break
        except Exception as e:
            log(f"{type(e).__name__} for {full}: {e}")
            time.sleep(3 * (attempt + 1))
    STATS["failed"] += 1
    return None


def as_list(x):
    if isinstance(x, list):
        return x
    try:
        return json.loads(x) if x else []
    except (TypeError, json.JSONDecodeError):
        return []


def now():
    return datetime.now(timezone.utc)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_of(text):
    return int(datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def load_bets():
    if not BETS.exists():
        return []
    with BETS.open() as f:
        return list(csv.DictReader(f))


def save_bets(rows):
    tmp = BETS.with_suffix(".tmp")
    extra = sorted({k for r in rows for k in r if k not in FIELDS and k is not None})
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS + extra, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(BETS)


def load_seen():
    try:
        return json.loads(SEEN.read_text()) if SEEN.exists() else {}
    except json.JSONDecodeError:
        raise SystemExit(f"  {SEEN.name} is damaged -- not overwriting it. Tell Claude.")


def save_seen(seen):
    tmp = SEEN.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen))
    tmp.replace(SEEN)


def row_id(r):
    return f"{r['strategy']}|{r['market_id']}|{r['mode']}|{r['placed']}"


# ---------------------------------------------------------------------------
def fee_rate(m):
    """Taker fee rate for a #35b (mentions) market: at least the Mentions rate, or the
    highest rate of any label it carries (so the taker line is never too cheap)."""
    words = [str(m.get("category") or "")]
    for src in [m] + [e for e in (m.get("events") or []) if isinstance(e, dict)]:
        words.append(str(src.get("category") or ""))
        for t in src.get("tags") or []:
            words += [str(t.get("label") or ""), str(t.get("slug") or "")] if isinstance(t, dict) \
                else [str(t)]
    text = " ".join(words).lower().replace("-", " ")
    rates = [rate for key, rate in FEE_RATES if re.search(r"\b" + re.escape(key) + r"\b", text)]
    return max([MENTIONS_FEE] + rates)


def order_book(token):
    """Best bid, best ask, and all ask levels for one outcome (None if empty)."""
    b = get_json(f"{CLOB}/book", {"token_id": token})
    if not isinstance(b, dict):
        return None, None, []
    try:
        bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids") or []]
        asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks") or [])
    except (KeyError, TypeError, ValueError):
        return None, None, []
    return (max(bids)[0] if bids else None), (asks[0][0] if asks else None), asks


def buy_into_asks(asks, stake, rate):
    """Spend `stake` buying into the real asks, cheapest first, paying the taker fee on
    each level. Returns (shares, average price, average fee) or None if the book is too thin."""
    left, shares, paid_px, paid_fee = stake, 0.0, 0.0, 0.0
    for p, sz in asks:
        per = p + rate * p * (1 - p)                 # cash per share incl. fee
        take = min(sz, left / per)
        shares += take
        paid_px += take * p
        paid_fee += take * rate * p * (1 - p)
        left -= take * per
        if left <= 1e-6:
            # if the fee is instead taken out of the SHARES, $10 buys stake/avg_price
            # shares minus the fee in shares -- keep whichever count is SMALLER
            avg = paid_px / shares
            alt = (stake / avg) * (1 - rate * (1 - avg))
            shares = min(shares, alt)
            return shares, avg, stake / shares - avg
    return None


# =============================================================================
# 1. CHECK FILLS AND SETTLE
# =============================================================================
def market_info(mid):
    """("final", 1 | 0 | 0.5), ("open", None) or ("error", None)."""
    m = get_json(f"{GAMMA}/{mid}")
    if not isinstance(m, dict):
        return "error", None, None
    toks = as_list(m.get("clobTokenIds"))
    try:                                    # missing volume = NOT zero (an empty reply then
        v = m.get("volumeNum", m.get("volume"))  # stays "unsure" instead of "no trades")
        vol = 1.0 if v in (None, "") else float(v)
    except (TypeError, ValueError):
        vol = 1.0
    cid = (m.get("conditionId"), tuple(str(t) for t in toks) if len(toks) == 2 else None, vol)
    if not m.get("closed"):
        return "open", None, cid
    status = m.get("umaResolutionStatus")
    if status is not None and str(status).lower() != "resolved":
        return "open", None, cid                       # disputed / pending: not final
    try:
        px = [float(x) for x in as_list(m.get("outcomePrices"))]
        y = px[0]
        if len(px) != 2 or abs(px[0] + px[1] - 1) > 0.01:
            return "open", None, cid           # e.g. ["0","0"]: not a real final result
    except (IndexError, TypeError, ValueError):
        return "open", None, cid
    for final in (1.0, 0.0, 0.5):
        if abs(y - final) < 1e-6:
            return "final", final, cid
    return "open", None, cid


def check_maker(r, ids, seen, fresh):
    """Add new real fills to a maker order. True if checked, False if it couldn't be.
    Only up to where the WHOLE trade feed has caught up (`fresh`), so a feed running late
    can never look like 'no trades'."""
    placed = ts_of(r["placed"])
    done_to = int(float(r.get("checked_to") or 0))
    start = max(placed, done_to - OVERLAP_S) if done_to else placed
    upto = min(int(now().timestamp()) - LAG_S, fresh - 120)
    if upto <= start:
        return True
    cid, tokens, vol = ids
    got = ta.trades_between(get_json, cid, start, upto, log=log, pause=PAUSE, tokens=tokens,
                            allow_empty=vol == 0, deadline=CLOCK[0] + BUDGET_S + 60)
    if got is None:
        return False
    got, bad = got
    if bad:
        # a match that can't be judged: this bet is settled on the WORSE of "what we saw"
        # and "fully filled" (see settle) -- it is not left out, and never flatters
        r["worst_case"] = "1"
        log(f"maker bet {row_id(r)}: {len(bad)} match(es) that don't add up -- worst case")
    limit, size = float(r["price"]), float(r["shares"])
    have = float(r.get("filled_sh") or 0)
    try:                                   # counted fills live IN the bet's own row (one
        own = json.loads(r.get("counted") or "[]")    # atomic write), the json file is a
    except json.JSONDecodeError:                      # second copy
        own = []
    counted = set(own) | set(seen.get(row_id(r), []))
    for ts, fills, tx, _ in got:
        if tx in counted or have >= size - 1e-9:
            continue
        through = sum(sh for yp, sh in fills if (1 - yp) < limit - 1e-9)   # No below limit
        if through > 0:
            have = min(size, have + through)
            counted.add(tx)
            if not r.get("filled_at"):
                r["filled_at"] = iso(datetime.fromtimestamp(ts, timezone.utc))
    seen[row_id(r)] = sorted(counted)
    r["counted"] = json.dumps(sorted(counted))
    r["filled_sh"] = f"{have:.4f}"
    r["checked_to"] = str(upto)
    return True


def settle(r, res):
    if r["mode"] == "maker":
        sh = float(r.get("filled_sh") or 0)
        if r.get("worst_case") == "1":
            # worse of: the fills we saw, or the whole order filled
            full, lim = float(r["shares"]), float(r["price"])
            if full * (1 - res) - full * lim < sh * (1 - res) - sh * lim:
                sh = full
        if sh <= 0:
            r["status"], r["settled"] = "expired", iso(now())
            return False
        r["shares"], r["cost"] = f"{sh:.4f}", f"{sh * float(r['price']):.4f}"
    payout = float(r["shares"]) * (1 - res)               # we hold "No": pays 1 - Yes result
    r["status"] = "won" if payout > float(r["cost"]) else "lost"
    r["settled"], r["payout"] = iso(now()), f"{payout:.4f}"
    r["profit"] = f"{payout - float(r['cost']):.4f}"
    return True


def end_ts(r):
    try:
        e = datetime.fromisoformat(str(r.get("end") or "").replace("Z", "+00:00"))
        return (e if e.tzinfo else e.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def update_old(rows, seen, clock, save=lambda: None):
    settled_now, info = 0, {}
    busy = get_json(GAMMA, {"closed": "false", "active": "true", "limit": 3,
                            "order": "volume24hr", "ascending": "false"})
    fresh = ta.newest_trade_time(get_json, [m.get("conditionId") for m in busy or []
                                            if isinstance(m, dict) and m.get("conditionId")],
                                 log=log)
    if fresh is None:
        STATS["unsure"] += 1
        log("trade feed freshness unknown -- no maker checks this run")
    live = [r for r in rows if r.get("fill_rule") == FILL_RULE
            and r.get("status") in ("open", "open_order")]
    live.sort(key=lambda r: int(float(r.get("checked_to") or 0)))     # least recently checked
    for r in live:
        if time.time() - clock > BUDGET_S:
            log("budget used up -- remaining bets are checked next run")
            STATS["unsure"] += 1
            break
        try:
            mid = r["market_id"]
            if mid not in info:
                info[mid] = market_info(mid)
                time.sleep(PAUSE)
            state, res, cid = info[mid]
            if state == "error":
                STATS["unsure"] += 1
                continue
            e = end_ts(r)
            if state == "open" and e and now().timestamp() > e + NO_RESULT_DAYS * 86400:
                r["status"], r["settled"] = "no result", iso(now())    # never settled
                continue
            if r["mode"] == "maker":
                ok = bool(cid[0]) and fresh is not None and check_maker(r, cid, seen, fresh)
                if not ok:
                    STATS["unsure"] += 1
                    if state == "final":
                        r["failed_checks"] = str(int(r.get("failed_checks") or 0) + 1)
                        if int(r["failed_checks"]) >= MAX_FAILED_CHECKS:
                            r["status"], r["settled"] = "unverified", iso(now())
                            log(f"maker bet on {mid}: fills never checkable -- unverified")
                    continue
            if state != "final":
                continue
            if r["mode"] == "maker":
                # settle only after a successful check PAST the moment the end was first seen
                if not r.get("ended_seen"):
                    r["ended_seen"] = iso(now())
                    continue
                if int(float(r.get("checked_to") or 0)) < ts_of(r["ended_seen"]):
                    continue
            settled_now += settle(r, res)
        except Exception:
            STATS["row_errors"] += 1
            log(f"bet {row_id(r)} skipped this run: {traceback.format_exc()[-300:]}")
        finally:
            save()                                    # progress kept even if the run is cut off
    for r in rows:                                     # finished bets: forget counted fills
        if r.get("status") not in ("open", "open_order"):
            seen.pop(row_id(r), None)
    return settled_now


# =============================================================================
# 2. FIND TODAY'S CANDIDATES AND PLACE PAPER BETS
# =============================================================================
def candidates_35b():
    """#35b: open mention markets, not about Trump, 'Yes' 10-90% -> buy 'No'.
    Returns None if the listing itself failed (so the run can show RED)."""
    out, offset = [], 0
    while offset < 2000:
        page = get_json(GAMMA_EVENTS, {"closed": "false", "active": "true", "limit": 100,
                                       "offset": offset, "tag_slug": "mention-markets"})
        if not isinstance(page, list):
            if offset == 0:
                return None
            STATS["unsure"] += 1               # only part of the list could be read
            return out
        for ev in page:
            title = str(ev.get("title") or "")
            if R35["exclude"] in title.lower():
                continue
            tags = [str(t.get("slug") or "") if isinstance(t, dict) else str(t)
                    for t in ev.get("tags") or []]
            if tags and not any("mention" in t.lower() for t in tags):
                continue                       # Gamma ignored the filter: not a mention market
            for m in ev.get("markets") or []:
                if R35["exclude"] in str(m.get("question") or "").lower():
                    continue
                if [str(o).lower() for o in as_list(m.get("outcomes"))] != ["yes", "no"]:
                    continue
                if m.get("closed") or m.get("acceptingOrders", True) is False \
                        or len(as_list(m.get("clobTokenIds"))) != 2:
                    continue
                try:
                    m["_yes"] = float(as_list(m.get("outcomePrices"))[0])
                except (IndexError, TypeError, ValueError):
                    continue
                if R35["lo"] <= m["_yes"] <= R35["hi"]:
                    m["events"] = [{"title": title, "tags": ev.get("tags") or [],
                                    "category": ev.get("category")}]
                    out.append(m)
        if len(page) < 100:
            break
        offset += 100
        time.sleep(PAUSE)
    return out


def place(rows, cands, clock):
    have = {r.get("market_id") for r in rows if r.get("strategy") == "35b"}
    new = 0
    for m in cands:
        if time.time() - clock > PLACE_DEADLINE_S:
            log("out of time -- remaining candidates next run")
            STATS["unsure"] += 1
            break
        mid = str(m.get("id"))
        if mid in have:
            continue                                   # one bet per market, ever
        no_tok = as_list(m.get("clobTokenIds"))[1]
        bid, ask, asks = order_book(no_tok)
        time.sleep(PAUSE)
        if ask is None or ask >= 0.999:
            continue
        base = {"strategy": "35b", "placed": iso(now()), "market_id": mid,
                "question": (m.get("question") or "").replace("\n", " ")[:120],
                "end": m.get("endDate") or "", "yes_mid": f"{m['_yes']:.4f}", "no_token": no_tok,
                "filled_at": "", "settled": "", "payout": "", "profit": "", "fill_rule": FILL_RULE}
        placed_any = False
        fill = buy_into_asks(asks, STAKE, fee_rate(m))
        if fill:                                       # TAKER: into the real asks + fee
            shares, avg_px, avg_fee = fill
            rows.append({**base, "mode": "taker", "price": f"{avg_px:.4f}",
                         "fee_per_share": f"{avg_fee:.5f}", "shares": f"{shares:.4f}",
                         "cost": f"{STAKE:.4f}", "status": "open"})
            placed_any = True
        if bid is not None:                            # MAKER: 1 cent over the bid, on the
            tick = float(m.get("orderPriceMinTickSize") or 0.01)       # grid, below the ask
            limit = math.floor((bid + MAKER_IMPROVE) / tick + 1e-9) * tick
            while limit >= ask - 1e-9:
                limit -= tick
            limit = round(limit, 4)
            if limit > 0:
                rows.append({**base, "mode": "maker", "price": f"{limit:.4f}",
                             "fee_per_share": "0", "shares": f"{STAKE / limit:.4f}",
                             "cost": f"{STAKE:.4f}", "status": "open_order", "filled_sh": "0"})
                placed_any = True
        if placed_any:
            have.add(mid)
            new += 1
    return new


# =============================================================================
# 3. SCOREBOARD
# =============================================================================
def report(rows, settled_now, new):
    W = 74
    print("\n" + "=" * W)
    print(f" POLYMARKET PAPER BOT  |  {datetime.now():%Y-%m-%d %H:%M}  |  no real money")
    print("=" * W)
    print(f" Today: {new} new #35b markets; {settled_now} bets settled.")
    print("\n #35b MENTION MARKETS, not Trump ('Yes' 10-90% -> buy 'No'; watch MAKER)")
    for mode, name in (("taker", "Taker (into the asks + fee)"), ("maker", "Maker (limit, no fee)      ")):
        rs = [r for r in rows if r.get("strategy") == "35b" and r.get("mode") == mode
              and r.get("fill_rule") == FILL_RULE]
        done = [r for r in rs if r["status"] in ("won", "lost")]
        won = sum(r["status"] == "won" for r in done)
        line = f"   {name} finished {len(done):>3} (won {won}, lost {len(done) - won})"
        if done:
            profit = sum(float(r["profit"]) for r in done)
            staked = sum(float(r["cost"]) for r in done)
            line += f"  profit ${profit:+.2f} ({profit / staked:+.2%} of money spent)"
        line += f"  | running {sum(r['status'] in ('open', 'open_order') for r in rs)}"
        print(line)
        extra = {k: sum(r["status"] == k for r in rs) for k in ("expired", "unverified", "no result")}
        if any(extra.values()):
            print(f"      never filled {extra['expired']}, UNVERIFIED {extra['unverified']}, "
                  f"no result {extra['no result']} (the last two are left out -- if many, distrust)")
        wc = sum(r.get("worst_case") == "1" for r in rs)
        if wc:
            print(f"      {wc} maker bet(s) settled on the WORST CASE (a match that couldn't be"
                  f" judged) -- if many, the maker line understates")
        if done and (extra["unverified"] or extra["no result"]):
            worst = sum(float(r["profit"]) for r in done) - STAKE * (extra["unverified"] + extra["no result"])
            print(f"      WORST CASE (every left-out bet a full $10 loss): profit ${worst:+.2f}")
        paid = [r for r in done if r["mode"] == "taker" or float(r.get("filled_sh") or 0) > 0]
        if paid:
            gaps = [float(r["price"]) + float(r["fee_per_share"] or 0) - (1 - float(r["yes_mid"]))
                    for r in paid]
            print(f"      real cost vs Polymarket's price ({len(paid)} bought): "
                  f"{sum(gaps) / len(gaps) * 100:+.2f} cents/share")
    old = sum(r.get("fill_rule") != FILL_RULE for r in rows)
    if old:
        print(f"\n ({old} older paper bets -- earlier, looser rules or retired strategies -- are"
              f" kept in the file but NOT scored.)")
    print("\n Judge after 1-2 months / 100+ finished bets. Markets that end EARLY are mostly")
    print(" 'Yes' wins (our losses), so early scores look worse than the final result.")
    print(f" All bets: {BETS.name}")
    print("=" * W + "\n")


def main():
    clock = time.time()
    CLOCK[0] = clock
    force = "--force" in sys.argv
    if LAST_RUN.exists() and not force:
        try:
            last = float(LAST_RUN.read_text().strip())
        except ValueError:
            last = 0
        if time.time() - last < MIN_GAP_H * 3600:
            print(f"  Paper bot ran {(time.time() - last) / 3600:.1f} h ago -- next run after {MIN_GAP_H} h.")
            return
    used = pm_minutes.used_this_month()
    if used >= pm_minutes.MONTH_LIMIT:
        print(f"  SKIPPED: about {used} GitHub minutes used this month -- see pm_minutes.csv")
        sys.exit(1)
    key = pm_minutes.start("paper_bot")
    try:
        _run(clock, key)
    finally:
        pm_minutes.finish(key)


def _run(clock, key):
    LAST_RUN.write_text(str(time.time()))             # written FIRST: a run cut off by the
    rows = load_bets()                                 # time limit is not repeated at once
    seen = load_seen()
    print("  Checking earlier paper bets...")
    settled_now = update_old(rows, seen, clock, save=lambda: (save_bets(rows), save_seen(seen)))
    save_bets(rows)                                    # each bet's counted fills are in its own
    save_seen(seen)                                    # row, so nothing can be counted twice
    print("  Looking for today's #35b candidates...")
    if time.time() - clock < PLACE_DEADLINE_S:
        cands = candidates_35b()
    else:
        cands = []
        STATS["unsure"] += 1                           # no time left to look for new bets
    new = place(rows, cands or [], clock)
    save_bets(rows)
    report(rows, settled_now, new)
    share = STATS["failed"] / STATS["calls"] if STATS["calls"] else 0
    problems = []
    if cands is None:
        problems.append("candidate listing FAILED")
    if share > 0.2:
        problems.append(f"{share:.0%} of requests failed")
    if STATS["unsure"]:
        problems.append(f"{STATS['unsure']} bet check(s) unsure (they wait)")
    if STATS["row_errors"]:
        problems.append(f"{STATS['row_errors']} bet(s) hit an error (see {LOG.name})")
    if problems:
        print("  UNHEALTHY: " + "; ".join(problems))
        log("unhealthy run: " + "; ".join(problems))
        sys.exit(1)                                    # shows RED on GitHub (results saved)


if __name__ == "__main__":
    main()
