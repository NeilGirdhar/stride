"""Stride — Strava sync.

Pulls your Strava activity history (all sports) and writes under data/:
  imported/strava-activities.json  raw activity list, deduped by id
  generated/fitness-summary.json   compact derived metrics
  private/strava-tokens.json       OAuth tokens (gitignored)

The raw history is fetched in full once; subsequent syncs only pull activities
newer than the latest one already stored, then merge.

Also writes (after `details`):
  imported/strava-run-details.json  per-run cache: shoes (gear_id), photos, best efforts
  imported/strava-gear.json         gear_id -> shoe name + mileage
  generated/records.json            best efforts: 400m, 1k, 5k, 10k, half, 30k, marathon

Usage:
  uv run stride-sync auth            one-time browser authorization
  uv run stride-sync sync            fetch new activities + recompute summary
  uv run stride-sync sync --full     ignore checkpoint, refetch everything
  uv run stride-sync details         backfill per-run shoes + PRs (resumable)
  uv run stride-sync details --limit=100   do it in chunks (rate limits)

No third-party dependencies — Python 3 standard library only.
"""

import json
import operator
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast

ROOT = pathlib.Path(
    pathlib.Path(pathlib.Path(pathlib.Path(__file__).resolve()).parent).parent
).parent
DATA_DIR = ROOT / "data"
IMPORTED_DIR = DATA_DIR / "imported"
GENERATED_DIR = DATA_DIR / "generated"
ENTERED_DIR = DATA_DIR / "entered"
PRIVATE_DIR = DATA_DIR / "private"
CONFIG_PATH = ROOT / "scripts" / "strava_config.json"
TOKENS_PATH = PRIVATE_DIR / "strava-tokens.json"
RAW_PATH = IMPORTED_DIR / "strava-activities.json"
SUMMARY_PATH = GENERATED_DIR / "fitness-summary.json"
DETAILS_PATH = IMPORTED_DIR / "strava-run-details.json"  # cache: id -> detail
GEAR_PATH = IMPORTED_DIR / "strava-gear.json"  # gear_id -> shoe
RECORDS_PATH = GENERATED_DIR / "records.json"  # best efforts
CLUB_OVERRIDES_PATH = ENTERED_DIR / "club-overrides.json"
CLUB_PATTERNS_PATH = ENTERED_DIR / "club-patterns.json"

AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"  # noqa: S105
ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
ACTIVITY_URL = "https://www.strava.com/api/v3/activities"
GEAR_URL = "https://www.strava.com/api/v3/gear"
SCOPE = "read,activity:read_all"

RUN_TYPES = ("Run", "TrailRun")
# Strava best_effort name -> our record key. Strava computes these per run.
# Note Strava's casing: "400m" but "1K"/"5K"/"10K"/"30K"/"Marathon".
BEST_EFFORT_KEYS = {
    "400m": "400m",
    "1K": "1k",
    "5K": "5k",
    "10K": "10k",
    "Half-Marathon": "half",
    "30K": "30k",
    "Marathon": "marathon",
}
# Fallback for 30k/marathon on long runs where Strava didn't flag a best effort
# — found as the fastest window from the distance/time streams.
STREAM_RECORDS = {"30k": 30000, "marathon": 42195}
JsonDict = dict[str, Any]


# ---------- small IO helpers ----------


def load_json(path: str | os.PathLike[str], default: object = None) -> object:
    try:
        with pathlib.Path(path).open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError, json.JSONDecodeError:
        return default


def save_json(path: str | os.PathLike[str], obj: object) -> None:
    pathlib.Path(pathlib.Path(path).parent).mkdir(exist_ok=True, parents=True)
    with pathlib.Path(path).open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


def load_config() -> JsonDict:
    cfg = cast("JsonDict | None", load_json(CONFIG_PATH))
    if not cfg or cfg.get("client_id", "").startswith("YOUR_"):
        sys.exit(
            "Missing or unfilled scripts/strava_config.json.\n"
            "Copy scripts/strava_config.example.json to scripts/strava_config.json "
            "and fill in your Strava client_id and client_secret.\n"
            "See scripts/README.md."
        )
    return cfg


