"""
pm_paper_bot.py  --  PAPER trading for #22, #26, #32 and #35b on Polymarket
=================================================================
Runs once a day. NO wallet, NO keys, NO real bets -- it only READS public
Polymarket data and writes down what it WOULD have done, at REAL prices.

FOUR STRATEGIES, side by side (all buy "No"):
  #22  AGAINST LONG SHOTS: market ends in 6-8 days and "Yes" costs 2%-10%.
       (Backtest: 4 of 5 rules; the open question is the real buying cost.)
  #26  AGAINST HYPE: "Yes" rose 10+ points over the past week, is now between
       10% and 90%, and the market ends in 3 days to ~13 months.
       (Spotted on Polymarket, 4 of 5 rules on unseen Kalshi data.)
  #32  BRAND-NEW MARKETS: the market opened 1-3 days ago, "Yes" is between
       10% and 90%, and $1,000+ has traded so far. (Backtest: 4 of 5 rules.)
  #35b MENTION MARKETS, NOT Trump: an open "Will X say <word>?" market (Polymarket's
       "mention-markets" label) whose event and question don't mention Trump, "Yes"
       between 10% and 90%. (Backtest #35: "Yes" overpriced 3-5 points everywhere,
       but costs ate it; non-Trump looked best -- found AFTER the test, so it must
       prove itself here, on new markets. The MAKER line is the one that matters.)
       Note: the bot runs once a day, so bets land 0-24 h before the speech, not 2 h.

EACH RUN
  1. Settles old paper bets whose markets have finished (win = $1/share, else $0).
  2. Finds today's candidates for both strategies (one bet per market, ever).
  3. Records TWO paper bets of $10 each per candidate:
       TAKER  buy "No" right now at the real best ASK, plus Polymarket's taker fee.
       MAKER  a limit order 1 cent above the best BID (fee-free); later runs check
              whether the price ever came down to it (= would it have filled?).
  4. Prints the scoreboard for each strategy and each way of buying, plus the
     REAL cost compared with the market's mid price.

HOW TO RUN (polymarket folder, once a day -- any time is fine)
    python pm_paper_bot.py
Saves everything to pm_paper_bets.csv. Judge each strategy after 1-2 months.
"""
import csv
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BETS = HERE / "pm_paper_bets.csv"
LOG = HERE / "pm_paper_log.txt"
GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com"
RULES = {"days_min": 6, "days_max": 8, "lo": 0.02, "hi": 0.10, "min_volume": 10_000,
         "stake": 10.0, "maker_improve": 0.01}
# Taker fee rate by category (Polymarket: fee = shares x rate x p x (1-p)). We can't
# always tell the category, so unknown = 0.05 (the common middle rate).
FEE_RATES = [("crypto", 0.07), ("bitcoin", 0.07), ("ethereum", 0.07),
             ("nfl", 0.03), ("college football", 0.03),
             ("politic", 0.04), ("election", 0.04), ("finance", 0.04), ("tech", 0.04),
             ("geopolit", 0.0), ("world", 0.0)]
DEFAULT_FEE = 0.05
# #26 (locked): "Yes" rose >= 10 points in 7 days, now 10%-90%, market ends in 3+ days
R26 = {"rise": 0.10, "lo": 0.10, "hi": 0.90, "min_days_left": 3, "max_days_left": 400}
# #32 (locked): market opened 1-3 days ago, "Yes" 10%-90%, $1,000+ traded SO FAR
R32 = {"min_age_days": 1, "max_age_days": 3, "lo": 0.10, "hi": 0.90, "min_volume": 1_000}
R35 = {"lo": 0.10, "hi": 0.90, "exclude": "trump"}
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
TRADES = "https://data-api.polymarket.com/v2/trades"
FIELDS = ["strategy", "placed", "mode", "market_id", "question", "end", "yes_mid", "no_token",
          "price", "fee_per_share", "shares", "cost", "status", "filled_at",
          "settled", "payout", "profit"]
