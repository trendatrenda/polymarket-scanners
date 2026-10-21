"""
pm_hoops_download.py  --  data for "basketball team efficiency vs Polymarket"
==========================================================================
Read-only, resumable (everything is cached; if it stops, just run it again).
    python pm_hoops_download.py                 (~30-60 min the first time)

Leagues: NBA, EuroLeague, WNBA (college left out: Polymarket barely covers it).
  1. Polymarket: every finished game-winner market. ONE market per game: the one whose
     title is the game itself ("A vs. B"), most money traded. Extra/duplicate markets are
     dropped and counted.
  2. Polymarket price of team A 1 HOUR BEFORE TIP-OFF = the last REAL trade in the 2 hours
     before that moment (none -> no price). Only trades before the bet moment are used.
  3. Box scores (free): ESPN for NBA/WNBA, the EuroLeague's own feed. Possessions per team
     = shots - offensive rebounds + turnovers + 0.44 x free throws (the standard formula).
  4. Pairs every Polymarket game with its box score (same teams, tip-off within 6 h) and
     checks the winner agrees (should be ~100%).
Files: hoops_poly.csv, hoops_games.csv, hoops_pairs.csv (+ hoops_cache/).
"""
import csv
import json
import re
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import pm_rewards_scanner as rs
import pm_trades_api as ta

HERE = Path(__file__).resolve().parent
CACHE = HERE / "hoops_cache"
POLY, GAMES, PAIRS = HERE / "hoops_poly.csv", HERE / "hoops_games.csv", HERE / "hoops_pairs.csv"
GAMMA = "https://gamma-api.polymarket.com/events"
SERIES = {"NBA": "10345", "EuroLeague": "10371", "WNBA": "10105"}
SINCE = "2025-05-01"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/{p}/{what}"
ESPN_PATH = {"NBA": "nba", "WNBA": "wnba"}
ESPN_DAYS = {"NBA": [(date(2025, 10, 1), date(2026, 6, 30)), (date(2026, 10, 1), None)],
             "WNBA": [(date(2025, 5, 1), date(2025, 10, 31)), (date(2026, 5, 1), date(2026, 10, 31))]}
EURO_GAMES = "https://api-live.euroleague.net/v2/competitions/E/seasons/{s}/games"
EURO_BOX = "https://live.euroleague.net/api/Boxscore?gamecode={g}&seasoncode={s}"
EURO_SEASONS = ["E2025", "E2026"]
POLY_FIELDS = ["league", "event", "title", "team_a", "team_b", "a_won", "ko", "cid", "tok_a", "tok_b",
               "volume", "candidates", "fee_raw", "price_a", "price_src", "age_s"]
GAME_FIELDS = ["league", "season", "gid", "ko", "stype", "neutral", "home", "away", "home_pts", "away_pts",
               "home_poss", "away_poss"]


# ---------------------------------------------------------------------------- helpers
def fetch(url, tries=3, pause=0.0):
    for i in range(tries):
        try:
            time.sleep(pause)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research)",
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception:
            time.sleep(3 * (i + 1))
    return None


def cached_json(name, url, pause=0.0):
    p = CACHE / name
    if p.exists():
        return json.loads(p.read_text())
    txt = fetch(url, pause=pause)
    if txt is None:
        return None
    try:
        j = json.loads(txt)
    except ValueError:
        return None
    p.write_text(json.dumps(j))
    return j


def ts_of(s):
    s = str(s or "").replace("Z", "+00:00").replace(" ", "T")
    if re.search(r"[+-]\d\d$", s):
        s += ":00"
    try:
        d = datetime.fromisoformat(s)
        return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp())
    except ValueError:
        return None


STOP = {"fc", "bc", "b", "c", "kk", "bk", "sk", "as", "sad", "club", "basketball", "basket", "the", "de", "cf"}


def words(name):
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    w = re.findall(r"[a-z0-9]+", s)
    return [x for x in w if x not in STOP] or w


def same_team(a, b):
    """1.0 = surely the same team. Word overlap (share of the SHORTER name) or spelling
    similarity, whichever is higher."""
    x, y = set(words(a)), set(words(b))
    ov = len(x & y) / min(len(x), len(y)) if x and y else 0.0
    return max(ov, SequenceMatcher(None, " ".join(sorted(x)), " ".join(sorted(y))).ratio())