def load_club_patterns() -> dict[str, re.Pattern[str]]:
    rows = cast("list[JsonDict]", load_json(CLUB_PATTERNS_PATH, []) or [])
    return {
        str(row["id"]): re.compile(str(row["pattern"]), re.IGNORECASE)
        for row in rows
        if row.get("id") and row.get("pattern")
    }


# ---------- HTTP ----------


def http_post(url: str, data: dict[str, object]) -> JsonDict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req) as resp:
        return cast("JsonDict", json.load(resp))


def http_get(url: str, params: Mapping[str, object], token: str) -> object:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{qs}")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


# ---------- OAuth ----------


def auth(cfg: JsonDict) -> None:
    """One-time browser authorization.

    Spins up a localhost server to catch Strava's redirect, exchanges the code for tokens, and saves
    them.
    """
    port = cfg.get("redirect_port", 8721)
    redirect_uri = f"http://localhost:{port}/"
    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "approval_prompt": "auto",
        "scope": SCOPE,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            q = urllib.parse.urlparse(self.path).query
            captured.update(urllib.parse.parse_qs(q))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h2>Stride: Strava connected.</h2>"
                b"<p>You can close this tab and return to the terminal.</p>"
            )

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    print("\nOpen this URL in your browser and click Authorize:\n")
    print(f"  {url}\n")
    try:
        import webbrowser  # noqa: PLC0415

        webbrowser.open(url)
    except Exception:  # noqa: BLE001, S110
        pass

    print(f"Waiting for the Strava redirect on http://localhost:{port}/ ...")
    server = HTTPServer(("localhost", port), Handler)
    server.timeout = 300
    while "code" not in captured and "error" not in captured:
        server.handle_request()

    if "error" in captured:
        sys.exit(f"Authorization failed: {captured['error'][0]}")

    code = captured["code"][0]
    tok = http_post(
        TOKEN_URL,
        {
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code": code,
            "grant_type": "authorization_code",
        },
    )
    save_tokens(tok)
    print("Authorized. Tokens saved to data/private/strava-tokens.json")
    print("Now run:  uv run stride-sync sync")


def save_tokens(tok: JsonDict) -> None:
    save_json(
        TOKENS_PATH,
        {
            "access_token": tok["access_token"],
            "refresh_token": tok["refresh_token"],
            "expires_at": tok["expires_at"],
        },
    )


def valid_access_token(cfg: JsonDict) -> str:
    tokens = cast("JsonDict | None", load_json(TOKENS_PATH))
    if not tokens:
        sys.exit("Not authorized yet. Run:  uv run stride-sync auth")
    if time.time() > tokens["expires_at"] - 60:
        tok = http_post(
            TOKEN_URL,
            {
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
            },
        )
        save_tokens(tok)
        return tok["access_token"]
    return tokens["access_token"]


# ---------- fetch ----------


def fetch_activities(token: str, after_epoch: float | None = None) -> list[JsonDict]:
    """Fetch activities.

    Page through /athlete/activities. If after_epoch is given, only newer activities are returned.
    """
    out: list[JsonDict] = []
    page = 1
    while True:
        params = {"per_page": 200, "page": page}
        if after_epoch:
            params["after"] = int(after_epoch)
        for attempt in range(4):
            try:
                batch = cast("list[JsonDict]", http_get(ACTIVITIES_URL, params, token))
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:  # rate limited — back off
                    wait = 60 * (attempt + 1)
                    print(f"  rate limited, waiting {wait}s ...")
                    time.sleep(wait)
                else:
                    raise
        else:
            sys.exit("Repeated rate limiting from Strava; try again later.")
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 200:
            break
        page += 1
    return out


