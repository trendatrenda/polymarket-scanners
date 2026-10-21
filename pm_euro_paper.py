"""
pm_euro_paper.py  --  #37 EuroLeague PAPER bot (forward test, new games only)
===========================================================================
Runs every 2 h on GitHub. NO wallet, NO keys, NO real bets: it reads public data and
writes down what it WOULD do, at the REAL order-book prices of that moment.
    python pm_euro_paper.py              (normal run)
    python pm_euro_paper.py --summary    (only print the score so far)

LOCKED RULES (set 21 Oct 2026, before any of the games it will bet on were played)
  Ratings    per team OFFENSE, DEFENSE (points per 100 possessions) and PACE, adjusted for
             opponent and home court; recent games count more (half-life 45 days); gently
             pulled toward average. Same method as pm_hoops_test.py. Only games that
             started 4+ hours before the run are used.
  Confidence (the fix for the backtest's over-caution) win chance = logistic curve of the
             expected margin, its 2 numbers fitted on EARLIER games that were predicted
             BEFORE they were played (the last 400; at least 80, else no bets). Judged on
             prediction accuracy only, never on profit.
  When       a game whose tip-off is 1-3 h after the run (each game is looked at once).
             Both teams must have played 10+ games this season.
  Price      the REAL order book: buy $20 of the team's shares at the actual asks, level
             by level (book too thin -> no bet), plus that market's own taker fee
             (rate x p x (1-p), 0 if the market has no fee).
  The bet    the team whose win chance beats its full cost by 5+ points. One per game.
             Held to the end; settled on Polymarket's final result.
  Verdict    after 60+ settled bets (likely by Feb-Apr 2027): PASS only if profit > 0 AND
             our win chances were MORE accurate than the market's (Brier) on all games it
             looked at. Anything else = retire.
Files: euro_games.csv (results + possessions), euro_paper_bets.csv, euro_paper_log.csv
"""
import argparse
import csv
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pm_rewards_scanner as rs
from pm_hoops_download import EURO_BOX, EURO_GAMES, fetch, is_game_winner, poss, same_team, ts_of

HERE = Path(__file__).resolve().parent
GAMES = HERE / "euro_games.csv"
BETS = HERE / "euro_paper_bets.csv"
LOG = HERE / "euro_paper_log.csv"
SEASONS = ["E2025", "E2026"]
CURRENT = "E2026"
GAMMA = "https://gamma-api.polymarket.com"
BOOK = "https://clob.polymarket.com/book"
SERIES = "10371"
HL, LAMBDA, MIN_GAMES, MIN_CAL, EDGE, STAKE = 45.0, 10.0, 10, 80, 0.05, 20.0
BLOCK = 6 * 3600
MAX_BOX_PER_RUN = 250
G_FIELDS = ["season", "gid", "code", "ko", "phase", "home", "away", "home_pts", "away_pts", "poss", "played"]
B_FIELDS = ["placed", "ko", "game", "cid", "team", "token", "q", "avg_ask", "fee", "cost", "shares",
            "status", "settled", "payout", "pnl"]
L_FIELDS = ["time", "ko", "game", "cid", "team_a", "q_a", "ask_a", "ask_b", "mid_a", "decision", "a_won"]


def now():
    return int(time.time())


def read(path):
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def write(path, fields, rows):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


