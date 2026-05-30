#!/usr/bin/env python3
"""Stride — Strava sync.

Pulls your Strava activity history (all sports) and writes, under data/:
  strava-activities.json  raw activity list, deduped by id (gitignored)
  fitness-summary.json    compact derived metrics — the small file Claude reads
  strava-tokens.json      OAuth tokens (gitignored)

The raw history is fetched in full once; subsequent syncs only pull activities
newer than the latest one already stored, then merge.

Also writes (after `details`):
  strava-run-details.json  per-run cache: shoes (gear_id), photos, best efforts
  strava-gear.json         gear_id -> shoe name + mileage
  records.json             best efforts: 400m, 1k, 5k, 10k, half, 30k, marathon

Usage:
  python3 scripts/sync_strava.py auth            one-time browser authorization
  python3 scripts/sync_strava.py sync            fetch new activities + recompute summary
  python3 scripts/sync_strava.py sync --full     ignore checkpoint, refetch everything
  python3 scripts/sync_strava.py details         backfill per-run shoes + PRs (resumable)
  python3 scripts/sync_strava.py details --limit=100   do it in chunks (rate limits)

No third-party dependencies — Python 3 standard library only.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
CONFIG_PATH = os.path.join(ROOT, "scripts", "strava_config.json")
TOKENS_PATH = os.path.join(DATA_DIR, "strava-tokens.json")
RAW_PATH = os.path.join(DATA_DIR, "strava-activities.json")
SUMMARY_PATH = os.path.join(DATA_DIR, "fitness-summary.json")
DETAILS_PATH = os.path.join(DATA_DIR, "strava-run-details.json")  # cache: id -> detail
GEAR_PATH = os.path.join(DATA_DIR, "strava-gear.json")            # gear_id -> shoe
RECORDS_PATH = os.path.join(DATA_DIR, "records.json")            # best efforts

AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
ACTIVITY_URL = "https://www.strava.com/api/v3/activities"
GEAR_URL = "https://www.strava.com/api/v3/gear"
SCOPE = "read,activity:read_all"

RUN_TYPES = ("Run", "TrailRun")
# Strava best_effort name -> our record key. Strava computes these per run.
BEST_EFFORT_KEYS = {"400m": "400m", "1k": "1k", "5k": "5k", "10k": "10k",
                    "Half-Marathon": "half"}
# Distances Strava does NOT compute — we find the fastest window from streams.
STREAM_RECORDS = {"30k": 30000, "marathon": 42195}


# ---------- small IO helpers ----------

def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def load_config():
    cfg = load_json(CONFIG_PATH)
    if not cfg or cfg.get("client_id", "").startswith("YOUR_"):
        sys.exit(
            "Missing or unfilled scripts/strava_config.json.\n"
            "Copy scripts/strava_config.example.json to scripts/strava_config.json "
            "and fill in your Strava client_id and client_secret.\n"
            "See scripts/README.md."
        )
    return cfg


# ---------- HTTP ----------

def http_post(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def http_get(url, params, token):
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{qs}")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


# ---------- OAuth ----------

def auth(cfg):
    """One-time browser authorization. Spins up a localhost server to catch
    Strava's redirect, exchanges the code for tokens, and saves them."""
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
        def do_GET(self):
            q = urllib.parse.urlparse(self.path).query
            captured.update(urllib.parse.parse_qs(q))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Stride: Strava connected.</h2>"
                             b"<p>You can close this tab and return to the terminal.</p>")

        def log_message(self, *args):
            pass

    print("\nOpen this URL in your browser and click Authorize:\n")
    print(f"  {url}\n")
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass

    print(f"Waiting for the Strava redirect on http://localhost:{port}/ ...")
    server = HTTPServer(("localhost", port), Handler)
    server.timeout = 300
    while "code" not in captured and "error" not in captured:
        server.handle_request()

    if "error" in captured:
        sys.exit(f"Authorization failed: {captured['error'][0]}")

    code = captured["code"][0]
    tok = http_post(TOKEN_URL, {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "code": code,
        "grant_type": "authorization_code",
    })
    save_tokens(tok)
    print("Authorized. Tokens saved to data/strava-tokens.json")
    print("Now run:  python3 scripts/sync_strava.py sync")


def save_tokens(tok):
    save_json(TOKENS_PATH, {
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expires_at": tok["expires_at"],
    })


def valid_access_token(cfg):
    tokens = load_json(TOKENS_PATH)
    if not tokens:
        sys.exit("Not authorized yet. Run:  python3 scripts/sync_strava.py auth")
    if time.time() > tokens["expires_at"] - 60:
        tok = http_post(TOKEN_URL, {
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        })
        save_tokens(tok)
        return tok["access_token"]
    return tokens["access_token"]


# ---------- fetch ----------

