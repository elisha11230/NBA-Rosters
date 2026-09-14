#!/usr/bin/env python3
"""
build_depth_chart.py
--------------------
Builds the baked depth-chart JSON for the NBA app from ESPN's public endpoints.
No API key. Two lanes of data, joined per team:

  depthcharts endpoint -> ordering within each position + injury flags
  roster endpoint      -> authoritative single position, jersey, headshot, bio
  (optional) core stats -> season PPG / RPG / APG per player

Run in an open-network environment (same posture as the NFL pipeline):

  python build_depth_chart.py                 # all 30 teams, no stats
  python build_depth_chart.py --stats         # all 30 teams, with season averages
  python build_depth_chart.py --team 25       # single team (OKC), quick check

Emits depth.json next to this file. The HTML app reads that file.

Known ESPN gotchas handled here:
  * default python-requests User-Agent can 403 -> we send a browser UA
  * $ref URLs point at sports.core.api.espn.pvt -> swap .pvt for .com
  * depthcharts cross-lists a player in every eligible lane -> we assign ONE
    primary lane from the roster position, so no dedup guesswork is needed
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"
HEADSHOT = "https://a.espncdn.com/i/headshots/nba/players/full/{id}.png"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

LANE_ORDER = ["PG", "SG", "SF", "PF", "C"]
LANE_FULL = {"PG": "Point Guard", "SG": "Shooting Guard", "SF": "Small Forward",
             "PF": "Power Forward", "C": "Center"}
ROLES = ["Starter", "2nd unit", "3rd", "4th", "5th"]


# ---------------------------------------------------------------- http
def fetch_json(url, tries=3, pause=0.6):
    url = url.replace("sports.core.api.espn.pvt", "sports.core.api.espn.com")
    last = None
    for attempt in range(tries):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as e:
            last = e
            time.sleep(pause * (attempt + 1))
    raise RuntimeError(f"fetch failed: {url} :: {last}")


# ---------------------------------------------------------------- teams
def get_teams():
    data = fetch_json(f"{SITE}/teams")
    out = []
    for t in data["sports"][0]["leagues"][0]["teams"]:
        team = t["team"]
        logo = team.get("logos", [{}])[0].get("href", "")
        out.append({
            "id": team["id"],
            "abbr": team.get("abbreviation", ""),
            "name": team.get("displayName", ""),
            "color": team.get("color", "1d428a"),
            "logo": logo,
        })
    return out


# ---------------------------------------------------------------- roster
def get_roster(team_id):
    """id -> {pos, jersey, headshot, name} using the roster's single position."""
    data = fetch_json(f"{SITE}/teams/{team_id}/roster")
    out = {}
    for a in data.get("athletes", []):
        # NBA roster is usually flat, but tolerate a grouped shape too
        items = a.get("items") if isinstance(a, dict) and "items" in a else [a]
        for p in items:
            pid = str(p.get("id"))
            if not pid or pid == "None":
                continue
            pos = (p.get("position") or {}).get("abbreviation", "")
            out[pid] = {
                "pos": pos.upper(),
                "jersey": p.get("jersey", ""),
                "headshot": (p.get("headshot") or {}).get("href") or HEADSHOT.format(id=pid),
                "name": p.get("displayName", ""),
                "age": p.get("age"),
            }
    return out