# ---------------------------------------------------------------------------- 1. games + box scores
def update_games():
    old = {r["gid"]: r for r in read(GAMES)}
    rows = dict(old)
    for s in SEASONS:
        if s != CURRENT and any(r["season"] == s for r in old.values()):
            continue                                       # finished season: already stored
        txt = fetch(EURO_GAMES.format(s=s))
        try:
            data = json.loads(txt)["data"]
        except Exception:
            print(f"  EuroLeague {s}: game list NOT readable this run")
            continue
        for g in data:
            loc, road = g.get("local") or {}, g.get("road") or {}
            code = g.get("gameCode") or g.get("code")
            gid = f"{s}_{code}"
            prev = rows.get(gid, {})
            nm = lambda side: (side.get("club") or {}).get("name") or (side.get("club") or {}).get("abbreviatedName")
            rows[gid] = {**prev, "season": s, "gid": gid, "code": code, "ko": ts_of(g.get("utcDate") or g.get("date")),
                         "phase": (g.get("phaseType") or {}).get("code"), "home": nm(loc), "away": nm(road),
                         "home_pts": loc.get("score") if g.get("played") else "",
                         "away_pts": road.get("score") if g.get("played") else "",
                         "played": int(bool(g.get("played"))), "poss": prev.get("poss", "")}
    need = [r for r in rows.values() if str(r["played"]) == "1" and not r.get("poss")][:MAX_BOX_PER_RUN]

    def box(r):
        txt = fetch(EURO_BOX.format(g=r["code"], s=r["season"]), pause=0.3)
        try:
            tot = {}
            for t in json.loads(txt)["Stats"]:
                x = t["totr"]
                tot[int(float(x["Points"]))] = poss(float(x["FieldGoalsAttempted2"]) + float(x["FieldGoalsAttempted3"]),
                                                    float(x["OffensiveRebounds"]), float(x["Turnovers"]),
                                                    float(x["FreeThrowsAttempted"]))
            a, b = tot.get(int(float(r["home_pts"]))), tot.get(int(float(r["away_pts"])))
            return r["gid"], round((a + b) / 2, 2) if a and b else "x"
        except Exception:
            return r["gid"], ""                            # try again next run
    with ThreadPoolExecutor(max_workers=2) as pool:
        for gid, p in pool.map(box, need):
            rows[gid]["poss"] = p
    out = sorted(rows.values(), key=lambda r: (int(r["ko"] or 0), r["gid"]))
    write(GAMES, G_FIELDS, out)
    played = [r for r in out if str(r["played"]) == "1"]
    print(f"  EuroLeague games: {len(played)} played ({sum(1 for r in played if r['poss'] not in ('', 'x'))}"
          f" with possessions), {len(out) - len(played)} still to play; box scores read this run: {len(need)}")
    return out


# ---------------------------------------------------------------------------- 2. ratings (pure Python)
def solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c] / M[c][c]
                M[r] = [x - f * y for x, y in zip(M[r], M[c])]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


class Ratings:
    """Weighted ridge least squares, built up game by game (fast enough without numpy).
    Weight of a game = 0.5 ** (age in days / half-life)."""

    def __init__(self, teams, t0, hl=HL):
        self.ix = {t: i for i, t in enumerate(teams)}
        self.n, self.t0, self.hl = len(teams), t0, hl
        k = 2 * self.n + 2
        self.S = [[0.0] * k for _ in range(k)]
        self.v = [0.0] * k
        self.Sp = [[0.0] * (self.n + 1) for _ in range(self.n + 1)]
        self.vp = [0.0] * (self.n + 1)
        self.poss_sum = self.poss_n = 0.0

    def add(self, g, avg_poss):
        w = 2 ** ((g["ko"] - self.t0) / 86400 / self.hl)       # grows with time = recent counts more
        ps = g["poss"] or avg_poss
        hh = 0 if g["neutral"] else 1
        i, j = self.ix[g["home"]], self.ix[g["away"]]
        n = self.n
        for off, de, pts, h in ((i, j, g["hp"], hh), (j, i, g["ap"], -hh)):
            cols = [(0, 1.0), (1 + off, 1.0), (1 + n + de, 1.0)] + ([(2 * n + 1, float(h))] if h else [])
            y = 100 * pts / ps
            for a, xa in cols:
                self.v[a] += w * xa * y
                for b, xb in cols:
                    self.S[a][b] += w * xa * xb
        if g["poss"]:
            cols = [0, 1 + i, 1 + j]
            for a in cols:
                self.vp[a] += w * g["poss"]
                for b in cols:
                    self.Sp[a][b] += w
            self.poss_sum += g["poss"]
            self.poss_n += 1

    def model(self, at):
        scale = 2 ** ((at - self.t0) / 86400 / self.hl)       # ridge relative to current weights
        n = self.n
        A = [r[:] for r in self.S]
        for k in range(1, 2 * n + 1):
            A[k][k] += LAMBDA * scale
        b = solve(A, self.v)
        Ap = [r[:] for r in self.Sp]
        for k in range(1, n + 1):
            Ap[k][k] += LAMBDA * scale
        avg = self.poss_sum / self.poss_n if self.poss_n else 72.0
        c = solve(Ap, self.vp) if self.poss_n else [avg] + [0.0] * n

        def margin(home, away, neutral):
            if home not in self.ix or away not in self.ix:
                return None
            i, j = self.ix[home], self.ix[away]
            h = 0 if neutral else b[-1]
            eh = b[0] + b[1 + i] + b[1 + n + j] + h
            ea = b[0] + b[1 + j] + b[1 + n + i] - h
            return (c[0] + c[1 + i] + c[1 + j]) / 100 * (eh - ea)
        return margin


