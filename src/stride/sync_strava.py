"""Stride — Strava sync.

Pulls your Strava activity history (all sports) and writes under data/:
  imported/strava-activities.json  raw activity list, deduped by id
  generated/fitness-summary.json   compact derived metrics
  private/strava-config.json       API credentials, written by `auth` (gitignored)
  private/strava-tokens.json       OAuth tokens (gitignored)

The raw history is fetched in full once; subsequent syncs only pull activities
newer than the latest one already stored, then merge.

Also writes (after `details`):
  imported/strava-run-details.json  per-run cache: shoes (gear_id), photos, best efforts
  imported/strava-gear.json         gear_id -> shoe name + mileage
  generated/records.json            best efforts: 400m, 1k, 5k, 10k, half, 30k, marathon

Usage:
  uv run stride-sync auth            prompt for API credentials, then authorize in the browser
  uv run stride-sync sync            fetch new activities + recompute summary
  uv run stride-sync sync --full     ignore checkpoint, refetch everything
  uv run stride-sync details         backfill per-run shoes + PRs (resumable)
  uv run stride-sync streams         cache per-run streams for the durability model (resumable)
  uv run stride-sync details --limit=100   do it in chunks (rate limits)

No third-party dependencies — Python 3 standard library only.
"""

import json
import math
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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class EconomyBands:
    aerobic_efficiency: tuple[float, float]
    aerobic_power: tuple[float, float]
    anaerobic_floor: float


ROOT = pathlib.Path(
    pathlib.Path(pathlib.Path(pathlib.Path(__file__).resolve()).parent).parent
).parent
DATA_DIR = ROOT / "data"
IMPORTED_DIR = DATA_DIR / "imported"
GENERATED_DIR = DATA_DIR / "generated"
ENTERED_DIR = DATA_DIR / "entered"
PRIVATE_DIR = DATA_DIR / "private"
CONFIG_PATH = PRIVATE_DIR / "strava-config.json"
TOKENS_PATH = PRIVATE_DIR / "strava-tokens.json"
RAW_PATH = IMPORTED_DIR / "strava-activities.json"
SUMMARY_PATH = GENERATED_DIR / "fitness-summary.json"
DETAILS_PATH = IMPORTED_DIR / "strava-run-details.json"  # cache: id -> detail
GEAR_PATH = IMPORTED_DIR / "strava-gear.json"  # gear_id -> shoe
RECORDS_PATH = GENERATED_DIR / "records.json"  # best efforts
CLUB_OVERRIDES_PATH = ENTERED_DIR / "club-overrides.json"
CLUB_PATTERNS_PATH = ENTERED_DIR / "club-patterns.json"
TRAINING_CONFIG_PATH = ENTERED_DIR / "training-config.json"
SAMPLES_PATH = PRIVATE_DIR / "strava-durability-samples.json"  # cache: id -> segments
HR_ZONES_PATH = GENERATED_DIR / "hr-zones.json"  # computed max HR + zone bpm boundaries
ZONE_METRICS_PATH = GENERATED_DIR / "zone-metrics.json"  # per-run m/beat by HR zone

AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"  # ruff:ignore[hardcoded-password-string]
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
# HR zones and the economy-metric bands are fractions of max HR (personal, kept in
# data/entered/training-config.json); max HR itself is measured from the runs
# (rolling max-average, written to data/generated/hr-zones.json by `streams`).
DEFAULT_ZONE_FRACTIONS = {
    "z1_floor": 0.50,
    "z1_z2": 0.60,
    "z2_z3": 0.70,
    "z3_z4": 0.80,
    "z4_z5": 0.90,
}
# Fraction-of-max-HR bands the three economy metrics are measured in. Aerobic ones
# are [lo, hi] ranges; anaerobic is an open-ended floor (high Z4 and above).
DEFAULT_ECONOMY_BANDS = EconomyBands(
    aerobic_efficiency=(0.62, 0.73),
    aerobic_power=(0.75, 0.80),
    anaerobic_floor=0.85,
)
DEFAULT_MAX_HR = 190.0
MAX_HR_WINDOWS = (10, 30, 60)  # seconds; max-average HR over each, not single peak
# Zone economy (m/beat) is measured over sustained in-band blocks: a block must
# last at least ZONE_MIN_BLOCK_S, and its first ZONE_TRIM_S is dropped because HR
# lags the effort and would otherwise inflate the ratio.
ZONE_MIN_BLOCK_S = 60.0
ZONE_TRIM_S = 20.0