# ---------------------------------------------------------------------------- 1. Polymarket games
def is_game_winner(m):
    if str(m.get("sportsMarketType") or "").lower() == "moneyline":
        return True
    outs = [str(o).lower() for o in rs.as_list(m.get("outcomes"))]
    q = str(m.get("question") or "")
    return len(outs) == 2 and outs != ["yes", "no"] and not re.search(
        r"spread|o/u|over|under|total|points|1h|2h|1st|2nd|half|quarter|q[1-4]", q, re.I)


def poly_games():
    out, dropped, unclear = [], 0, 0
    for league, sid in SERIES.items():
        n0 = len(out)
        for page in range(400):
            ev = rs.get_json(GAMMA, {"series_id": sid, "closed": "true", "limit": 50, "offset": page * 50,
                                     "order": "startDate", "ascending": "false"})
            ev = [e for e in (ev if isinstance(ev, list) else []) if isinstance(e, dict)]
            if not ev:
                break
            old = 0
            for e in ev:
                title = str(e.get("title") or "").strip()
                cands = [m for m in (e.get("markets") or []) if isinstance(m, dict) and is_game_winner(m)
                         and [str(p) for p in rs.as_list(m.get("outcomePrices"))] in (["0", "1"], ["1", "0"])
                         and len(rs.as_list(m.get("clobTokenIds"))) == 2]
                main = [m for m in cands if str(m.get("question") or "").strip() == title] or cands
                if not main:
                    continue
                dropped += len(cands) - 1
                m = max(main, key=lambda x: float(x.get("volume") or 0))
                outs = rs.as_list(m.get("outcomes"))
                toks = rs.as_list(m.get("clobTokenIds"))
                ko = ts_of(m.get("gameStartTime") or e.get("startTime"))
                if len(outs) != 2 or not ko:
                    unclear += 1
                    continue
                if datetime.fromtimestamp(ko, timezone.utc).strftime("%Y-%m-%d") < SINCE:
                    old += 1
                    continue
                out.append({"league": league, "event": e.get("id"), "title": title, "team_a": outs[0],
                            "team_b": outs[1], "a_won": 1 if str(rs.as_list(m.get("outcomePrices"))[0]) == "1" else 0,
                            "ko": ko, "cid": m.get("conditionId"), "tok_a": str(toks[0]), "tok_b": str(toks[1]),
                            "volume": round(float(m.get("volume") or 0), 2), "candidates": len(cands),
                            "fee_raw": json.dumps({k: m.get(k) for k in ("feesEnabled", "feeSchedule", "feeType",
                                                                         "makerBaseFee", "takerBaseFee") if k in m})})
            if old >= len(ev):
                break
            time.sleep(0.15)
        print(f"    Polymarket {league}: {len(out) - n0} games")
    print(f"    extra game-winner markets dropped (one market per game kept): {dropped};"
          f" unclear games skipped: {unclear}")
    seen, uniq = set(), []
    for r in out:
        if r["cid"] not in seen:
            seen.add(r["cid"])
            uniq.append(r)
    return uniq


def pre_price(r):
    """Team A's price 1 h before tip-off: last REAL trade in the 2 h before that moment."""
    t = int(r["ko"]) - 3600
    got = ta.trades_between(rs.get_json, r["cid"], t - 7200, t, log=lambda x: None, max_pages=300,
                            tokens=(r["tok_a"], r["tok_b"]))
    if got is None:
        return None, "unsure", ""
    if not got[0]:
        return None, "no trades", ""
    ts, fills, _, _ = max(got[0], key=lambda x: x[0])
    sz = sum(s for _, s in fills)
    return (sum(p * s for p, s in fills) / sz, "trade", t - ts) if sz > 0 and ts <= t else (None, "none", "")