# ---------------------------------------------------------------- depth
def get_depth(team_id):
    """Returns (team_meta, {LANE: [ {id,name,depth_index,injury} ordered ]})."""
    data = fetch_json(f"{SITE}/teams/{team_id}/depthcharts")
    tm = data.get("team", {})
    season = data.get("season", {})
    meta = {
        "id": str(tm.get("id", team_id)),
        "abbr": tm.get("abbreviation", ""),
        "name": tm.get("displayName", ""),
        "color": tm.get("color", "1d428a"),
        "logo": tm.get("logo", ""),
        "standing": tm.get("standingSummary", ""),
        "seasonYear": season.get("year"),
        "seasonType": season.get("name", ""),
        "seasonDisplay": tm.get("seasonSummary", ""),
        "asOf": data.get("timestamp", ""),
    }
    lanes = {}
    positions = (data.get("depthchart") or [{}])[0].get("positions", {})
    for key, block in positions.items():
        lane = key.upper()
        if lane not in LANE_ORDER:
            continue
        rows = []
        for idx, ath in enumerate(block.get("athletes", [])):
            injuries = ath.get("injuries") or []
            injury = injuries[0].get("type", {}).get("description") if injuries else None
            rows.append({
                "id": str(ath.get("id")),
                "name": ath.get("displayName", ""),
                "depth_index": idx,
                "injury": (injury or "").replace("-", " ").strip().capitalize() or None,
            })
        lanes[lane] = rows
    return meta, lanes


# ---------------------------------------------------------------- team panel (age + schedule)
def age_summary(roster):
    named = [(r["name"], r["age"]) for r in roster.values()
             if isinstance(r.get("age"), (int, float))]
    if not named:
        return None
    ages = [a for _, a in named]
    named.sort(key=lambda x: x[1])
    return {
        "avg": round(sum(ages) / len(ages), 1),
        "count": len(ages),
        "buckets": {
            "young": sum(1 for a in ages if a <= 23),
            "prime": sum(1 for a in ages if 24 <= a <= 29),
            "vet": sum(1 for a in ages if a >= 30),
        },
        "youngest": {"name": named[0][0], "age": named[0][1]},
        "oldest": {"name": named[-1][0], "age": named[-1][1]},
    }


def get_schedule(team_id, season_year, limit=6):
    url = f"{SITE}/teams/{team_id}/schedule"
    if season_year:
        url += f"?season={season_year}"
    try:
        data = fetch_json(url)
    except RuntimeError:
        return []
    out = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        cs = comp.get("competitors", [])
        me = next((c for c in cs if str((c.get("team") or {}).get("id")) == str(team_id)), None)
        opp = next((c for c in cs if c is not me), None)
        if not opp:
            continue
        ot = opp.get("team", {})
        out.append({
            "date": ev.get("date", ""),
            "home": (me or {}).get("homeAway") == "home",
            "opp": ot.get("abbreviation") or ot.get("displayName", ""),
            "oppName": ot.get("displayName", ""),
            "oppLogo": (ot.get("logos") or [{}])[0].get("href") or ot.get("logo", ""),
        })
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- stats (optional)
def get_averages(athlete_id, season_year):
    """Season PPG / RPG / APG from the core athlete statistics endpoint. None on miss."""
    url = f"{CORE}/seasons/{season_year}/types/2/athletes/{athlete_id}/statistics"
    try:
        data = fetch_json(url, tries=2)
    except RuntimeError:
        return None
    want = {"avgPoints": "ppg", "avgRebounds": "rpg", "avgAssists": "apg"}
    out = {}
    for cat in data.get("splits", {}).get("categories", []):
        for st in cat.get("stats", []):
            if st.get("name") in want:
                out[want[st["name"]]] = round(float(st.get("value", 0)), 1)
    return out or None


# ---------------------------------------------------------------- transform (pure, testable)
def resolve_lane(roster_pos, depth_index_by_lane):
    """Pick ONE lane for a player. Roster's single position is authoritative;
    only generic G / F fall back to wherever the depth chart ranks him highest."""
    p = (roster_pos or "").upper()
    if p in LANE_ORDER:
        return p
    if p == "G":
        return _highest(depth_index_by_lane, ["PG", "SG"], default="SG")
    if p == "F":
        return _highest(depth_index_by_lane, ["SF", "PF"], default="SF")
    if p in ("C", "C-F", "FC"):
        return "C"
    # unknown / two-way with no position -> best available depth listing
    return _highest(depth_index_by_lane, LANE_ORDER, default="SF")


def _highest(depth_index_by_lane, candidates, default):
    best, best_idx = None, 999
    for lane in candidates:
        if lane in depth_index_by_lane and depth_index_by_lane[lane] < best_idx:
            best, best_idx = lane, depth_index_by_lane[lane]
    return best or default