PAUSE = 0.25


def log(msg):
    with LOG.open("a") as f:
        f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def get_json(url, params=None):
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(4):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "Mozilla/5.0 (research)"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            log(f"HTTP {e.code} for {full}: {e.read().decode(errors='replace')[:200]}")
            return None
        except Exception as e:
            log(f"{type(e).__name__} for {full}: {e}")
            time.sleep(3 * (attempt + 1))
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


def load_bets():
    if not BETS.exists():
        return []
    with BETS.open() as f:
        return list(csv.DictReader(f))


def save_bets(rows):
    tmp = BETS.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(BETS)


def fee_rate(m):
    if m.get("feesEnabled") is False:
        return 0.0
    text = " ".join(str(m.get(k) or "") for k in ("category", "question", "slug")).lower()
    for ev in m.get("events") or []:
        if isinstance(ev, dict):
            text += " " + str(ev.get("title") or "").lower() + " " + str(ev.get("category") or "").lower()
    for key, rate in FEE_RATES:
        if key in text:
            return rate
    return DEFAULT_FEE


def order_book(token):
    """Best bid and best ask for one outcome (None if the book is empty)."""
    b = get_json(f"{CLOB}/book", {"token_id": token})
    if not isinstance(b, dict):
        return None, None, 0.0
    bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids") or []]
    asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks") or []]
    best_bid = max(bids)[0] if bids else None
    best_ask = min(asks)[0] if asks else None
    ask_size = sum(s for p, s in asks if best_ask is not None and p <= best_ask + 0.005)
    return best_bid, best_ask, ask_size


# =============================================================================
# 1. SETTLE / CHECK OLD BETS
# =============================================================================
def market_result(mid):
    """1 = Yes won, 0 = No won, None = not finished yet (or unclear)."""
    m = get_json(f"{GAMMA}/{mid}")
    if not isinstance(m, dict) or not m.get("closed"):
        return None
    try:
        final = [float(p) for p in as_list(m.get("outcomePrices"))]
    except (TypeError, ValueError):
        return None
    if len(final) == 2 and final[0] >= 0.99:
        return 1
    if len(final) == 2 and final[1] >= 0.99:
        return 0
    return None