def download_poly():
    done = {}
    if POLY.exists():
        with POLY.open(newline="") as f:
            done = {r["cid"]: r for r in csv.DictReader(f) if r["price_src"] != "unsure"}
    games = poly_games()
    todo = [g for g in games if g["cid"] not in done]
    print(f"    {len(done)} priced already, {len(todo)} to price...")
    keep = list(done.values())
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for i, (g, (p, src, age)) in enumerate(zip(todo, pool.map(pre_price, todo)), 1):
            keep.append({**g, "price_a": "" if p is None else round(p, 4), "price_src": src, "age_s": age})
            if i % 100 == 0:
                _write(POLY, POLY_FIELDS, keep)
                print(f"    {i}/{len(todo)} priced   about {(time.time() - t0) / i * (len(todo) - i) / 60:.0f} min left")
    _write(POLY, POLY_FIELDS, keep)
    src = {}
    for r in keep:
        src[r["price_src"]] = src.get(r["price_src"], 0) + 1
    print(f"    price 1 h before tip-off: {src}")


def _write(path, fields, rows):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


# ---------------------------------------------------------------------------- 2. box scores
def poss(fga, oreb, tov, fta):
    return fga - oreb + tov + 0.44 * fta


def espn_team_poss(team):
    st = {s.get("name"): s.get("displayValue") for s in team.get("statistics", [])}
    try:
        fga = float(str(st["fieldGoalsMade-fieldGoalsAttempted"]).split("-")[1])
        fta = float(str(st["freeThrowsMade-freeThrowsAttempted"]).split("-")[1])
        oreb = float(st["offensiveRebounds"])
        tov = float(st.get("totalTurnovers") or st["turnovers"])
        return poss(fga, oreb, tov, fta)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def espn_games(league):
    rows, today = [], date.today()
    days = []
    for a, b in ESPN_DAYS[league]:
        d = a
        while d <= min(b or today, today - timedelta(days=1)):
            days.append(d)
            d += timedelta(days=1)
    path = ESPN_PATH[league]

    def day_games(d):
        j = cached_json(f"espn_{league}_{d:%Y%m%d}.json",
                        ESPN.format(p=path, what="scoreboard") + f"?dates={d:%Y%m%d}&limit=200")
        out = []
        for e in (j or {}).get("events", []):
            comp = (e.get("competitions") or [{}])[0]
            if not (comp.get("status") or e.get("status") or {}).get("type", {}).get("completed"):
                continue
            c = {x.get("homeAway"): x for x in comp.get("competitors", [])}
            if "home" not in c or "away" not in c:
                continue
            out.append({"league": league, "season": (e.get("season") or {}).get("year"),
                        "gid": e.get("id"), "ko": ts_of(e.get("date")),
                        "stype": (e.get("season") or {}).get("type"), "neutral": int(bool(comp.get("neutralSite"))),
                        "home": c["home"]["team"].get("displayName"), "away": c["away"]["team"].get("displayName"),
                        "home_pts": c["home"].get("score"), "away_pts": c["away"].get("score")})
        return out

    with ThreadPoolExecutor(max_workers=4) as pool:
        for got in pool.map(day_games, days):
            rows += got

    def box(g):
        j = cached_json(f"espnbox_{league}_{g['gid']}.json", ESPN.format(p=path, what="summary") + f"?event={g['gid']}")
        teams = ((j or {}).get("boxscore") or {}).get("teams", [])
        by = {t.get("homeAway"): espn_team_poss(t) for t in teams}
        return {**g, "home_poss": by.get("home"), "away_poss": by.get("away")}

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(box, rows))
    ok = sum(1 for g in rows if g["home_poss"] and g["away_poss"])
    print(f"    {league}: {len(rows)} finished games from ESPN, possessions for {ok}")
    return rows