def fetch_activities(token, after_epoch=None):
    """Page through /athlete/activities. If after_epoch is given, only newer
    activities are returned."""
    out = []
    page = 1
    while True:
        params = {"per_page": 200, "page": page}
        if after_epoch:
            params["after"] = int(after_epoch)
        for attempt in range(4):
            try:
                batch = http_get(ACTIVITIES_URL, params, token)
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
    "id", "name", "sport_type", "type", "start_date", "start_date_local",
    "distance", "moving_time", "elapsed_time", "total_elevation_gain",
    "average_speed", "max_speed", "average_heartrate", "max_heartrate",
    "average_cadence", "average_watts", "weighted_average_watts",
    "suffer_score", "achievement_count", "gear_id",
)


def slim(a):
    return {k: a.get(k) for k in KEEP if a.get(k) is not None}


def sync(cfg, full=False):
    token = valid_access_token(cfg)
    existing = {a["id"]: a for a in (load_json(RAW_PATH, []) or [])}

    after = None
    if existing and not full:
        newest = max(parse_dt(a["start_date"]) for a in existing.values())
        after = newest.timestamp()
        print(f"Incremental sync: fetching activities after {newest.date()} "
              f"({len(existing)} already stored).")
    else:
        print("Full sync: fetching entire activity history (first run).")

    fetched = fetch_activities(token, after_epoch=after)
    for a in fetched:
        existing[a["id"]] = slim(a)

    activities = sorted(existing.values(), key=lambda a: a["start_date"])
    save_json(RAW_PATH, activities)

    summary = compute_summary(activities)
    save_json(SUMMARY_PATH, summary)

    print(f"Fetched {len(fetched)} new; {len(activities)} total stored.")
    print(f"Wrote {os.path.relpath(RAW_PATH, ROOT)} and "
          f"{os.path.relpath(SUMMARY_PATH, ROOT)}.")
    rf = summary.get("running_fitness", {})
    if rf.get("best_equiv_5k"):
        print(f"Best recent equivalent 5K: {rf['best_equiv_5k']} "
              f"(from {rf.get('best_from', {}).get('name', '?')}).")


# ---------- derive fitness summary ----------

def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def sport_of(a):
    return a.get("sport_type") or a.get("type") or "Unknown"