# ---------- small IO helpers ----------


def load_json(path: str | os.PathLike[str], default: object = None) -> object:
    try:
        with pathlib.Path(path).open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError, json.JSONDecodeError:
        return default


def load_zone_fractions() -> dict[str, float]:
    """Zone-divider fractions of max HR, from training-config.json."""
    cfg = cast("JsonDict | None", load_json(TRAINING_CONFIG_PATH)) or {}
    configured = cast("JsonDict", cfg.get("hr_zone_fractions") or {})
    return {
        name: float(configured.get(name, default))
        for name, default in DEFAULT_ZONE_FRACTIONS.items()
    }


def load_economy_bands() -> EconomyBands:
    """Fraction-of-max-HR bands for the economy metrics, from training-config.json."""
    cfg = cast("JsonDict | None", load_json(TRAINING_CONFIG_PATH)) or {}
    configured = cast("JsonDict", cfg.get("economy_bands") or {})
    efficiency = configured.get("aerobic_efficiency") or DEFAULT_ECONOMY_BANDS.aerobic_efficiency
    power = configured.get("aerobic_power") or DEFAULT_ECONOMY_BANDS.aerobic_power
    floor = configured.get("anaerobic_floor", DEFAULT_ECONOMY_BANDS.anaerobic_floor)
    return EconomyBands(
        aerobic_efficiency=(float(efficiency[0]), float(efficiency[1])),
        aerobic_power=(float(power[0]), float(power[1])),
        anaerobic_floor=float(floor),
    )


def compute_zone_bands(max_hr: float, fractions: dict[str, float]) -> JsonDict:
    """Zone dividers and the economy-metric bands (bpm) from max HR."""
    economy = load_economy_bands()
    bpm = lambda fraction: round(fraction * max_hr, 1)  # ruff:ignore[lambda-assignment]
    efficiency = economy.aerobic_efficiency
    power = economy.aerobic_power
    return {
        "dividers_bpm": {name: bpm(fraction) for name, fraction in fractions.items()},
        "aerobic_efficiency": {"min": bpm(efficiency[0]), "max": bpm(efficiency[1])},
        "aerobic_power": {"min": bpm(power[0]), "max": bpm(power[1])},
        "anaerobic_floor": bpm(economy.anaerobic_floor),
    }


def load_hr_zones() -> dict[str, dict[str, float]]:
    """Aerobic-metric bands (bpm) from the computed hr-zones.json, with fallbacks.

    Derived from measured max HR; until `streams` has written hr-zones.json they
    fall back to DEFAULT_MAX_HR x the configured economy-band fractions.
    """
    data = cast("JsonDict | None", load_json(HR_ZONES_PATH))
    bands = cast("JsonDict", (data or {}).get("zones_bpm") or {})
    if not bands.get("aerobic_efficiency"):
        bands = compute_zone_bands(DEFAULT_MAX_HR, load_zone_fractions())

    def band(key: str, default: tuple[float, float]) -> dict[str, float]:
        configured = cast("JsonDict", bands.get(key) or {})
        return {
            "min": float(configured.get("min", default[0] * DEFAULT_MAX_HR)),
            "max": float(configured.get("max", default[1] * DEFAULT_MAX_HR)),
        }

    return {
        "high_zone2": band("aerobic_efficiency", DEFAULT_ECONOMY_BANDS.aerobic_efficiency),
        "high_zone3": band("aerobic_power", DEFAULT_ECONOMY_BANDS.aerobic_power),
    }


HR_ZONES = load_hr_zones()
HIGH_ZONE2_HR_MIN = HR_ZONES["high_zone2"]["min"]
HIGH_ZONE2_HR_MAX = HR_ZONES["high_zone2"]["max"]
HIGH_ZONE3_HR_MIN = HR_ZONES["high_zone3"]["min"]
HIGH_ZONE3_HR_MAX = HR_ZONES["high_zone3"]["max"]


def save_json(path: str | os.PathLike[str], obj: object) -> None:
    pathlib.Path(pathlib.Path(path).parent).mkdir(exist_ok=True, parents=True)
    with pathlib.Path(path).open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


def configured(cfg: JsonDict | None) -> bool:
    if not cfg:
        return False
    return not str(cfg.get("client_id", "")).startswith("YOUR_")


