"""
pm_trades_api.py  --  ONE careful way to read Polymarket's public trade list
=============================================================================
Used by pm_mm_paper.py and pm_paper_bot.py. It never guesses: if the answer might be
incomplete or wrong, it says so (returns None) instead of pretending "no trades".

    trades_between(get_json, cid, start, end)  ->  (matches, bad_times), or None if unsure

Returns (matches, bad_times). Each match (one blockchain transaction):
    (time, fills, transaction_id, True), fills = [(yes_price, shares), ...] -- the MAKER
    fills, each at its own price, as a "Yes" price (a No fill at p = Yes at 1-p).
bad_times = when matches happened that can't be judged (rows missing / unreadable); the
    callers treat those moments as NOT checkable and book the worst case for them.
Why: the live probe (18 Oct 2026) showed that when one big order sweeps several price
levels, the TAKER's row is recorded at the AVERAGE price, so an order that went through
our bid could look like it stayed above it. So we read every individual fill
(taker_only=false) and judge each maker fill at its own price. The taker's row (the
largest, = all the maker fills together) is left out; a match that doesn't add up is
reported in bad_times instead of being judged.
Probe facts: replies are {"data": [...], "pagination": {..., "has_more", "next_cursor"}},
newest-first, each trade names its market (condition_id); time-range parameters are
ignored. Every request (also with a cursor) repeats condition/limit/taker_only, so later
pages can't silently fall back to taker-only rows -- and a full page in which no
transaction has 2+ rows (i.e. no maker rows) is treated as unsure.
"""
import time
from datetime import datetime

TRADES = "https://data-api.polymarket.com/v2/trades"
PAGE = 1000
MAX_PAGES = 40
TAKER_ONLY = "false"
CURSOR_WITH_PARAMS = True     # repeat the filters on every page (see pm_trades_probe.py)


def trade_time(t):
    v = t.get("timestamp") or t.get("time") or t.get("match_time")
    try:
        ts = int(float(v))
        return ts // 1000 if ts > 10 ** 12 else ts
    except (TypeError, ValueError):
        try:
            return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None


def unpack(d):
    """(items, cursor, has_more) from a reply, or None if it is an error / unknown shape.
    has_more is True / False / None (not given)."""
    has_more = None
    if isinstance(d, list):
        items, cursor = d, None
    elif isinstance(d, dict) and not d.get("error") and ("data" in d or "trades" in d):
        items = d.get("data") if "data" in d else d.get("trades")
        if items is None:
            return None
        pag = d.get("pagination") if isinstance(d.get("pagination"), dict) else {}
        cursor = pag.get("next_cursor") or pag.get("nextCursor") or d.get("next_cursor")
        hm = pag.get("has_more", d.get("has_more"))
        has_more = hm if isinstance(hm, bool) else None
    else:
        return None
    if not isinstance(items, list) or any(not isinstance(t, dict) for t in items):
        return None
    if cursor in ("", "LTE="):
        cursor = None
    return items, cursor, has_more


def params_for(cid, cursor):
    base = {"condition": cid, "limit": PAGE, "taker_only": TAKER_ONLY}
    if not cursor:
        return base
    return {**base, "cursor": cursor} if CURSOR_WITH_PARAMS else {"cursor": cursor}


CHECK_SIDES = True             # also require the taker's row to trade the OPPOSITE way to
                               # every maker row (catches a missing taker row whose makers
                               # happen to add up). Off until the probe (section 8) shows
                               # the data labels sides that way -- then switch it on.
MATCH_ABS = 0.01               # shares: taker row vs the maker rows it matched may differ by
MATCH_REL = 0.001              # rounding only -- max(0.01 shares, 0.1%). A bigger gap can be
                               # a missing maker fill (maybe the one through our bid), so the
                               # match is reported as bad. See pm_trades_probe.py section 7.


def group(rows, start=None, end=None):
    """rows (tx, ts, yes_price, shares, direction) -> (matches, bad_times), keeping only matches
    whose time is in (start, end] -- rows are grouped FIRST, so a match whose rows sit on
    both sides of the window edge is still judged whole.
    matches   = [(time, [(yes_price, shares) maker fills], tx, True)] for every COMPLETE
                order match. With taker_only=false a real match has the taker's row plus
                at least one maker row, and the taker's size equals the makers' together.
    bad_times = the times of matches that are NOT complete (one row, or sizes that don't
                add up). Their taker row alone shows an AVERAGE price that can hide a sweep
                through our bid, so we never judge them -- the caller treats the period
                around each one as NOT checkable (worst case), instead of guessing."""
    by = {}
    for tx, ts, yp, sh, d in rows:
        by.setdefault(tx, []).append((ts, yp, sh, d))
    out, bad = [], []
    for tx, rs in by.items():
        ts = min(r[0] for r in rs)
        fills = [(yp, sh) for _, yp, sh, _ in rs]
        big = max(fills, key=lambda f: f[1])
        if start is not None and not start < ts <= end:
            continue
        if CHECK_SIDES and len(rs) >= 2:
            bi = max(range(len(rs)), key=lambda i: rs[i][2])
            dirs = [r[3] for r in rs]
            if None in dirs or any(dirs[i] == dirs[bi] for i in range(len(rs)) if i != bi):
                bad.append(ts)
                continue
        others = sum(f[1] for f in fills) - big[1]
        if start is not None and not start < ts <= end:
            continue
        if len(fills) < 2 or abs(big[1] - others) > max(MATCH_ABS, MATCH_REL * big[1]):
            bad.append(ts)
            continue
        fills.remove(big)
        out.append((ts, fills, tx, True))
    return sorted(out, key=lambda x: x[0]), sorted(bad)