def euro_games():
    rows, box_ok = [], 0
    for s in EURO_SEASONS:
        j = cached_json(f"euro_games_{s}.json", EURO_GAMES.format(s=s))
        data = (j or {}).get("data") if isinstance(j, dict) else None
        if not data:
            print(f"    EuroLeague {s}: game list NOT readable")
            continue
        for g in data:
            if not g.get("played"):
                continue
            loc, road = g.get("local") or {}, g.get("road") or {}
            name = lambda side: (side.get("club") or {}).get("name") or (side.get("club") or {}).get("abbreviatedName")
            code = g.get("gameCode") or g.get("code")
            rows.append({"league": "EuroLeague", "season": s, "gid": f"{s}_{code}", "code": code,
                         "ko": ts_of(g.get("utcDate") or g.get("date")), "stype": (g.get("phaseType") or {}).get("code"),
                         "neutral": int(str((g.get("phaseType") or {}).get("code") or "").upper() in ("FF",)),
                         "home": name(loc), "away": name(road), "home_pts": loc.get("score"),
                         "away_pts": road.get("score"), "home_poss": None, "away_poss": None})

    def box(g):
        j = cached_json(f"eurobox_{g['gid']}.json", EURO_BOX.format(g=g["code"], s=g["season"]), pause=0.3)
        try:
            tot = {}
            for t in j["Stats"]:
                x = t["totr"]
                p = poss(float(x["FieldGoalsAttempted2"]) + float(x["FieldGoalsAttempted3"]),
                         float(x["OffensiveRebounds"]), float(x["Turnovers"]), float(x["FreeThrowsAttempted"]))
                tot[int(float(x["Points"]))] = p
            hp, ap = tot.get(int(float(g["home_pts"]))), tot.get(int(float(g["away_pts"])))
            return {**g, "home_poss": hp, "away_poss": ap}
        except Exception:
            return g

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(box, rows))
    box_ok = sum(1 for g in rows if g["home_poss"] and g["away_poss"])
    print(f"    EuroLeague: {len(rows)} finished games, possessions for {box_ok}"
          + ("" if box_ok > 0.8 * max(1, len(rows)) else
             "  -> box scores NOT readable: EuroLeague ratings will use points with an average pace"))
    return rows


def download_games():
    CACHE.mkdir(exist_ok=True)
    rows = []
    for lg in ("NBA", "WNBA"):
        rows += espn_games(lg)
    rows += euro_games()
    good = [g for g in rows if g["ko"] and g["home_pts"] not in (None, "") and g["away_pts"] not in (None, "")]
    bad = {}
    for g in rows:
        if g not in good:
            bad[g["league"]] = bad.get(g["league"], 0) + 1
    if bad:
        print(f"    games left out (no time or score readable): {bad}  -- if large, tell Claude")
    rows = good
    _write(GAMES, GAME_FIELDS, rows)
    return rows


# ---------------------------------------------------------------------------- 3. pairing
def pair():
    with POLY.open(newline="") as f:
        poly = list(csv.DictReader(f))
    with GAMES.open(newline="") as f:
        games = list(csv.DictReader(f))
    by = {}
    for g in games:
        by.setdefault(g["league"], []).append(g)
    out, miss, wrong = [], {}, 0
    for r in poly:
        ko = int(r["ko"])
        best, score = None, 0.0
        for g in by.get(r["league"], []):
            if abs(int(g["ko"]) - ko) > 6 * 3600:
                continue
            for a_home in (True, False):
                h, a = (r["team_a"], r["team_b"]) if a_home else (r["team_b"], r["team_a"])
                s = min(same_team(h, g["home"]), same_team(a, g["away"]))
                if s > score:
                    best, score, side = g, s, a_home
        if not best or score < 0.75:
            miss[r["league"]] = miss.get(r["league"], 0) + 1
            continue
        hp, ap = float(best["home_pts"]), float(best["away_pts"])
        a_won_box = int((hp > ap) == side)
        if a_won_box != int(r["a_won"]):
            wrong += 1
            continue
        out.append({**r, "gid": best["gid"], "a_home": int(side)})
    _write(PAIRS, list(poly[0].keys()) + ["gid", "a_home"] if poly else ["league"], out)
    per = {}
    for r in out:
        per[r["league"]] = per.get(r["league"], 0) + 1
    print(f"    paired: {per}   not paired: {miss}")
    print(f"    winner check: {wrong} pairs where the box score's winner disagrees with Polymarket"
          f" (left out; should be ~0 -- if not, tell Claude)")


def main():
    print("=" * 78)
    print(" 1) Polymarket game-winner markets + price 1 h before tip-off")
    download_poly()
    print(" 2) box scores")
    download_games()
    print(" 3) pairing")
    pair()
    print(" Done. Next: python pm_hoops_test.py")
    print("=" * 78)


if __name__ == "__main__":
    main()