def assemble_lanes(depth_lanes, roster_by_id):
    """Join depth ordering with roster positions -> each player once, in one lane.

    depth_lanes:   {LANE: [ {id,name,depth_index,injury} ]}
    roster_by_id:  {id: {pos,jersey,headshot,name}}
    returns:       {LANE: [ {id,name,jersey,headshot,role,injury,depth_index} ]}
    """
    # where does each player appear across the depth chart, and at what rank
    seen = {}  # id -> {name, injury, by_lane:{LANE:index}}
    for lane, rows in depth_lanes.items():
        for row in rows:
            rec = seen.setdefault(row["id"], {"name": row["name"], "injury": row["injury"], "by_lane": {}})
            rec["by_lane"][lane] = row["depth_index"]
            if row["injury"] and not rec["injury"]:
                rec["injury"] = row["injury"]

    # include rostered players who never appear on the depth chart (deep bench)
    for pid, r in roster_by_id.items():
        seen.setdefault(pid, {"name": r["name"], "injury": None, "by_lane": {}})

    out = {lane: [] for lane in LANE_ORDER}
    for pid, rec in seen.items():
        rpos = roster_by_id.get(pid, {}).get("pos", "")
        lane = resolve_lane(rpos, rec["by_lane"])
        # sort key: players listed at this lane keep their depth order; others trail
        rank = rec["by_lane"].get(lane, 900 + len(out[lane]))
        info = roster_by_id.get(pid, {})
        out[lane].append({
            "id": pid,
            "name": rec["name"] or info.get("name", ""),
            "jersey": info.get("jersey", ""),
            "headshot": info.get("headshot") or HEADSHOT.format(id=pid),
            "injury": rec["injury"],
            "_rank": rank,
        })

    for lane in LANE_ORDER:
        out[lane].sort(key=lambda x: x["_rank"])
        for i, p in enumerate(out[lane]):
            p["role"] = ROLES[i] if i < len(ROLES) else f"{i+1}th"
            p.pop("_rank", None)
    return out


# ---------------------------------------------------------------- build
def build_team(team_id, with_stats=False):
    meta, depth_lanes = get_depth(team_id)
    roster = get_roster(team_id)
    lanes = assemble_lanes(depth_lanes, roster)

    if with_stats and meta.get("seasonYear"):
        for lane in LANE_ORDER:
            for p in lanes[lane]:
                p["stats"] = get_averages(p["id"], meta["seasonYear"])
                time.sleep(0.15)

    panel = {
        "age": age_summary(roster),
        "schedule": get_schedule(meta["id"], meta.get("seasonYear")),
    }

    return {
        "id": meta["id"], "abbr": meta["abbr"], "name": meta["name"],
        "color": meta["color"], "logo": meta["logo"], "standing": meta["standing"],
        "season": meta["seasonDisplay"], "seasonType": meta["seasonType"],
        "asOf": meta["asOf"], "lanes": lanes, "panel": panel,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", help="single team id (e.g. 25 = OKC)")
    ap.add_argument("--stats", action="store_true", help="also fetch season PPG/RPG/APG")
    ap.add_argument("--out", default="depth.json")
    args = ap.parse_args()

    if args.team:
        teams = [{"id": args.team}]
    else:
        teams = get_teams()

    built = []
    for t in teams:
        try:
            row = build_team(t["id"], with_stats=args.stats)
            built.append(row)
            print(f"  ok  {row['abbr'] or t['id']:4}  "
                  + " ".join(f"{k}:{len(v)}" for k, v in row["lanes"].items()), file=sys.stderr)
        except Exception as e:
            print(f"  FAIL {t['id']}: {e}", file=sys.stderr)
        time.sleep(0.4)

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "teamCount": len(built),
        "teams": built,
    }
    with open(args.out, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"wrote {args.out} :: {len(built)} teams", file=sys.stderr)


if __name__ == "__main__":
    main()