def logistic_fit(pairs):
    """P(home wins) = 1 / (1 + exp(-(a + b * margin))), fitted by maximum likelihood."""
    a, b = 0.0, 0.1
    for _ in range(50):
        ga = gb = haa = hab = hbb = 0.0
        for m, y in pairs:
            p = 1 / (1 + math.exp(-max(-30, min(30, a + b * m))))
            ga += y - p
            gb += (y - p) * m
            w = p * (1 - p)
            haa += w
            hab += w * m
            hbb += w * m * m
        det = haa * hbb - hab * hab
        if det <= 1e-12:
            break
        da, db = (hbb * ga - hab * gb) / det, (haa * gb - hab * ga) / det
        a, b = a + da, b + db
        if abs(da) + abs(db) < 1e-9:
            break
    return a, b


def prepare(games):
    g = []
    for r in games:
        if str(r["played"]) != "1" or r["home_pts"] in ("", None) or not r["ko"]:
            continue
        p = r.get("poss")
        g.append({"gid": r["gid"], "season": r["season"], "ko": int(float(r["ko"])), "home": r["home"],
                  "away": r["away"], "hp": float(r["home_pts"]), "ap": float(r["away_pts"]),
                  "poss": float(p) if p not in ("", "x", None) else None,
                  "neutral": int(str(r.get("phase") or "").upper() == "FF")})
    return sorted(g, key=lambda x: x["ko"])


def engine(games, until):
    """Walk through history once. Returns (margin model for `until`, calibration pairs that
    were predicted before they were played, games-this-season counts) using only games
    that started 4+ h before each prediction."""
    played = prepare(games)
    if len(played) < 50:
        return None, [], {}
    teams = sorted({x["home"] for x in played} | {x["away"] for x in played}
                   | {r["home"] for r in games if r["home"]} | {r["away"] for r in games if r["away"]})
    R = Ratings(teams, played[0]["ko"])
    have = [x["poss"] for x in played if x["poss"]]
    avg = sum(have) / len(have) if have else 72.0
    pairs, k, model, block = [], 0, None, None
    cnt = {}
    for g in played + [{"ko": until + 3600, "_end": True}]:
        cut = (g["ko"] - 3600 - 4 * 3600) // BLOCK * BLOCK
        while k < len(played) and played[k]["ko"] <= cut:
            x = played[k]
            R.add(x, avg)
            key = x["season"]
            cnt[(key, x["home"])] = cnt.get((key, x["home"]), 0) + 1
            cnt[(key, x["away"])] = cnt.get((key, x["away"]), 0) + 1
            k += 1
        if k < 50:
            continue
        if block != cut:
            model, block = R.model(cut), cut
        if g.get("_end"):
            break
        enough = cnt.get((g["season"], g["home"]), 0) >= MIN_GAMES and cnt.get((g["season"], g["away"]), 0) >= MIN_GAMES
        m = model(g["home"], g["away"], g["neutral"])
        if enough and m is not None:
            pairs.append((g["ko"], m, 1.0 if g["hp"] > g["ap"] else 0.0))
    counts = {}
    for (s, t), v in cnt.items():
        if s == CURRENT:
            counts[t] = v
    return model, pairs, counts


# ---------------------------------------------------------------------------- 3. markets + prices
def upcoming_markets(t_now):
    out = []
    for page in range(6):
        ev = rs.get_json(f"{GAMMA}/events", {"series_id": SERIES, "closed": "false", "limit": 50,
                                             "offset": page * 50, "order": "startDate", "ascending": "true"})
        ev = [e for e in (ev if isinstance(ev, list) else []) if isinstance(e, dict)]
        if not ev:
            break
        for e in ev:
            title = str(e.get("title") or "").strip()
            c = [m for m in (e.get("markets") or []) if isinstance(m, dict) and is_game_winner(m)
                 and len(rs.as_list(m.get("clobTokenIds"))) == 2 and not m.get("closed")]
            main = [m for m in c if str(m.get("question") or "").strip() == title] or c
            if not main:
                continue
            m = max(main, key=lambda x: float(x.get("volume") or 0))
            ko = ts_of(m.get("gameStartTime") or e.get("startTime"))
            if ko and t_now + 3600 <= ko <= t_now + 3 * 3600:
                out.append((e, m, ko))
        time.sleep(0.1)
    return out