def load_config() -> JsonDict:
    cfg = cast("JsonDict | None", load_json(CONFIG_PATH))
    if not configured(cfg):
        sys.exit("No Strava credentials yet. Run:  uv run stride-sync auth")
    return cast("JsonDict", cfg)


def prompt_config() -> JsonDict:
    """Interactively collect Strava API credentials and write them to CONFIG_PATH."""
    print(
        "Strava API credentials needed.\n"
        "Open (or create) an API application at https://www.strava.com/settings/api\n"
        "with Authorization Callback Domain set to exactly 'localhost', then paste:\n"
    )
    client_id = input("  Client ID: ").strip()
    client_secret = input("  Client Secret: ").strip()
    if not client_id or not client_secret:
        sys.exit("Client ID and Client Secret are both required.")
    port = input("  Redirect port [8721]: ").strip()
    cfg = {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_port": int(port) if port else 8721,
    }
    save_json(CONFIG_PATH, cfg)
    print("\nSaved credentials to data/private/strava-config.json (gitignored).\n")
    return cfg


def ensure_config() -> JsonDict:
    cfg = cast("JsonDict | None", load_json(CONFIG_PATH))
    return cast("JsonDict", cfg) if configured(cfg) else prompt_config()


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

        def log_message(self, format: str, *args: object) -> None:  # ruff:ignore[builtin-argument-shadowing]
            pass

    print("\nOpen this URL in your browser and click Authorize:\n")
    print(f"  {url}\n")
    try:
        import webbrowser  # ruff:ignore[import-outside-top-level]

        webbrowser.open(url)
    except Exception:  # ruff:ignore[blind-except, try-except-pass]
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


def grade_cost_factor(grade_pct: float) -> float:
    """Approximate flat-equivalent cost multiplier from grade percent."""
    grade = max(-0.3, min(0.3, grade_pct / 100.0))
    cost = (
        155.4 * grade**5 - 30.4 * grade**4 - 43.3 * grade**3 + 46.3 * grade**2 + 19.5 * grade + 3.6
    )
    return max(0.45, min(5.0, cost / 3.6))


def aerobic_metric_from_streams(st: JsonDict, hr_min: float, hr_max: float) -> float | None:
    times = (st.get("time") or {}).get("data") or []
    dists = (st.get("distance") or {}).get("data") or []
    hrs = (st.get("heartrate") or {}).get("data") or []
    grades = (st.get("grade_smooth") or {}).get("data") or []
    n = min(len(times), len(dists), len(hrs), len(grades))
    if n < 2:
        return None

    adjusted_m = 0.0
    beats = 0.0
    for i in range(1, n):
        hr = hrs[i]
        if not (hr_min <= hr <= hr_max):
            continue
        dt = max(0.0, float(times[i]) - float(times[i - 1]))
        dd = max(0.0, float(dists[i]) - float(dists[i - 1]))
        if dt <= 0 or dd <= 0:
            continue
        adjusted_m += dd * grade_cost_factor(float(grades[i]))
        beats += float(hr) * dt / 60.0

    return adjusted_m / beats if beats > 0 else None


def grade_adjusted_distance_km(st: JsonDict) -> float | None:
    dists = (st.get("distance") or {}).get("data") or []
    grades = (st.get("grade_smooth") or {}).get("data") or []
    n = min(len(dists), len(grades))
    if n < 2:
        return None

    adjusted_m = 0.0
    for i in range(1, n):
        dd = max(0.0, float(dists[i]) - float(dists[i - 1]))
        adjusted_m += dd * grade_cost_factor(float(grades[i]))
    return adjusted_m / 1000


def best_grade_adjusted_speed(st: JsonDict, seconds: float = 60.0) -> float | None:
    times = (st.get("time") or {}).get("data") or []
    dists = (st.get("distance") or {}).get("data") or []
    grades = (st.get("grade_smooth") or {}).get("data") or []
    n = min(len(times), len(dists), len(grades))
    if n < 2:
        return None

    adjusted = [0.0]
    for i in range(1, n):
        dd = max(0.0, float(dists[i]) - float(dists[i - 1]))
        adjusted.append(adjusted[-1] + dd * grade_cost_factor(float(grades[i])))

    best = None
    j = 0
    for i in range(n):
        j = max(j, i + 1)
        while j < n and float(times[j]) - float(times[i]) < seconds:
            j += 1
        if j >= n:
            break
        distance_m = adjusted[j] - adjusted[i]
        if distance_m <= 0:
            continue
        span = float(times[j]) - float(times[i])
        speed = distance_m / span
        best = speed if best is None else max(best, speed)
    return best