def newest_trade_time(get_json, cids, log=print):
    """How far the trade data has caught up: the newest trade time across a few BUSY
    markets (read the same way as the checks themselves). None if unknown."""
    best = None
    for cid in cids:
        got = unpack(get_json(TRADES, {"condition": cid, "limit": 5, "taker_only": TAKER_ONLY}))
        if not got or not got[0]:
            continue
        times = [trade_time(t) for t in got[0] if isinstance(t, dict) and trade_time(t)]
        if times:
            best = max(times) if best is None else max(best, max(times))
    if best is None:
        log("trade data freshness: unknown")
    return best


def trades_between(get_json, cid, start, end, log=print, pause=0.15, tokens=None,
                   allow_empty=False, max_pages=MAX_PAGES, deadline=None):
    """Order matches of market `cid` with start < time <= end, as (matches, bad_times)
    -- see group() -- or None if the whole answer is unsure (failed / error reply, cut off,
    out-of-order pages, a row whose time can't be read, a lost market filter...).
    `tokens` = (yes_token_id, no_token_id): each row's outcome is taken from its token id.
    `deadline`: a clock time; past it, the read stops and answers "unsure".
    `allow_empty`: an empty history counts as "no trades" (only for markets Polymarket
    itself shows with zero volume); otherwise an empty first page is unsure.
    Rows repeated from an earlier page (offset paging while new trades arrive) are skipped."""
    rows, seen, cursor = {}, set(), None
    order, prev_last, bad_extra = None, None, []
    for page in range(max_pages):
        if deadline is not None and time.time() > deadline:
            log(f"trades {cid}: out of time while reading -- unsure (tried again next run)")
            return None
        got = unpack(get_json(TRADES, params_for(cid, cursor)))
        if got is None:
            log(f"trades {cid}: failed or error reply")
            return None
        items, cursor, has_more = got
        if not items:
            if page == 0:
                if allow_empty and has_more is False:
                    return [], []
                log(f"trades {cid}: empty first page -- unsure")
                return None
            if has_more is False:
                m, b = group(rows.values(), start, end)
                return m, sorted(b + bad_extra)
            log(f"trades {cid}: empty page while paging -- unsure")
            return None
        times, mine, tx_count, n_same, first_new = [], 0, {}, {}, None
        labelled = any((t.get("conditionId") or t.get("condition_id") or t.get("market"))
                       for t in items)
        for t in items:
            tc = t.get("conditionId") or t.get("condition_id") or t.get("market")
            if labelled and (not tc or str(tc).lower() != str(cid).lower()):
                continue                          # another market's trade (or unlabelled)
            ts = trade_time(t)
            if ts is None:
                log(f"trades {cid}: a row with no readable time -- unsure")
                return None
            tok = str(t.get("token_id") or t.get("asset") or "")
            try:
                p, sh = float(t["price"]), float(t.get("size") or t.get("shares"))
                oi = int(t.get("outcome_index", t.get("outcomeIndex")))
                if tokens and tok:
                    if tok not in tokens:
                        raise ValueError("unknown token")
                    oi = 0 if tok == str(tokens[0]) else 1
                if oi not in (0, 1):
                    raise ValueError("bad outcome")
            except (TypeError, ValueError, KeyError):
                mine += 1                         # this market's row, but unusable: the
                times.append(ts)                  # moment it happened is NOT checkable
                if start < ts <= end:
                    bad_extra.append(ts)
                continue
            mine += 1
            times.append(ts)
            tx = t.get("transactionHash") or t.get("transaction_hash") or ""
            tx_count[tx] = tx_count.get(tx, 0) + 1
            key = (tx, ts, p, sh, oi, str(t.get("side") or ""),
                   str(t.get("proxyWallet") or t.get("proxy_wallet") or t.get("taker") or ""))
            if key in seen:
                continue                          # repeated from an EARLIER page only
            first_new = ts if first_new is None else first_new
            n_same[key] = n_same.get(key, 0) + 1  # identical rows WITHIN a page are real
            side = str(t.get("side") or "").upper()
            d = None if side not in ("BUY", "SELL") else (1 if (side == "BUY") == (oi == 0) else -1)
            if start - 60 < ts <= end + 60:      # a little either side: whole matches
                rows[(key, n_same[key])] = (tx or repr(key), ts, p if oi == 0 else 1 - p, sh, d)
        seen |= set(n_same)
        if mine == 0:
            log(f"trades {cid}: a page with none of this market's trades -- unsure")
            return None
        if len(items) >= 100 and not any(n > 1 for tx, n in tx_count.items() if tx):
            log(f"trades {cid}: a full page with NO maker rows -- the filter was lost; unsure")
            return None
        down = all(a >= b for a, b in zip(times, times[1:]))
        up = all(a <= b for a, b in zip(times, times[1:]))
        if not down and not up:
            log(f"trades {cid}: a page out of time order -- unsure")
            return None
        if order is None and times[0] != times[-1]:
            order = "new" if down else "old"
        elif order == "new" and not down or order == "old" and not up:
            log(f"trades {cid}: pages changed direction -- unsure")
            return None
        if prev_last is not None and first_new is not None and (
                order == "new" and first_new > prev_last or order == "old" and first_new < prev_last):
            log(f"trades {cid}: a page does not continue from the last one -- unsure")
            return None
        prev_last = times[-1]
        done = (order == "new" and times[-1] <= start or order == "old" and times[-1] > end
                or has_more is False)
        if done:
            m, b = group(rows.values(), start, end)
            return m, sorted(b + bad_extra)
        if not cursor:
            log(f"trades {cid}: more data but no way to read it -- unsure")
            return None
        time.sleep(pause)
    log(f"trades {cid}: cut off after {max_pages} pages -- unsure")
    return None