def taker_cost(token, fee_rate):
    """Average price to buy $STAKE at the REAL asks, level by level, + fee. None = too thin."""
    bk = rs.get_json(BOOK, {"token_id": token})
    asks = sorted(((float(a["price"]), float(a["size"])) for a in (bk or {}).get("asks", [])
                   if float(a.get("size") or 0) > 0), key=lambda x: x[0])
    spend, sh = 0.0, 0.0
    for p, s in asks:
        take = min(s, (STAKE - spend) / p)
        spend += take * p
        sh += take
        if spend >= STAKE - 1e-6:
            break
    if spend < STAKE - 1e-6 or sh <= 0:
        return None, None, None
    avg = spend / sh
    fee = fee_rate * avg * (1 - avg)
    return avg, fee, avg + fee


def mid_price(token):
    """(best bid + best ask) / 2 -- only for the accuracy comparison, never for betting."""
    bk = rs.get_json(BOOK, {"token_id": token}) or {}
    b = [float(x["price"]) for x in bk.get("bids", []) if float(x.get("size") or 0) > 0]
    a = [float(x["price"]) for x in bk.get("asks", []) if float(x.get("size") or 0) > 0]
    return (max(b) + min(a)) / 2 if b and a else ""


def fee_rate_of(m):
    if not m.get("feesEnabled"):
        return 0.0
    fs = m.get("feeSchedule")
    try:
        fs = json.loads(fs) if isinstance(fs, str) else (fs or {})
        return float(fs.get("rate") or 0.0)
    except (ValueError, AttributeError, TypeError):
        return 0.05                                        # fee on but unreadable: assume 5%


# ---------------------------------------------------------------------------- 4. run
def settle(bets, log):
    open_cids = {b["cid"] for b in bets if b["status"] == "open"} | {l["cid"] for l in log if l["a_won"] == ""}
    res = {}
    for cid in open_cids:
        m = rs.get_json(f"{GAMMA}/markets", {"condition_ids": cid})
        m = m[0] if isinstance(m, list) and m else None
        if not m:
            continue
        px = [str(x) for x in rs.as_list(m.get("outcomePrices"))]
        if m.get("closed") and px in (["1", "0"], ["0", "1"]):
            res[cid] = (rs.as_list(m.get("clobTokenIds")), px)
    for b in bets:
        if b["status"] == "open" and b["cid"] in res:
            toks, px = res[b["cid"]]
            won = px[toks.index(b["token"])] == "1" if b["token"] in toks else None
            if won is None:
                continue
            pay = float(b["shares"]) if won else 0.0
            b.update(status="won" if won else "lost", settled=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     payout=round(pay, 4), pnl=round(pay - STAKE, 4))
    for l in log:
        if l["a_won"] == "" and l["cid"] in res:
            l["a_won"] = 1 if res[l["cid"]][1][0] == "1" else 0
    return bets, log