# ---------- durability streams ----------
# Per-run HR/grade/speed streams, segmented and cached for the durability model.
# `stride-durability-model` reads SAMPLES_PATH offline and does no network I/O.
WARMUP_SECONDS = 8 * 60
WARMUP_METRES = 1000.0
MIN_SPEED_MPS = 1.6
MAX_SPEED_MPS = 7.5
MIN_HR = 90.0
MAX_HR = 205.0
MIN_SEGMENT_DT = 0.5
MAX_SEGMENT_DT = 10.0


@dataclass(frozen=True)
class StreamActivity:
    id: int
    name: str
    date: str
    start_ts: float
    distance_m: float


@dataclass(frozen=True)
class Segment:
    run_id: int
    date: str
    dt: float
    speed: float
    adjusted_speed: float
    hr: float


def parse_ts(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def date_from_activity(activity: JsonDict) -> str:
    raw = str(activity.get("start_date_local") or activity["start_date"])
    return raw.split("T", 1)[0]


def load_stream_activities() -> list[StreamActivity]:
    activities = cast("list[JsonDict]", load_json(RAW_PATH, []) or [])
    runs = []
    for activity in activities:
        sport = activity.get("sport_type") or activity.get("type")
        distance = float(activity.get("distance") or 0)
        if sport not in RUN_TYPES or distance <= 0:
            continue
        runs.append(
            StreamActivity(
                id=int(activity["id"]),
                name=str(activity.get("name") or ""),
                date=date_from_activity(activity),
                start_ts=parse_ts(str(activity.get("start_date_local") or activity["start_date"])),
                distance_m=distance,
            )
        )
    return sorted(runs, key=operator.attrgetter("start_ts"))


def numeric_stream(streams: JsonDict, key: str) -> list[float]:
    values = (streams.get(key) or {}).get("data") or []
    out = []
    for value in values:
        if value is None:
            out.append(float("nan"))
            continue
        out.append(float(value))
    return out


def stream_segments(activity: StreamActivity, streams: JsonDict) -> list[Segment]:
    times = numeric_stream(streams, "time")
    dists = numeric_stream(streams, "distance")
    grades = numeric_stream(streams, "grade_smooth")
    hrs = numeric_stream(streams, "heartrate")
    n = min(len(times), len(dists), len(grades), len(hrs))
    segments: list[Segment] = []
    prev_speed = None

    for i in range(1, n):
        elapsed = times[i]
        dist = dists[i]
        segment_values = (times[i - 1], elapsed, dists[i - 1], dist, grades[i], hrs[i])
        if not all(math.isfinite(value) for value in segment_values):
            continue
        if elapsed < WARMUP_SECONDS or dist < WARMUP_METRES:
            continue
        dt = times[i] - times[i - 1]
        dd = dists[i] - dists[i - 1]
        if dt < MIN_SEGMENT_DT or dt > MAX_SEGMENT_DT or dd <= 0:
            continue
        speed = dd / dt
        hr = hrs[i]
        if not (MIN_SPEED_MPS <= speed <= MAX_SPEED_MPS and MIN_HR <= hr <= MAX_HR):
            continue
        if prev_speed is not None and abs(speed - prev_speed) > 3.0:
            prev_speed = speed
            continue
        prev_speed = speed
        adjusted_speed = speed * grade_cost_factor(grades[i])
        segments.append(
            Segment(
                run_id=activity.id,
                date=activity.date,
                dt=dt,
                speed=speed,
                adjusted_speed=adjusted_speed,
                hr=hr,
            )
        )
    return segments


def rolling_max_avg(series: list[float], window: int) -> float:
    """Max average over any contiguous `window`-sample run (not the single peak)."""
    if len(series) < window:
        return 0.0
    total = sum(series[:window])
    best = total
    for i in range(window, len(series)):
        total += series[i] - series[i - window]
        best = max(best, total)
    return best / window


def athlete_max_hr(cache: dict[str, JsonDict]) -> dict[int, float]:
    """Max-average HR over each window length, across all cached runs.

    Each segment's HR is expanded to ~1 Hz by its duration, then we take the best
    rolling average per window — robust to single-sample spikes.
    """
    best: dict[int, float] = dict.fromkeys(MAX_HR_WINDOWS, 0.0)
    for row in cache.values():
        series: list[float] = []
        for segment in cast("list[JsonDict]", row.get("segments") or []):
            series.extend([float(segment["hr"])] * max(1, round(float(segment["dt"]))))
        for window in MAX_HR_WINDOWS:
            best[window] = max(best[window], rolling_max_avg(series, window))
    return best


def write_hr_zones(cache: dict[str, JsonDict]) -> JsonDict:
    """Recompute max HR from the cache, write hr-zones.json, return the zone bands."""
    windows = athlete_max_hr(cache)
    measured = [value for value in windows.values() if value > 0]
    max_hr = max(measured) if measured else DEFAULT_MAX_HR
    fractions = load_zone_fractions()
    bands = compute_zone_bands(max_hr, fractions)
    save_json(
        HR_ZONES_PATH,
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "max_hr": round(max_hr, 1),
            "max_hr_by_window_s": {str(w): round(windows[w], 1) for w in MAX_HR_WINDOWS},
            "fractions": fractions,
            "zones_bpm": bands,
        },
    )
    print(f"Max HR {max_hr:.0f} bpm -> wrote {HR_ZONES_PATH.relative_to(ROOT)}")
    return bands