# Keep only the fields we need from each summary activity — smaller raw file.
KEEP = (
    "id",
    "name",
    "sport_type",
    "type",
    "start_date",
    "start_date_local",
    "distance",
    "moving_time",
    "elapsed_time",
    "total_elevation_gain",
    "average_speed",
    "max_speed",
    "average_heartrate",
    "max_heartrate",
    "average_cadence",
    "average_watts",
    "weighted_average_watts",
    "suffer_score",
    "achievement_count",
    "gear_id",
)


def slim(a: JsonDict) -> JsonDict:
    return {k: a.get(k) for k in KEEP if a.get(k) is not None}


def sync(cfg: JsonDict, *, full: bool = False) -> None:
    token = valid_access_token(cfg)
    existing = {a["id"]: a for a in cast("list[JsonDict]", load_json(RAW_PATH, []) or [])}

    after = None
    if existing and not full:
        newest = max(parse_dt(a["start_date"]) for a in existing.values())
        after = newest.timestamp()
        print(
            f"Incremental sync: fetching activities after {newest.date()} "
            f"({len(existing)} already stored)."
        )
    else:
        print("Full sync: fetching entire activity history (first run).")

    fetched = fetch_activities(token, after_epoch=after)
    for a in fetched:
        existing[a["id"]] = slim(a)

    activities = sorted(existing.values(), key=operator.itemgetter("start_date"))
    save_json(RAW_PATH, activities)
    prune_club_overrides(activities)

    summary = compute_summary(activities)
    save_json(SUMMARY_PATH, summary)

    print(f"Fetched {len(fetched)} new; {len(activities)} total stored.")
    print(f"Wrote {os.path.relpath(RAW_PATH, ROOT)} and {os.path.relpath(SUMMARY_PATH, ROOT)}.")
    rf = summary.get("running_fitness", {})
    if rf.get("best_equiv_5k"):
        print(
            f"Best recent equivalent 5K: {rf['best_equiv_5k']} "
            f"(from {rf.get('best_from', {}).get('name', '?')})."
        )


def prune_club_overrides(activities: list[JsonDict]) -> None:
    overrides = cast("list[JsonDict]", load_json(CLUB_OVERRIDES_PATH, []) or [])
    if not overrides:
        return

    club_patterns = load_club_patterns()
    activities_by_id = {int(a["id"]): a for a in activities}
    kept = []
    removed = []
    for row in overrides:
        activity_id = int(row.get("activity_id") or 0)
        club_id = str(row.get("club") or "")
        activity = activities_by_id.get(activity_id)
        pattern = club_patterns.get(club_id)
        if activity and pattern and pattern.search(activity.get("name") or ""):
            removed.append(row)
        else:
            kept.append(row)

    if len(kept) == len(overrides):
        return

    save_json(CLUB_OVERRIDES_PATH, kept)
    removed_ids = ", ".join(str(row.get("activity_id")) for row in removed)
    print(
        f"Removed {len(removed)} redundant club override(s) from "
        f"{os.path.relpath(CLUB_OVERRIDES_PATH, ROOT)}: {removed_ids}."
    )


# ---------- derive fitness summary ----------


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def sport_of(a: JsonDict) -> str:
    return a.get("sport_type") or a.get("type") or "Unknown"