def run():
    t = now()
    games = update_games()
    bets, log = read(BETS), read(LOG)
    bets, log = settle(bets, log)
    model, pairs, counts = engine(games, t)
    cal = [(m, y) for ko, m, y in pairs if ko <= t - 4 * 3600][-400:]
    seen = {l["cid"] for l in log}
    mk = [x for x in upcoming_markets(t) if x[1].get("conditionId") not in seen]
    print(f"  calibration games: {len(cal)} (need {MIN_CAL})   games tipping off in 1-3 h: {len(mk)}")
    if model is None or len(cal) < MIN_CAL:
        mk = []
    else:
        a, b = logistic_fit(cal)
        print(f"  confidence curve: a={a:.3f}  b={b:.4f} per point of margin")
    fixtures = [r for r in games if r["season"] == CURRENT and str(r["played"]) != "1" and r["ko"]]
    for e, m, ko in mk:
        outs = rs.as_list(m.get("outcomes"))
        toks = [str(x) for x in rs.as_list(m.get("clobTokenIds"))]
        best, score, a_home = None, 0.0, None
        for f in fixtures:
            if abs(int(float(f["ko"])) - ko) > 6 * 3600:
                continue
            for ah in (True, False):
                h, w_ = (outs[0], outs[1]) if ah else (outs[1], outs[0])
                s = min(same_team(h, f["home"]), same_team(w_, f["away"]))
                if s > score:
                    best, score, a_home = f, s, ah
        row = {"time": datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds"), "ko": ko,
               "game": e.get("title"), "cid": m.get("conditionId"), "team_a": outs[0], "a_won": ""}
        if not best or score < 0.75:
            log.append({**row, "decision": "no fixture match"})
            continue
        enough = counts.get(best["home"], 0) >= MIN_GAMES and counts.get(best["away"], 0) >= MIN_GAMES
        mg = model(best["home"], best["away"], int(str(best.get("phase") or "").upper() == "FF"))
        if mg is None or not enough:
            log.append({**row, "decision": "too few games played"})
            continue
        qh = 1 / (1 + math.exp(-(a + b * mg)))
        qa = qh if a_home else 1 - qh
        fr = fee_rate_of(m)
        av_a, fee_a, ca = taker_cost(toks[0], fr)
        av_b, fee_b, cb = taker_cost(toks[1], fr)
        mid = mid_price(toks[0])
        dec = "no bet"
        pick = None
        if ca is not None and qa - ca >= EDGE:
            pick = (outs[0], toks[0], qa, av_a, fee_a, ca)
        if cb is not None and (1 - qa) - cb >= EDGE and (pick is None or (1 - qa) - cb > qa - ca):
            pick = (outs[1], toks[1], 1 - qa, av_b, fee_b, cb)
        if ca is None and cb is None:
            dec = "book too thin"
        if pick:
            team, tok, q, av, fee, cost = pick
            bets.append({"placed": row["time"], "ko": ko, "game": e.get("title"), "cid": m.get("conditionId"),
                         "team": team, "token": tok, "q": round(q, 4), "avg_ask": round(av, 4),
                         "fee": round(fee, 5), "cost": round(cost, 4), "shares": round(STAKE / cost, 4),
                         "status": "open", "settled": "", "payout": "", "pnl": ""})
            dec = f"BET {team} at {cost:.3f} (our chance {q:.3f})"
        log.append({**row, "q_a": round(qa, 4), "ask_a": "" if av_a is None else round(av_a, 4),
                    "ask_b": "" if av_b is None else round(av_b, 4),
                    "mid_a": "" if mid == "" else round(mid, 4), "decision": dec})
        print(f"  {e.get('title')}: {dec}")
    write(BETS, B_FIELDS, bets)
    write(LOG, L_FIELDS, log)
    summary(bets, log)


def summary(bets=None, log=None):
    bets = read(BETS) if bets is None else bets
    log = read(LOG) if log is None else log
    done = [b for b in bets if b["status"] in ("won", "lost")]
    pnl = sum(float(b["pnl"]) for b in done)
    print(f"\n  #37 EUROLEAGUE PAPER: {len(bets)} bets, {len(done)} settled, {sum(b['status'] == 'won' for b in done)} won,"
          f" profit ${pnl:.2f}" + (f" ({pnl / (len(done) * STAKE):+.1%} per bet)" if done else ""))
    sc = [l for l in log if l["a_won"] != "" and l.get("q_a") not in ("", None) and l.get("mid_a") not in ("", None)]
    if sc:
        bq = sum((float(l["q_a"]) - float(l["a_won"])) ** 2 for l in sc) / len(sc)
        bm = sum((float(l["mid_a"]) - float(l["a_won"])) ** 2 for l in sc) / len(sc)
        print(f"  accuracy on {len(sc)} finished games (Brier, lower = better): our model {bq:.4f}   market {bm:.4f}")
    print(f"  verdict after 60+ settled bets: PASS only if profit > 0 AND our model beats the market's accuracy"
          f"  ({len(done)}/60 so far)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", action="store_true")
    if ap.parse_args().summary:
        summary()
        return
    run()


if __name__ == "__main__":
    main()