def band_m_per_beat(segments: list[JsonDict], lo: float, hi: float) -> float | None:
    """Grade-adjusted metres per heartbeat over sustained blocks in HR band [lo, hi].

    A block is contiguous segments with HR in band; only blocks lasting at least
    ZONE_MIN_BLOCK_S count, and the first ZONE_TRIM_S of each is dropped (HR lag).
    """
    blocks: list[list[JsonDict]] = []
    current: list[JsonDict] = []
    for segment in segments:
        if lo <= float(segment["hr"]) <= hi:
            current.append(segment)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    metres = 0.0
    beats = 0.0
    for block in blocks:
        if sum(float(s["dt"]) for s in block) < ZONE_MIN_BLOCK_S:
            continue
        skipped = 0.0
        for segment in block:
            dt = float(segment["dt"])
            if skipped < ZONE_TRIM_S:
                skipped += dt
                continue
            metres += float(segment["adjusted_speed"]) * dt
            beats += float(segment["hr"]) * dt / 60.0
    return metres / beats if beats > 0 else None


def write_zone_metrics(cache: dict[str, JsonDict], bands: JsonDict) -> None:
    """Per-run economy (m/beat) for each band, from the cached segments.

    All three are the same sustained-block measure, just different HR bands:
    aerobic efficiency and power in their [lo, hi] ranges, anaerobic from its floor up.
    """
    efficiency = cast("JsonDict", bands["aerobic_efficiency"])
    power = cast("JsonDict", bands["aerobic_power"])
    band_for = {
        "aerobic_efficiency": (float(efficiency["min"]), float(efficiency["max"])),
        "aerobic_power": (float(power["min"]), float(power["max"])),
        "anaerobic_power": (float(bands["anaerobic_floor"]), float("inf")),
    }
    out: JsonDict = {}
    for run_id, row in cache.items():
        segments = cast("list[JsonDict]", row.get("segments") or [])
        present = {
            name: round(value, 4)
            for name, (lo, hi) in band_for.items()
            if (value := band_m_per_beat(segments, lo, hi)) is not None
        }
        if present:
            out[run_id] = {"date": row["date"], **present}
    save_json(ZONE_METRICS_PATH, out)
    print(f"Zone economy: {len(out)} runs with a sustained in-band block")