def fmt_mmss(sec):
    if sec is None:
        return None
    sec = round(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def riegel_equiv_5k(dist_km, time_sec, target_km=5.0):
    """Predicted equivalent time over target_km, given an effort over dist_km."""
    if not dist_km or not time_sec:
        return None
    return time_sec * (target_km / dist_km) ** 1.06


def compute_summary(acts):
    now = datetime.now(timezone.utc)

    # ---- totals by sport ----
    by_sport = {}
    for a in acts:
        s = by_sport.setdefault(sport_of(a), {
            "count": 0, "distance_km": 0.0, "moving_hours": 0.0, "elevation_m": 0.0})
        s["count"] += 1
        s["distance_km"] += (a.get("distance") or 0) / 1000
        s["moving_hours"] += (a.get("moving_time") or 0) / 3600
        s["elevation_m"] += a.get("total_elevation_gain") or 0
    for s in by_sport.values():
        s["distance_km"] = round(s["distance_km"], 1)
        s["moving_hours"] = round(s["moving_hours"], 1)
        s["elevation_m"] = round(s["elevation_m"])

    # ---- last 12 weeks volume (all sports hours + running km) ----
    weekly = {}
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
        {"week_start": k,
         "all_moving_hours": round(v["all_moving_hours"], 1),
         "run_km": round(v["run_km"], 1)}
        for k, v in sorted(weekly.items())
    ]

    # ---- running fitness: Riegel-equivalent 5K from recent runs ----
    recent_runs = [
        a for a in acts
        if sport_of(a) == "Run"
        and parse_dt(a["start_date"]) > now - timedelta(days=60)
        and (a.get("distance") or 0) >= 2000
        and (a.get("moving_time") or 0) > 0
    ]
    equivs = []
    for a in recent_runs:
        eq = riegel_equiv_5k(a["distance"] / 1000, a["moving_time"])
        equivs.append((eq, a))
    equivs.sort(key=lambda x: x[0])

    running_fitness = {"recent_runs_considered": len(recent_runs)}
    if equivs:
        best_eq, best_a = equivs[0]
        median_eq = equivs[len(equivs) // 2][0]
        running_fitness.update({
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
        })

    # ---- running form trends by month: cadence + HR efficiency ----
    form = {}
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
            "efficiency_m_per_beat_x1000": round(sum(v["eff"]) / len(v["eff"]), 1) if v["eff"] else None,
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
    def __init__(self, cfg):
        self.cfg = cfg
        t = load_json(TOKENS_PATH)
        if not t:
            sys.exit("Not authorized yet. Run:  python3 scripts/sync_strava.py auth")
        self.access = t["access_token"]
        self.expires_at = t["expires_at"]
        if time.time() > self.expires_at - 60:
            self.refresh()

    def refresh(self):
        t = load_json(TOKENS_PATH)
        new = http_post(TOKEN_URL, {
            "client_id": self.cfg["client_id"],
            "client_secret": self.cfg["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": t["refresh_token"],
        })
        save_tokens(new)
        self.access = new["access_token"]
        self.expires_at = new["expires_at"]


def api_get(tm, url, params=None):
    """GET with auto token-refresh on 401 and back-off on 429."""
    for attempt in range(5):
        if time.time() > tm.expires_at - 60:
            tm.refresh()
        try:
            return http_get(url, params or {}, tm.access)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                tm.refresh()
            elif e.code == 429:
                wait = 60 * (attempt + 1)
                print(f"  rate limited, waiting {wait}s ...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"Repeated failures fetching {url}")


def photo_url(detail):
    urls = ((detail.get("photos") or {}).get("primary") or {}).get("urls") or {}
    return urls.get("600") or urls.get("100") or None


def best_window(times, dists, target_m):
    """Fastest contiguous stretch covering target_m, via two pointers."""
    if not times or not dists:
        return None
    n = len(dists)
    j = 0
    best = None
    for i in range(n):
        if j < i:
            j = i
        while j < n and dists[j] - dists[i] < target_m:
            j += 1
        if j >= n:
            break
        span = times[j] - times[i]
        best = span if best is None else min(best, span)
    return best


def details(cfg, limit=None):
    tm = Tokens(cfg)
    activities = load_json(RAW_PATH, []) or []
    if not activities:
        sys.exit("No activities yet. Run:  python3 scripts/sync_strava.py sync")
    runs = [a for a in activities
            if (a.get("sport_type") or a.get("type")) in RUN_TYPES and a.get("distance")]
    cache = load_json(DETAILS_PATH, {}) or {}

    todo = [a for a in runs if str(a["id"]) not in cache]
    batch = todo[:limit] if limit else todo
    print(f"Runs: {len(runs)} total, {len(cache)} already detailed, "
          f"{len(todo)} remaining. Fetching {len(batch)} now"
          + (" (rate limits may pause this) ..." if batch else "."))

    for i, a in enumerate(batch):
        d = api_get(tm, f"{ACTIVITY_URL}/{a['id']}")
        entry = {
            "gear_id": d.get("gear_id"),
            "photo": photo_url(d),
            "best_efforts": [
                {"name": be["name"], "distance": be["distance"],
                 "elapsed_time": be["elapsed_time"]}
                for be in (d.get("best_efforts") or [])
            ],
        }
        if (a.get("distance") or 0) >= min(STREAM_RECORDS.values()):
            try:
                st = api_get(tm, f"{ACTIVITY_URL}/{a['id']}/streams",
                             {"keys": "time,distance", "key_by_type": "true"})
                times = (st.get("time") or {}).get("data")
                dists = (st.get("distance") or {}).get("data")
                entry["windows"] = {
                    k: best_window(times, dists, m)
                    for k, m in STREAM_RECORDS.items()
                    if (a.get("distance") or 0) >= m
                }
            except Exception:
                entry["windows"] = {}
        cache[str(a["id"])] = entry
        if (i + 1) % 25 == 0:
            save_json(DETAILS_PATH, cache)
            print(f"  {i + 1}/{len(batch)} ...")

    save_json(DETAILS_PATH, cache)
    fetch_gear(tm, cache)
    recompute_records(activities, cache)

    remaining = len(todo) - len(batch)
    print(f"Done. {len(cache)} runs detailed"
          + (f", {remaining} still remaining — run `details` again to continue."
             if remaining else "."))
    print(f"Wrote {os.path.relpath(RECORDS_PATH, ROOT)} and "
          f"{os.path.relpath(GEAR_PATH, ROOT)}.")


def fetch_gear(tm, cache):
    gear = load_json(GEAR_PATH, {}) or {}
    ids = {e.get("gear_id") for e in cache.values() if e.get("gear_id")}
    for gid in ids:
        if gid in gear:
            continue
        try:
            g = api_get(tm, f"{GEAR_URL}/{gid}")
        except Exception:
            continue
        gear[gid] = {
            "name": g.get("name") or g.get("nickname") or gid,
            "distance_km": round((g.get("distance") or 0) / 1000, 1),
            "retired": bool(g.get("retired")),
        }
    save_json(GEAR_PATH, gear)


def fmt_hms(sec):
    if sec is None:
        return None
    sec = round(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def recompute_records(activities, cache):
    by_id = {str(a["id"]): a for a in activities}
    records = {}

    def consider(key, sec, aid):
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

def main():
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