def maker_filled(row):
    """Did the 'No' price ever come down to our limit price after we placed it?
    (Uses Polymarket's hourly price history -- a slightly optimistic check.)"""
    start = int(datetime.strptime(row["placed"], "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=timezone.utc).timestamp())
    end = int(now().timestamp())
    limit = float(row["price"])
    any_points = False
    a = start
    while a < end:
        b = min(a + 6 * 86400, end)
        d = get_json(f"{CLOB}/prices-history", {"market": row["no_token"], "startTs": a,
                                                "endTs": b, "fidelity": 60})
        for p in (d or {}).get("history", []) if isinstance(d, dict) else []:
            any_points = True
            if float(p["p"]) <= limit:
                return datetime.fromtimestamp(int(p["t"]), timezone.utc)
        a = b
        time.sleep(PAUSE)
    if not any_points:                 # history is often empty for short/closed markets
        return filled_by_trades(row["market_id"], start, limit)
    return None


def filled_by_trades(market_id, start, limit):
    """Fallback: real trades after we placed the order. Our 'No' buy order at `limit`
    counts as filled only if 'No' traded BELOW it (price went through our order)."""
    m = get_json(f"{GAMMA}/{market_id}")
    cid = m.get("conditionId") if isinstance(m, dict) else None
    if not cid:
        return None
    cursor = None
    for _ in range(5):
        params = {"cursor": cursor} if cursor else {"condition": cid, "limit": 1000,
                                                    "taker_only": "true"}
        d = get_json(TRADES, params)
        items = d if isinstance(d, list) else (d or {}).get("data") or []
        cursor = None if isinstance(d, list) else ((d or {}).get("pagination") or {}).get("next_cursor")
        for t in items:
            try:
                ts = int(float(t.get("timestamp") or 0))
                ts = ts // 1000 if ts > 10 ** 12 else ts
                p, oi = float(t["price"]), int(t.get("outcome_index", t.get("outcomeIndex")))
            except (TypeError, ValueError, KeyError):
                continue
            no_price = p if oi == 1 else 1 - p
            if ts > start and no_price < limit - 1e-9:
                return datetime.fromtimestamp(ts, timezone.utc)
        if not cursor or not items:
            break
        time.sleep(PAUSE)
    return None


def update_old(rows):
    settled_now = 0
    results = {}
    for r in rows:
        if r["status"] in ("won", "lost", "expired"):
            continue
        if r["mode"] == "maker" and r["status"] == "open_order":
            when = maker_filled(r)
            if when:
                r["status"], r["filled_at"] = "open", iso(when)
        if r["market_id"] not in results:
            results[r["market_id"]] = market_result(r["market_id"])
            time.sleep(PAUSE)
        res = results[r["market_id"]]
        if res is None:
            continue
        if r["status"] == "open_order":                 # market ended, order never filled
            r["status"] = "expired"
            r["settled"] = iso(now())
            continue
        payout = float(r["shares"]) * (1.0 if res == 0 else 0.0)
        r["status"] = "won" if res == 0 else "lost"
        r["settled"], r["payout"] = iso(now()), f"{payout:.4f}"
        r["profit"] = f"{payout - float(r['cost']):.4f}"
        settled_now += 1
    return settled_now


# =============================================================================
# 2. FIND TODAY'S CANDIDATES AND PLACE PAPER BETS
# =============================================================================
def list_active(end_min, end_max):
    """Open Yes/No markets ending between two dates (asked in 2-week pieces, so
    we never page too deep -- Polymarket refuses offsets past ~2,500)."""
    out, a = [], end_min
    while a < end_max:
        b = min(a + timedelta(days=14), end_max)
        offset = 0
        while offset < 2500:
            page = get_json(GAMMA, {"closed": "false", "active": "true", "limit": 500,
                                    "offset": offset, "volume_num_min": RULES["min_volume"],
                                    "end_date_min": iso(a), "end_date_max": iso(b)})
            if not isinstance(page, list) or not page:
                break
            for m in page:
                if [str(o).lower() for o in as_list(m.get("outcomes"))] != ["yes", "no"]:
                    continue
                if m.get("acceptingOrders", True) is False or len(as_list(m.get("clobTokenIds"))) != 2:
                    continue
                try:
                    m["_yes"] = float(as_list(m.get("outcomePrices"))[0])
                    m["_vol"] = float(m.get("volumeNum") or m.get("volume") or 0)
                except (IndexError, TypeError, ValueError):
                    continue
                if m["_vol"] >= RULES["min_volume"]:
                    out.append(m)
            if len(page) < 500:
                break
            offset += 500
            time.sleep(PAUSE)
        a = b
    return out


def candidates_22():
    """#22: ends in 6-8 days, 'Yes' at 2-10% -> buy 'No'."""
    t0 = now()
    ms = list_active(t0 + timedelta(days=RULES["days_min"]), t0 + timedelta(days=RULES["days_max"]))
    return [m for m in ms if RULES["lo"] <= m["_yes"] <= RULES["hi"]]


def candidates_26():
    """#26: 'Yes' ROSE >= 10 points over the past week, now between 10% and 90%,
    market ends in 3+ days -> buy 'No' (bet the hype is overpriced)."""
    t0 = now()
    ms = list_active(t0 + timedelta(days=R26["min_days_left"]), t0 + timedelta(days=R26["max_days_left"]))
    out, missing = [], 0
    for m in ms:
        chg = m.get("oneWeekPriceChange")
        if chg is None:
            missing += 1
            continue
        try:
            chg = float(chg)
        except (TypeError, ValueError):
            continue
        if chg >= R26["rise"] and R26["lo"] <= m["_yes"] <= R26["hi"]:
            out.append(m)
    if ms and missing == len(ms):
        log("#26: no market had a 'oneWeekPriceChange' field -- strategy skipped")
        print("  Note: Polymarket didn't send weekly price changes -- #26 skipped today.")
    return out


def parse_time(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def candidates_32():
    """#32: brand-new markets (opened 1-3 days ago), 'Yes' at 10-90% -> buy 'No'.
    Volume = what has traded SO FAR (we can't know a market's final volume)."""
    t0 = now()
    out, offset = [], 0
    while offset < 2500:
        page = get_json(GAMMA, {"closed": "false", "active": "true", "limit": 500, "offset": offset,
                                "volume_num_min": R32["min_volume"],
                                "start_date_min": iso(t0 - timedelta(days=R32["max_age_days"])),
                                "start_date_max": iso(t0 - timedelta(days=R32["min_age_days"]))})
        if not isinstance(page, list) or not page:
            break
        for m in page:
            if [str(o).lower() for o in as_list(m.get("outcomes"))] != ["yes", "no"]:
                continue
            if m.get("acceptingOrders", True) is False or len(as_list(m.get("clobTokenIds"))) != 2:
                continue
            start = parse_time(m.get("startDate") or m.get("createdAt"))
            end = parse_time(m.get("endDate"))
            if not start or (end and end <= t0 + timedelta(days=1)):
                continue
            age = (t0 - start).total_seconds() / 86400
            try:
                m["_yes"] = float(as_list(m.get("outcomePrices"))[0])
                m["_vol"] = float(m.get("volumeNum") or m.get("volume") or 0)
            except (IndexError, TypeError, ValueError):
                continue
            if (R32["min_age_days"] <= age <= R32["max_age_days"] and m["_vol"] >= R32["min_volume"]
                    and R32["lo"] <= m["_yes"] <= R32["hi"]):
                out.append(m)
        if len(page) < 500:
            break
        offset += 500
        time.sleep(PAUSE)
    return out


def candidates_35b():
    """#35b: open mention markets, not about Trump, 'Yes' 10-90% -> buy 'No'."""
    out, offset = [], 0
    while offset < 2000:
        page = get_json(GAMMA_EVENTS, {"closed": "false", "active": "true", "limit": 100,
                                       "offset": offset, "tag_slug": "mention-markets"})
        if not isinstance(page, list) or not page:
            break
        for ev in page:
            title = str(ev.get("title") or "")
            if R35["exclude"] in title.lower():
                continue
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
                    m.setdefault("events", [{"title": title}])
                    out.append(m)
        if len(page) < 100:
            break
        offset += 100
        time.sleep(PAUSE)
    return out


def place(rows):
    have = {(r["strategy"], r["market_id"]) for r in rows}
    new = {"22": 0, "26": 0, "32": 0, "35b": 0}
    for strat, cands in (("22", candidates_22()), ("26", candidates_26()), ("32", candidates_32()),
                         ("35b", candidates_35b())):
        for m in cands:
            mid = str(m.get("id"))
            if (strat, mid) in have:
                continue                               # one bet per market and strategy
            no_tok = as_list(m.get("clobTokenIds"))[1]
            bid, ask, ask_size = order_book(no_tok)
            time.sleep(PAUSE)
            if ask is None or bid is None or ask >= 0.999:
                continue
            stake, yes = RULES["stake"], m["_yes"]
            base = {"strategy": strat, "placed": iso(now()), "market_id": mid,
                    "question": (m.get("question") or "").replace("\n", " ")[:120],
                    "end": m.get("endDate") or "", "yes_mid": f"{yes:.4f}", "no_token": no_tok,
                    "filled_at": "", "settled": "", "payout": "", "profit": ""}
            # TAKER: buy now at the ask, pay the fee
            fee = fee_rate(m) * ask * (1 - ask)
            rows.append({**base, "mode": "taker", "price": f"{ask:.4f}",
                         "fee_per_share": f"{fee:.5f}", "shares": f"{stake / (ask + fee):.4f}",
                         "cost": f"{stake:.4f}",
                         "status": "open" if ask_size * ask >= stake else "open_thin"})
            # MAKER: limit order 1 cent above the best bid (never at/above the ask), no fee
            limit = min(round(bid + RULES["maker_improve"], 4), round(ask - 0.001, 4))
            rows.append({**base, "mode": "maker", "price": f"{limit:.4f}", "fee_per_share": "0",
                         "shares": f"{stake / limit:.4f}", "cost": f"{stake:.4f}",
                         "status": "open_order"})
            have.add((strat, mid))
            new[strat] += 1
    return new


# =============================================================================
# 3. SCOREBOARD
# =============================================================================
TITLES = {"22": "#22  AGAINST LONG SHOTS ('Yes' 2-10%, ending in 6-8 days -> buy 'No')",
          "26": "#26  AGAINST HYPE ('Yes' rose 10+ points this week -> buy 'No')",
          "32": "#32  BRAND-NEW MARKETS (opened 1-3 days ago, 'Yes' 10-90% -> buy 'No')",
          "35b": "#35b MENTION MARKETS, not Trump ('Yes' 10-90% -> buy 'No'; watch MAKER)"}


def report(rows, settled_now, new):
    W = 74
    print("\n" + "=" * W)
    print(f" POLYMARKET PAPER BOT  |  {datetime.now():%Y-%m-%d %H:%M}  |  no real money")
    print("=" * W)
    print(f" Today: {new['22']} new #22, {new['26']} new #26, {new['32']} new #32, {new['35b']} new #35b"
          f" bets (2 paper bets each); "
          f"{settled_now} bets settled.")
    for strat, title in TITLES.items():
        print(f"\n {title}")
        for mode, name in (("taker", "Taker (buy now at the ask + fee)"),
                           ("maker", "Maker (limit order, no fee)   ")):
            rs = [r for r in rows if r["strategy"] == strat and r["mode"] == mode]
            done = [r for r in rs if r["status"] in ("won", "lost")]
            won = sum(r["status"] == "won" for r in done)
            running = sum(r["status"] in ("open", "open_thin") for r in rs)
            line = f"   {name}  finished {len(done):>3} (won {won}, lost {len(done) - won})"
            if done:
                profit = sum(float(r["profit"]) for r in done)
                staked = sum(float(r["cost"]) for r in done)
                line += f"  profit ${profit:+.2f} ({profit / staked:+.2%}/bet)"
            line += f"  | running {running}"
            if mode == "maker":
                line += (f", waiting {sum(r['status'] == 'open_order' for r in rs)}, "
                         f"never filled {sum(r['status'] == 'expired' for r in rs)}")
            print(line)
            if rs:
                gaps = [float(r["price"]) + float(r["fee_per_share"]) - (1 - float(r["yes_mid"]))
                        for r in rs]
                print(f"      real cost vs mid price: {sum(gaps) / len(gaps) * 100:+.2f} cents/share"
                      + ("   [#22 backtest assumed +1.00]" if strat == "22" and mode == "taker" else ""))
    print("\n Judge each after 1-2 months / 100+ finished bets. #26 bets can run for weeks.")
    print(f" All bets: {BETS.name}")
    print("=" * W + "\n")


def main():
    rows = load_bets()
    for r in rows:                                     # bets from before #26 existed
        r.setdefault("strategy", "22")
        if not r.get("strategy"):
            r["strategy"] = "22"
    print("  Checking earlier paper bets...")
    settled_now = update_old(rows)
    print("  Looking for today's candidates (#22 long shots, #26 hype, #32 new markets, "
          "#35b mentions)...")
    new = place(rows)
    save_bets(rows)
    report(rows, settled_now, new)


if __name__ == "__main__":
    main()