def streams(cfg: JsonDict, limit: int | None = None) -> None:
    """Fetch per-run HR/grade/speed streams, cache segments, and recompute max HR.

    Resumable: only activities missing from the cache are fetched. The durability
    model reads this cache offline; it does no network I/O of its own. Each run
    refreshes hr-zones.json (max HR + zone boundaries) from the full cache.
    """
    activities = load_stream_activities()
    if not activities:
        sys.exit("No activities yet. Run:  uv run stride-sync sync")

    cache = cast("dict[str, JsonDict]", load_json(SAMPLES_PATH, {}) or {})
    todo = [activity for activity in activities if str(activity.id) not in cache]
    batch = todo[:limit] if limit else todo
    print(
        f"Durability streams: {len(activities)} runs, {len(cache)} cached, "
        f"{len(todo)} remaining. Fetching {len(batch)} now."
    )

    if batch:
        tm = Tokens(cfg)
        for i, activity in enumerate(batch):
            st = cast(
                "JsonDict",
                api_get(
                    tm,
                    f"{ACTIVITY_URL}/{activity.id}/streams",
                    {"keys": "time,distance,grade_smooth,heartrate", "key_by_type": "true"},
                ),
            )
            segments = stream_segments(activity, st)
            cache[str(activity.id)] = {
                "date": activity.date,
                "name": activity.name,
                "segments": [
                    {
                        "dt": round(segment.dt, 3),
                        "speed": round(segment.speed, 4),
                        "adjusted_speed": round(segment.adjusted_speed, 4),
                        "hr": round(segment.hr, 2),
                    }
                    for segment in segments
                ],
            }
            if (i + 1) % 10 == 0:
                save_json(SAMPLES_PATH, cache)
                print(f"  {i + 1}/{len(batch)} ...")
        save_json(SAMPLES_PATH, cache)

    bands = write_hr_zones(cache)
    write_zone_metrics(cache, bands)


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

    todo = [
        a
        for a in runs
        if str(a["id"]) not in cache
        or "aerobic_efficiency_m_per_beat" not in cache[str(a["id"])]
        or "aerobic_power_m_per_beat" not in cache[str(a["id"])]
        or "best_60s_grade_adjusted_speed_mps" not in cache[str(a["id"])]
        or "grade_adjusted_distance_km" not in cache[str(a["id"])]
    ]
    batch = todo[:limit] if limit else todo
    print(
        f"Runs: {len(runs)} total, {len(cache)} already detailed, "
        f"{len(todo)} remaining. Fetching {len(batch)} now"
        + (" (rate limits may pause this) ..." if batch else ".")
    )

    for i, a in enumerate(batch):
        previous = cache.get(str(a["id"]), {})
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
        try:
            st = cast(
                "JsonDict",
                api_get(
                    tm,
                    f"{ACTIVITY_URL}/{a['id']}/streams",
                    {
                        "keys": "time,distance,grade_smooth,heartrate",
                        "key_by_type": "true",
                    },
                ),
            )
        except Exception:  # ruff:ignore[blind-except]
            st = {}
            entry["aerobic_efficiency_m_per_beat"] = None
            entry["aerobic_power_m_per_beat"] = None
            entry["best_60s_grade_adjusted_speed_mps"] = None
            entry["grade_adjusted_distance_km"] = None
        else:
            efficiency = aerobic_metric_from_streams(st, HIGH_ZONE2_HR_MIN, HIGH_ZONE2_HR_MAX)
            power = aerobic_metric_from_streams(st, HIGH_ZONE3_HR_MIN, HIGH_ZONE3_HR_MAX)
            best_60s = best_grade_adjusted_speed(st)
            adjusted_distance = grade_adjusted_distance_km(st)
            entry["aerobic_efficiency_m_per_beat"] = (
                round(efficiency, 4) if efficiency is not None else None
            )
            entry["aerobic_power_m_per_beat"] = round(power, 4) if power is not None else None
            entry["best_60s_grade_adjusted_speed_mps"] = (
                round(best_60s, 4) if best_60s is not None else None
            )
            entry["grade_adjusted_distance_km"] = (
                round(adjusted_distance, 4) if adjusted_distance is not None else None
            )

        if st and (a.get("distance") or 0) >= min(STREAM_RECORDS.values()):
            try:
                times = (st.get("time") or {}).get("data")
                dists = (st.get("distance") or {}).get("data")
                entry["windows"] = {
                    k: best_window(times, dists, m)
                    for k, m in STREAM_RECORDS.items()
                    if (a.get("distance") or 0) >= m
                }
            except Exception:  # ruff:ignore[blind-except]
                entry["windows"] = {}
        elif previous.get("windows"):
            entry["windows"] = previous["windows"]
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
        except Exception:  # ruff:ignore[blind-except, try-except-continue]
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
        auth(ensure_config())
    elif cmd == "sync":
        sync(load_config(), full="--full" in args)
    elif cmd in {"details", "streams"}:
        limit = None
        for a in args[1:]:
            if a.startswith("--limit="):
                limit = int(a.split("=", 1)[1])
        command = details if cmd == "details" else streams
        command(load_config(), limit=limit)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