def fmt_mmss(sec: float | None) -> str | None:
    if sec is None:
        return None
    sec = round(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def riegel_equiv_5k(
    dist_km: float | None,
    time_sec: float | None,
    target_km: float = 5.0,
) -> float | None:
    """Predicted equivalent time over target_km, given an effort over dist_km."""
    if not dist_km or not time_sec:
        return None
    return time_sec * (target_km / dist_km) ** 1.06


def compute_summary(acts: list[JsonDict]) -> JsonDict:
    now = datetime.now(UTC)

    # ---- totals by sport ----
    by_sport: dict[str, JsonDict] = {}
    for a in acts:
        s = by_sport.setdefault(
            sport_of(a),
            {"count": 0, "distance_km": 0.0, "moving_hours": 0.0, "elevation_m": 0.0},
        )
        s["count"] += 1
        s["distance_km"] += (a.get("distance") or 0) / 1000
        s["moving_hours"] += (a.get("moving_time") or 0) / 3600
        s["elevation_m"] += a.get("total_elevation_gain") or 0
    for s in by_sport.values():
        s["distance_km"] = round(s["distance_km"], 1)
        s["moving_hours"] = round(s["moving_hours"], 1)
        s["elevation_m"] = round(s["elevation_m"])

    # ---- last 12 weeks volume (all sports hours + running km) ----
    weekly: dict[str, JsonDict] = {}
    for a in acts:
        dt = parse_dt(a["start_date"])
        if dt < now - timedelta(weeks=12):
            continue
        wk = (dt - timedelta(days=dt.weekday())).date().isoformat()
        w = weekly.setdefault(wk, {"all_moving_hours": 0.0, "run_km": 0.0})
        w["all_moving_hours"] += (a.get("moving_time") or 0) / 3600
        if sport_of(a) == "Run":
            w["run_km"] += (a.get("distance") or 0) / 1000
    last_12_weeks = [
        {
            "week_start": k,
            "all_moving_hours": round(v["all_moving_hours"], 1),
            "run_km": round(v["run_km"], 1),
        }
        for k, v in sorted(weekly.items())
    ]

    # ---- running fitness: Riegel-equivalent 5K from recent runs ----
    recent_runs = [
        a
        for a in acts
        if sport_of(a) == "Run"
        and parse_dt(a["start_date"]) > now - timedelta(days=60)
        and (a.get("distance") or 0) >= 2000
        and (a.get("moving_time") or 0) > 0
    ]
    equivs: list[tuple[float, JsonDict]] = []
    for a in recent_runs:
        eq = riegel_equiv_5k(a["distance"] / 1000, a["moving_time"])
        if eq is None:
            continue
        equivs.append((eq, a))
    equivs.sort(key=operator.itemgetter(0))

    running_fitness: JsonDict = {"recent_runs_considered": len(recent_runs)}
    if equivs:
        best_eq, best_a = equivs[0]
        median_eq = equivs[len(equivs) // 2][0]
        running_fitness.update(
            {
                "window_days": 60,
                "best_equiv_5k": fmt_mmss(best_eq),
                "best_equiv_5k_sec": round(best_eq),
                "median_equiv_5k": fmt_mmss(median_eq),
                "best_from": {
                    "name": best_a.get("name"),
                    "date": best_a["start_date"][:10],
                    "distance_km": round(best_a["distance"] / 1000, 2),
                    "time": fmt_mmss(best_a["moving_time"]),
                },
            }
        )

    # ---- running form trends by month: cadence + HR efficiency ----
    form: dict[str, dict[str, list[float]]] = {}
    for a in acts:
        if sport_of(a) != "Run":
            continue
        dt = parse_dt(a["start_date"])
        if dt < now - timedelta(days=365):
            continue
        month = dt.strftime("%Y-%m")
        f = form.setdefault(month, {"cad": [], "eff": []})
        cad = a.get("average_cadence")
        if cad:
            f["cad"].append(cad * 2)  # Strava reports one-leg RPM; ×2 = steps/min
        spd, hr = a.get("average_speed"), a.get("average_heartrate")
        if spd and hr:
            f["eff"].append(spd / hr * 1000)  # metres per heartbeat ×1000
    running_form = {
        m: {
            "avg_cadence_spm": round(sum(v["cad"]) / len(v["cad"]), 1) if v["cad"] else None,
            "efficiency_m_per_beat_x1000": round(sum(v["eff"]) / len(v["eff"]), 1)
            if v["eff"]
            else None,
            "runs": max(len(v["cad"]), len(v["eff"])),
        }
        for m, v in sorted(form.items())
    }

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "total_activities": len(acts),
        "date_range": {
            "first": acts[0]["start_date"][:10] if acts else None,
            "last": acts[-1]["start_date"][:10] if acts else None,
        },
        "by_sport": by_sport,
        "last_12_weeks": last_12_weeks,
        "running_fitness": running_fitness,
        "running_form_by_month": running_form,
    }


# ---------- per-run details: shoes + best-effort PRs ----------


class Tokens:
    """Keeps a fresh access token across a long backfill (tokens expire ~6h)."""

    def __init__(self, cfg: JsonDict) -> None:
        self.cfg = cfg
        t = cast("JsonDict | None", load_json(TOKENS_PATH))
        if not t:
            sys.exit("Not authorized yet. Run:  uv run stride-sync auth")
        self.access = t["access_token"]
        self.expires_at = t["expires_at"]
        if time.time() > self.expires_at - 60:
            self.refresh()

    def refresh(self) -> None:
        t = cast("JsonDict", load_json(TOKENS_PATH))
        new = http_post(
            TOKEN_URL,
            {
                "client_id": self.cfg["client_id"],
                "client_secret": self.cfg["client_secret"],
                "grant_type": "refresh_token",
                "refresh_token": t["refresh_token"],
            },
        )
        save_tokens(new)
        self.access = new["access_token"]
        self.expires_at = new["expires_at"]


def api_get(tm: Tokens, url: str, params: dict[str, object] | None = None) -> object:
    """GET with auto token-refresh on 401 and back-off on 429.

    Strava's short limit is 100 requests per 15-minute window that resets on the
    quarter hour, so on 429 we sleep to just past the next boundary and retry.
    Up to ~24 windows (~6h) — comfortable for a long background backfill.
    """
    for _attempt in range(24):
        if time.time() > tm.expires_at - 60:
            tm.refresh()
        try:
            return http_get(url, params or {}, tm.access)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                tm.refresh()
            elif e.code == 429:
                now = time.time()
                wait = (900 - (now % 900)) + 5
                print(f"  rate limited — sleeping {int(wait)}s for the window to reset ...")
                time.sleep(wait)
            else:
                raise
    msg = f"Repeated failures fetching {url}"
    raise RuntimeError(msg)


def photo_url(detail: JsonDict) -> str | None:
    urls = ((detail.get("photos") or {}).get("primary") or {}).get("urls") or {}
    return urls.get("600") or urls.get("100") or None


def best_window(
    times: list[float] | None, dists: list[float] | None, target_m: int
) -> float | None:
    """Fastest contiguous stretch covering target_m, via two pointers."""
    if not times or not dists:
        return None
    n = len(dists)
    j = 0
    best = None
    for i in range(n):
        j = max(j, i)
        while j < n and dists[j] - dists[i] < target_m:
            j += 1
        if j >= n:
            break
        span = times[j] - times[i]
        best = span if best is None else min(best, span)
    return best


def details(cfg: JsonDict, limit: int | None = None) -> None:
    tm = Tokens(cfg)
    activities = cast("list[JsonDict]", load_json(RAW_PATH, []) or [])
    if not activities:
        sys.exit("No activities yet. Run:  uv run stride-sync sync")
    runs = [
        a
        for a in activities
        if (a.get("sport_type") or a.get("type")) in RUN_TYPES and a.get("distance")
    ]
    cache = cast("dict[str, JsonDict]", load_json(DETAILS_PATH, {}) or {})

    todo = [a for a in runs if str(a["id"]) not in cache]
    batch = todo[:limit] if limit else todo
    print(
        f"Runs: {len(runs)} total, {len(cache)} already detailed, "
        f"{len(todo)} remaining. Fetching {len(batch)} now"
        + (" (rate limits may pause this) ..." if batch else ".")
    )

    for i, a in enumerate(batch):
        d = cast("JsonDict", api_get(tm, f"{ACTIVITY_URL}/{a['id']}"))
        entry = {
            "gear_id": d.get("gear_id"),
            "photo": photo_url(d),
            "best_efforts": [
                {
                    "name": be["name"],
                    "distance": be["distance"],
                    "elapsed_time": be["elapsed_time"],
                }
                for be in (d.get("best_efforts") or [])
            ],
        }
        if (a.get("distance") or 0) >= min(STREAM_RECORDS.values()):
            try:
                st = cast(
                    "JsonDict",
                    api_get(
                        tm,
                        f"{ACTIVITY_URL}/{a['id']}/streams",
                        {"keys": "time,distance", "key_by_type": "true"},
                    ),
                )
                times = (st.get("time") or {}).get("data")
                dists = (st.get("distance") or {}).get("data")
                entry["windows"] = {
                    k: best_window(times, dists, m)
                    for k, m in STREAM_RECORDS.items()
                    if (a.get("distance") or 0) >= m
                }
            except Exception:  # noqa: BLE001
                entry["windows"] = {}
        cache[str(a["id"])] = entry
        if (i + 1) % 10 == 0:
            save_json(DETAILS_PATH, cache)
            print(f"  {i + 1}/{len(batch)} ...")

    save_json(DETAILS_PATH, cache)
    fetch_gear(tm, cache)
    recompute_records(activities, cache)

    remaining = len(todo) - len(batch)
    print(
        f"Done. {len(cache)} runs detailed"
        + (
            f", {remaining} still remaining — run `details` again to continue."
            if remaining
            else "."
        )
    )
    print(f"Wrote {os.path.relpath(RECORDS_PATH, ROOT)} and {os.path.relpath(GEAR_PATH, ROOT)}.")


def fetch_gear(tm: Tokens, cache: dict[str, JsonDict]) -> None:
    gear = cast("dict[str, JsonDict]", load_json(GEAR_PATH, {}) or {})
    ids = {str(e.get("gear_id")) for e in cache.values() if e.get("gear_id")}
    for gid in ids:
        if gid in gear:
            continue
        try:
            g = cast("JsonDict", api_get(tm, f"{GEAR_URL}/{gid}"))
        except Exception:  # noqa: BLE001, S112
            continue
        gear[gid] = {
            "name": g.get("name") or g.get("nickname") or gid,
            "distance_km": round((g.get("distance") or 0) / 1000, 1),
            "retired": bool(g.get("retired")),
        }
    save_json(GEAR_PATH, gear)


def fmt_hms(sec: float | None) -> str | None:
    if sec is None:
        return None
    sec = round(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def recompute_records(activities: list[JsonDict], cache: dict[str, JsonDict]) -> None:
    by_id = {str(a["id"]): a for a in activities}
    records: dict[str, JsonDict] = {}

    def consider(key: str, sec: float | None, aid: str | int) -> None:
        if sec is None:
            return
        if key not in records or sec < records[key]["sec"]:
            a = by_id.get(str(aid), {})
            records[key] = {
                "sec": round(sec),
                "time": fmt_hms(sec),
                "activity_id": int(aid),
                "name": a.get("name"),
                "date": (a.get("start_date") or "")[:10],
                "url": f"https://www.strava.com/activities/{aid}",
            }

    for aid, entry in cache.items():
        for be in entry.get("best_efforts", []):
            k = BEST_EFFORT_KEYS.get(be["name"])
            if k:
                consider(k, be["elapsed_time"], aid)
        for k, sec in (entry.get("windows") or {}).items():
            consider(k, sec, aid)

    save_json(RECORDS_PATH, records)
    order = ["400m", "1k", "5k", "10k", "half", "30k", "marathon"]
    have = [f"{k} {records[k]['time']}" for k in order if k in records]
    if have:
        print("Records: " + " · ".join(have))


# ---------- entry ----------


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "auth":
        auth(load_config())
    elif cmd == "sync":
        sync(load_config(), full="--full" in args)
    elif cmd == "details":
        limit = None
        for a in args[1:]:
            if a.startswith("--limit="):
                limit = int(a.split("=", 1)[1])
        details(load_config(), limit=limit)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
