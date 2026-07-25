"""Build Stride's supervised race prediction artifact.

Runtime stays static: this script reads Strava imports plus a manually editable
race registry and writes data/generated/race-model.json for the browser.
"""

from __future__ import annotations

import json
import math
import operator
import pathlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, SupportsFloat, cast

import numpy as np

from stride.sync_strava import load_hr_zones

ROOT = pathlib.Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
IMPORTED_DIR = DATA_DIR / "imported"
GENERATED_DIR = DATA_DIR / "generated"
ENTERED_DIR = DATA_DIR / "entered"
ACTIVITIES_PATH = IMPORTED_DIR / "strava-activities.json"
DETAILS_PATH = IMPORTED_DIR / "strava-run-details.json"
RACES_PATH = ENTERED_DIR / "races.json"
MARATHON_GOAL_PATH = ENTERED_DIR / "marathon-goal.json"
MODEL_PATH = GENERATED_DIR / "race-model.json"

RUN_TYPES = {"Run", "TrailRun"}
DAY_SEC = 86400
FITNESS_TAU = 42.0
FATIGUE_TAU = 7.0
RIEGEL_EXPONENT = 1.06
RIDGE_L2 = 8.0

# The model is fit on the five fitness metrics the Goals view tracks. Each is a
# causal, leave-one-out trailing aggregate of the runs before a race.
MODEL_FEATURES = [
    "aerobic_efficiency",
    "aerobic_power",
    "anaerobic_power",
    "load_tolerance",
    "durability",
]
# Detail-derived features use 0.0 as a "no stream data in window" sentinel; those
# are median-imputed before standardization so a data gap is not read as "weak".
IMPUTED_FEATURES = {"aerobic_efficiency", "aerobic_power", "anaerobic_power"}

LOAD_TOLERANCE_TAU = 9.0
DURABILITY_TAU = 28.0
DURABILITY_THRESHOLD_KM = 20.0

# Aerobic efficiency / power are grade-adjusted metres per heartbeat in a HR band.
# Strava streams here carry no per-run value, so — like the Goals view — fall back
# to whole-run metres per beat (60000 / (pace_sec * hr)) for runs averaging in band.
# Bands come from data/entered/training-config.json (shared with the sync step).
_HR_ZONES = load_hr_zones()
HIGH_ZONE2_HR = (_HR_ZONES["high_zone2"]["min"], _HR_ZONES["high_zone2"]["max"])
HIGH_ZONE3_HR = (_HR_ZONES["high_zone3"]["min"], _HR_ZONES["high_zone3"]["max"])

# Environmental normalization. Each race time is divided by a multiplicative
# "how much these conditions slowed you" factor so the fitness fit sees neutral
# (flat, road, ~10 C) times; predictions are neutral, and the goal race is
# re-adjusted to its own course/weather.
WARM_THRESHOLD_C = 15.0
HEAT_PENALTY_PER_C = 0.012
COLD_THRESHOLD_C = -5.0
COLD_PENALTY_PER_C = 0.004
ELEV_PENALTY_PER_M_PER_KM = 0.0033
TRAIL_SURFACE_PENALTY = 0.05
# Montreal daily-mean climatology (deg C) by month, used when a race carries no
# recorded temperature (Strava activities here have none).
MONTHLY_MEAN_TEMP_C = {
    1: -9.7,
    2: -7.7,
    3: -1.6,
    4: 6.3,
    5: 13.4,
    6: 18.6,
    7: 21.3,
    8: 20.0,
    9: 15.3,
    10: 8.5,
    11: 1.6,
    12: -5.9,
}


@dataclass
class MarathonGoal:
    pace_seconds: float
    race_date: str
    prediction_distances: dict[str, float]
    fitness_score_weights: dict[str, float]
    goal_conditions: dict[str, Any]


@dataclass(frozen=True)
class Run:
    id: int
    name: str
    ts: float
    date: str
    km: float
    moving_time: float
    pace_sec: float
    elevation_m: float
    grade_adjusted_km: float
    is_trail: bool
    hr: float | None
    aerobic_efficiency: float | None
    aerobic_power: float | None
    anaerobic_power: float | None
    load: float


@dataclass(frozen=True)
class RawRun:
    id: int
    name: str
    ts: float
    km: float
    moving_time: float
    pace_sec: float
    elevation_m: float
    grade_adjusted_km: float
    is_trail: bool
    hr: object
    aerobic_efficiency: object
    aerobic_power: object
    anaerobic_power: object


JsonDict = dict[str, Any]


def load_json(path: pathlib.Path, default: object) -> object:
    try:
        with pathlib.Path(path).open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError, json.JSONDecodeError:
        return default


def load_marathon_goal() -> MarathonGoal:
    goal = cast("JsonDict", load_json(MARATHON_GOAL_PATH, {}))
    pace_seconds = float(goal["marathon_pace_sec"])
    race_date = str(goal["race_date"])
    prediction_distances = {
        str(key): float(value)
        for key, value in cast("dict[str, SupportsFloat]", goal["prediction_distances"]).items()
    }
    fitness_score_weights = {
        str(key): float(value)
        for key, value in cast("dict[str, SupportsFloat]", goal["fitness_score_weights"]).items()
    }
    goal_conditions = cast("JsonDict", goal.get("goal_conditions", {}))
    return MarathonGoal(
        pace_seconds, race_date, prediction_distances, fitness_score_weights, goal_conditions
    )


MARATHON_GOAL = load_marathon_goal()


def save_json(path: pathlib.Path, obj: object) -> None:
    pathlib.Path(pathlib.Path(path).parent).mkdir(exist_ok=True, parents=True)
    with pathlib.Path(path).open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")


def parse_ts(value: str) -> float:
    text = value.replace("Z", "+00:00")
    return datetime.fromisoformat(text).timestamp()


def date_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).date().isoformat()


def optional_float(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float | str):
        msg = f"Expected a numeric value, got {type(value).__name__}"
        raise TypeError(msg)
    return float(value)


def normalize_runs(activities: list[JsonDict], details: dict[str, JsonDict]) -> list[Run]:
    raw: list[RawRun] = []
    for activity in activities:
        sport = activity.get("sport_type") or activity.get("type")
        distance = float(activity.get("distance") or 0)
        moving_time = float(activity.get("moving_time") or 0)
        if sport not in RUN_TYPES or distance <= 0 or moving_time <= 0:
            continue
        km = distance / 1000.0
        detail = details.get(str(activity["id"]), {})
        grade_adjusted_km = optional_float(detail.get("grade_adjusted_distance_km")) or km
        raw.append(
            RawRun(
                id=int(activity["id"]),
                name=str(activity.get("name") or ""),
                ts=parse_ts(str(activity.get("start_date_local") or activity["start_date"])),
                km=km,
                moving_time=moving_time,
                pace_sec=moving_time / km,
                elevation_m=float(activity.get("total_elevation_gain") or 0),
                grade_adjusted_km=grade_adjusted_km,
                is_trail=sport == "TrailRun",
                hr=activity.get("average_heartrate"),
                aerobic_efficiency=detail.get("aerobic_efficiency_m_per_beat"),
                aerobic_power=detail.get("aerobic_power_m_per_beat"),
                anaerobic_power=detail.get("best_60s_grade_adjusted_speed_mps"),
            )
        )
    raw.sort(key=operator.attrgetter("ts"))
    paces = [run.pace_sec for run in raw if run.pace_sec > 0]
    baseline = float(np.median(paces)) if paces else 0.0

    runs: list[Run] = []
    for run in raw:
        pace_boost = 0.0
        if baseline:
            pace_boost = clamp((baseline - run.pace_sec) / baseline, -0.12, 0.22)
        load = (run.km + run.elevation_m / 100.0) * (1.0 + pace_boost)
        runs.append(
            Run(
                id=run.id,
                name=run.name,
                ts=run.ts,
                date=date_from_ts(run.ts),
                km=run.km,
                moving_time=run.moving_time,
                pace_sec=run.pace_sec,
                elevation_m=run.elevation_m,
                grade_adjusted_km=run.grade_adjusted_km,
                is_trail=run.is_trail,
                hr=optional_float(run.hr),
                aerobic_efficiency=optional_float(run.aerobic_efficiency),
                aerobic_power=optional_float(run.aerobic_power),
                anaerobic_power=optional_float(run.anaerobic_power),
                load=load,
            )
        )
    return runs


def state_at(runs: list[Run], ts: float, exclude_id: int | None = None) -> dict[str, float]:
    included = [run for run in runs if run.ts < ts and run.id != exclude_id]
    if not included:
        return {"fitness": 0.0, "fatigue": 0.0, "form": 0.0}

    fitness = 0.0
    fatigue = 0.0
    last_ts = included[0].ts
    for run in included:
        dt_days = max(0.0, (run.ts - last_ts) / DAY_SEC)
        fitness *= math.exp(-dt_days / FITNESS_TAU)
        fatigue *= math.exp(-dt_days / FATIGUE_TAU)
        fitness += run.load / FITNESS_TAU
        fatigue += run.load / FATIGUE_TAU
        last_ts = run.ts

    dt_days = max(0.0, (ts - last_ts) / DAY_SEC)
    fitness *= math.exp(-dt_days / FITNESS_TAU)
    fatigue *= math.exp(-dt_days / FATIGUE_TAU)
    fitness_weekly = fitness * 7.0
    fatigue_weekly = fatigue * 7.0
    return {
        "fitness": fitness_weekly,
        "fatigue": fatigue_weekly,
        "form": fitness_weekly - fatigue_weekly,
    }


def ewma_rate_before(
    runs: list[Run],
    ts: float,
    tau_days: float,
    contribution: Callable[[Run], float],
    exclude_id: int | None = None,
) -> float:
    value = 0.0
    last_ts = None
    for run in runs:
        if run.ts >= ts:
            break
        if run.id == exclude_id:
            continue
        if last_ts is None:
            last_ts = run.ts
        dt_days = max(0.0, (run.ts - last_ts) / DAY_SEC)
        value *= math.exp(-dt_days / tau_days)
        value += contribution(run) / tau_days
        last_ts = run.ts
    if last_ts is not None:
        value *= math.exp(-max(0.0, (ts - last_ts) / DAY_SEC) / tau_days)
    return value * 7.0


def recent_runs(runs: list[Run], ts: float, days: int, exclude_id: int | None = None) -> list[Run]:
    start = ts - days * DAY_SEC
    return [run for run in runs if start <= run.ts < ts and run.id != exclude_id]


def aerobic_samples(
    runs: list[Run],
    hr_band: tuple[float, float],
    stream_value: Callable[[Run], float | None],
) -> list[float]:
    lo, hi = hr_band
    samples: list[float] = []
    for run in runs:
        value = stream_value(run)
        if value is not None:
            samples.append(value)
        elif run.hr is not None and lo <= run.hr <= hi and run.pace_sec > 0:
            samples.append(60000.0 / (run.pace_sec * run.hr))
    return samples


def factors_at(runs: list[Run], ts: float, exclude_id: int | None = None) -> dict[str, float]:
    state = state_at(runs, ts, exclude_id)
    r28 = recent_runs(runs, ts, 28, exclude_id)
    r56 = recent_runs(runs, ts, 56, exclude_id)
    r75 = recent_runs(runs, ts, 75, exclude_id)
    aerobic_efficiency = aerobic_samples(
        r75, HIGH_ZONE2_HR, operator.attrgetter("aerobic_efficiency")
    )
    aerobic_power = aerobic_samples(r75, HIGH_ZONE3_HR, operator.attrgetter("aerobic_power"))
    anaerobic_power = [run.anaerobic_power for run in r75 if run.anaerobic_power is not None]

    volume_28 = sum(run.km for run in r28) / 4.0
    consistency = len({date_from_ts(run.ts) for run in r56}) / 8.0
    longest_56 = max((run.km for run in r56), default=0.0)
    mp_specificity = ewma_rate_before(
        runs,
        ts,
        21.0,
        lambda run: (
            run.km
            * math.exp(-0.5 * ((run.pace_sec - MARATHON_GOAL.pace_seconds) / 12.0) ** 2)
            * clamp((run.km - 12.0) / 4.0, 0.0, 1.0)
        ),
        exclude_id,
    )
    long_run_durability = ewma_rate_before(
        runs,
        ts,
        DURABILITY_TAU,
        lambda run: max(0.0, run.km - DURABILITY_THRESHOLD_KM),
        exclude_id,
    )
    # "Load tolerance" mirrors the Goals view: grade-adjusted weekly volume.
    load_tolerance = ewma_rate_before(
        runs,
        ts,
        LOAD_TOLERANCE_TAU,
        operator.attrgetter("grade_adjusted_km"),
        exclude_id,
    )

    factors = {
        **state,
        "volume_28d_km_per_week": volume_28,
        "volume_56d_km_per_week": sum(run.km for run in r56) / 8.0,
        "longest_run_56d_km": longest_56,
        "long_run_durability": long_run_durability,
        "mp_specificity": mp_specificity,
        "aerobic_efficiency": float(np.median(aerobic_efficiency)) if aerobic_efficiency else 0.0,
        "aerobic_power": float(np.median(aerobic_power)) if aerobic_power else 0.0,
        "anaerobic_power": max(anaerobic_power) if anaerobic_power else 0.0,
        "consistency_runs_per_week": consistency,
        # The five supervised features (durability compressed with log1p so a few
        # very long runs do not dominate).
        "load_tolerance": load_tolerance,
        "durability": math.log1p(long_run_durability),
    }
    factors["fitness_score"] = fitness_score(factors)
    return factors


def fitness_score(factors: dict[str, float]) -> float:
    """Single low-variance score used by the first supervised fit.

    The component factors are still exported for inspection. With only a few
    race labels, learning every component independently is unstable, so this
    first version learns how strongly this aggregate score moves race times.
    """
    weights = MARATHON_GOAL.fitness_score_weights
    return (
        weights["fitness"] * factors["fitness"]
        + weights["form"] * factors["form"]
        + weights["volume_28d_km_per_week"] * factors["volume_28d_km_per_week"]
        + weights["long_run_durability_log"] * math.log1p(factors["long_run_durability"])
        + weights["consistency_runs_per_week"] * factors["consistency_runs_per_week"]
    )


def conditions_multiplier(elev_ratio: float, temp_c: float, *, is_trail: bool) -> JsonDict:
    """How much these conditions slow a race relative to flat road at ~10 C."""
    elevation = max(1.0, elev_ratio)
    if temp_c > WARM_THRESHOLD_C:
        temperature = 1.0 + HEAT_PENALTY_PER_C * (temp_c - WARM_THRESHOLD_C)
    elif temp_c < COLD_THRESHOLD_C:
        temperature = 1.0 + COLD_PENALTY_PER_C * (COLD_THRESHOLD_C - temp_c)
    else:
        temperature = 1.0
    surface = 1.0 + TRAIL_SURFACE_PENALTY if is_trail else 1.0
    return {
        "elevation": elevation,
        "temperature": temperature,
        "surface": surface,
        "temp_c": temp_c,
        "factor": elevation * temperature * surface,
    }


def estimate_temp_c(date_iso: str, override: object) -> float:
    if override is not None:
        return float(cast("SupportsFloat", override))
    return MONTHLY_MEAN_TEMP_C[datetime.fromisoformat(date_iso).month]


def elevation_ratio(run: Run) -> float:
    """Flat-equivalent stretch from the course profile.

    Prefer the stream grade-adjusted distance; fall back to elevation gain per km
    for runs without detailed streams.
    """
    gap = run.grade_adjusted_km / run.km if run.km > 0 else 1.0
    fallback = 1.0 + ELEV_PENALTY_PER_M_PER_KM * (run.elevation_m / max(run.km, 1e-6))
    return max(1.0, gap, fallback)


def conditions_for_race(run: Run, race: JsonDict) -> JsonDict:
    temp_c = estimate_temp_c(str(race["date"]), race.get("temp_c"))
    surface = race.get("surface")
    is_trail = surface == "trail" if surface is not None else run.is_trail
    return conditions_multiplier(elevation_ratio(run), temp_c, is_trail=is_trail)


def goal_conditions_multiplier() -> JsonDict:
    spec = MARATHON_GOAL.goal_conditions
    gain_per_km = float(spec.get("elevation_gain_per_km", 0.0))
    elev_ratio = 1.0 + ELEV_PENALTY_PER_M_PER_KM * gain_per_km
    temp_c = estimate_temp_c(MARATHON_GOAL.race_date, spec.get("temp_c"))
    is_trail = str(spec.get("surface", "road")) == "trail"
    return conditions_multiplier(elev_ratio, temp_c, is_trail=is_trail)


def feature_medians(rows: list[JsonDict]) -> dict[str, float]:
    medians: dict[str, float] = {}
    for name in IMPUTED_FEATURES:
        present = [
            cast("dict[str, float]", row["features"])[name]
            for row in rows
            if cast("dict[str, float]", row["features"])[name] != 0.0
        ]
        medians[name] = float(np.median(present)) if present else 0.0
    return medians


def feature_vector(factors: dict[str, float], medians: dict[str, float]) -> np.ndarray:
    values: list[float] = []
    for name in MODEL_FEATURES:
        value = float(factors[name])
        if name in IMPUTED_FEATURES and value == 0.0:
            value = medians.get(name, 0.0)
        values.append(value)
    return np.array(values, dtype=float)


def build_dataset(runs: list[Run], races: list[JsonDict]) -> list[JsonDict]:
    by_id = {run.id: run for run in runs}
    rows: list[JsonDict] = []
    for race in races:
        activity_id = int(race["activity_id"])
        activity = by_id.get(activity_id)
        if activity is None:
            continue
        race_ts = activity.ts
        features = factors_at(runs, race_ts, exclude_id=activity_id)
        conditions = conditions_for_race(activity, race)
        time_sec = float(race["time_sec"])
        rows.append(
            {
                "activity_id": activity_id,
                "name": race["name"],
                "date": race["date"],
                "race_ts": race_ts,
                "distance_km": float(race["distance_km"]),
                "time_sec": time_sec,
                "neutral_time_sec": time_sec / conditions["factor"],
                "conditions": conditions,
                "type": race.get("type", "race"),
                "effort": race.get("effort"),
                "features": features,
            }
        )
    rows.sort(key=operator.itemgetter("date"))
    return rows


def fit_ridge(rows: list[JsonDict]) -> JsonDict:
    if not rows:
        msg = "No race labels available. Add races to data/entered/races.json."
        raise SystemExit(msg)

    medians = feature_medians(rows)
    x_raw = np.array(
        [feature_vector(cast("dict[str, float]", row["features"]), medians) for row in rows],
        dtype=float,
    )
    means = x_raw.mean(axis=0)
    scales = x_raw.std(axis=0)
    scales = np.where(scales < 1e-6, 1.0, scales)
    x = (x_raw - means) / scales
    # Labels are neutralized for course, weather and surface before the fit so the
    # fitness coefficients are not polluted by a hot or hilly race day.
    y = np.array(
        [
            math.log(float(row["neutral_time_sec"]))
            - RIEGEL_EXPONENT * math.log(float(row["distance_km"]))
            for row in rows
        ],
        dtype=float,
    )

    design = np.column_stack([np.ones(len(rows)), x])
    penalty = np.eye(design.shape[1]) * RIDGE_L2
    penalty[0, 0] = 0.0
    beta = np.linalg.solve(design.T @ design + penalty, design.T @ y)
    fitted = design @ beta
    residuals = y - fitted
    sigma = float(np.sqrt(np.mean(residuals**2))) if len(rows) > 1 else 0.08

    annotated_rows = []
    for row in rows:
        predicted = predict_time(
            float(row["distance_km"]),
            cast("dict[str, float]", row["features"]),
            beta,
            means,
            scales,
            medians,
        )
        annotated_rows.append(
            {
                **row,
                "predicted_neutral_time_sec": predicted,
                "residual_sec": float(row["neutral_time_sec"]) - predicted,
            }
        )

    return {
        "kind": "ridge_log_time_residual",
        "distance_curve": {"type": "riegel", "exponent": RIEGEL_EXPONENT},
        "regularization": {"l2": RIDGE_L2, "intercept_penalized": False},
        "features": MODEL_FEATURES,
        "fixed_factor_score_weights": MARATHON_GOAL.fitness_score_weights,
        "feature_means": dict(zip(MODEL_FEATURES, map(float, means), strict=False)),
        "feature_scales": dict(zip(MODEL_FEATURES, map(float, scales), strict=False)),
        "feature_medians": medians,
        "intercept": float(beta[0]),
        "coefficients": dict(zip(MODEL_FEATURES, map(float, beta[1:]), strict=False)),
        "training_rmse_log": sigma,
        "training_rmse_pct": math.expm1(sigma),
        "rows": annotated_rows,
    }


def predict_time(
    distance_km: float,
    factors: dict[str, float],
    beta: np.ndarray,
    means: np.ndarray,
    scales: np.ndarray,
    medians: dict[str, float],
) -> float:
    z = (feature_vector(factors, medians) - means) / scales
    y = beta[0] + float(z @ beta[1:]) + RIEGEL_EXPONENT * math.log(distance_km)
    return float(math.exp(y))


def fitness_linear(
    factors: dict[str, float],
    means: np.ndarray,
    scales: np.ndarray,
    coef: np.ndarray,
    medians: dict[str, float],
) -> float:
    z = (feature_vector(factors, medians) - means) / scales
    return float(z @ coef)


def race_evidence_prediction(
    distance_km: float,
    factors: dict[str, float],
    rows: list[JsonDict],
    as_of_ts: float,
    beta: np.ndarray,
    means: np.ndarray,
    scales: np.ndarray,
    medians: dict[str, float],
) -> JsonDict | None:
    if not rows:
        return None
    coef = beta[1:]
    f_now = fitness_linear(factors, means, scales, coef, medians)

    candidates = []
    for row in rows:
        row_features = cast("dict[str, float]", row["features"])
        f_race = fitness_linear(row_features, means, scales, coef, medians)
        # Take the race's neutral (course/weather-normalized) time, slide it to the
        # target distance with Riegel, then to today's fitness with the model delta.
        adjusted = (
            float(row["neutral_time_sec"])
            * (distance_km / float(row["distance_km"])) ** RIEGEL_EXPONENT
            * math.exp(f_now - f_race)
        )
        age_days = max(0.0, (as_of_ts - float(row["race_ts"])) / DAY_SEC)
        confidence = math.exp(-age_days / 365.0)
        candidates.append(
            {
                "activity_id": row["activity_id"],
                "name": row["name"],
                "date": row["date"],
                "source_distance_km": row["distance_km"],
                "adjusted_time_sec": adjusted,
                "confidence": confidence,
            }
        )

    # Use the strongest performance evidence, but keep confidence/age in the
    # artifact so this can become a weighted posterior once there are more races.
    best = min(candidates, key=operator.itemgetter("adjusted_time_sec"))
    return {
        "time_sec": best["adjusted_time_sec"],
        "source": best,
        "candidates": candidates,
    }


def blended_prediction(
    distance_km: float,
    factors: dict[str, float],
    beta: np.ndarray,
    means: np.ndarray,
    scales: np.ndarray,
    model: JsonDict,
    as_of_ts: float,
    medians: dict[str, float],
) -> JsonDict:
    model_sec = predict_time(distance_km, factors, beta, means, scales, medians)
    evidence = race_evidence_prediction(
        distance_km,
        factors,
        cast("list[JsonDict]", model["rows"]),
        as_of_ts,
        beta,
        means,
        scales,
        medians,
    )
    if evidence is None:
        time_sec = model_sec
        evidence_sec = None
        blend_weight = 0.0
    else:
        evidence_sec = evidence["time_sec"]
        blend_weight = 0.65
        time_sec = math.exp(
            (1.0 - blend_weight) * math.log(model_sec) + blend_weight * math.log(evidence_sec)
        )
    return {
        "distance_km": distance_km,
        "time_sec": time_sec,
        "model_time_sec": model_sec,
        "race_evidence_time_sec": evidence_sec,
        "race_evidence": evidence,
        "blend_weight": blend_weight,
    }


def prediction_components(
    factors: dict[str, float],
    model: JsonDict,
    medians: dict[str, float],
) -> dict[str, float]:
    out = {}
    feature_means = cast("dict[str, float]", model["feature_means"])
    feature_scales = cast("dict[str, float]", model["feature_scales"])
    coefficients = cast("dict[str, float]", model["coefficients"])
    for value, name in zip(feature_vector(factors, medians), MODEL_FEATURES, strict=True):
        z = (float(value) - feature_means[name]) / feature_scales[name]
        out[name] = z * coefficients[name]
    return out


def build_series(  # ruff:ignore[too-many-arguments]
    runs: list[Run],
    model: JsonDict,
    beta: np.ndarray,
    means: np.ndarray,
    scales: np.ndarray,
    medians: dict[str, float],
    start: float,
    end: float,
    *,
    as_of: float,
) -> dict[str, list[dict[str, float | str]]]:
    out: dict[str, list[dict[str, float | str]]] = {
        key: [] for key in MARATHON_GOAL.prediction_distances
    }
    t = start
    while t <= end:
        # Past days use that day's fitness; days after the last run hold current
        # fitness flat (a maintenance projection) rather than decaying to detrained.
        eval_ts = min(t, as_of)
        factors = factors_at(runs, eval_ts)
        date = date_from_ts(t)
        for key, km in MARATHON_GOAL.prediction_distances.items():
            pred = blended_prediction(km, factors, beta, means, scales, model, eval_ts, medians)
            out[key].append({"date": date, "time_sec": round(float(pred["time_sec"]))})
        t += DAY_SEC
    return out


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def main() -> None:
    activities = cast("list[JsonDict]", load_json(ACTIVITIES_PATH, []))
    details = cast("dict[str, JsonDict]", load_json(DETAILS_PATH, {}))
    races = cast("list[JsonDict]", load_json(RACES_PATH, []))
    runs = normalize_runs(activities, details)
    if not runs:
        msg = "No runs found. Run `uv run stride-sync sync` first."
        raise SystemExit(msg)

    labels = build_dataset(runs, races)
    model = fit_ridge(labels)
    medians = cast("dict[str, float]", model["feature_medians"])

    beta = np.array(
        [float(model["intercept"])]
        + [cast("dict[str, float]", model["coefficients"])[name] for name in MODEL_FEATURES],
        dtype=float,
    )
    means = np.array(
        [cast("dict[str, float]", model["feature_means"])[name] for name in MODEL_FEATURES],
        dtype=float,
    )
    scales = np.array(
        [cast("dict[str, float]", model["feature_scales"])[name] for name in MODEL_FEATURES],
        dtype=float,
    )

    now = datetime.now(UTC).timestamp()
    as_of = max(now, runs[-1].ts)
    sigma = math.exp(float(model["training_rmse_log"]))
    current_factors = factors_at(runs, as_of)
    current_predictions = {
        key: blended_prediction(km, current_factors, beta, means, scales, model, as_of, medians)
        for key, km in MARATHON_GOAL.prediction_distances.items()
    }
    for pred in current_predictions.values():
        pred["low_sec"] = float(pred["time_sec"]) / sigma
        pred["high_sec"] = float(pred["time_sec"]) * sigma

    # Goal-race prediction: the neutral marathon estimate re-dressed in the goal
    # course's profile, weather and surface.
    goal_conditions = goal_conditions_multiplier()
    goal_distance = MARATHON_GOAL.prediction_distances.get("marathon", 42.195)
    goal_neutral = blended_prediction(
        goal_distance, current_factors, beta, means, scales, model, as_of, medians
    )
    goal_adjusted = float(goal_neutral["time_sec"]) * float(goal_conditions["factor"])
    goal_prediction = {
        "name": MARATHON_GOAL.race_date,
        "distance_km": goal_distance,
        "neutral_time_sec": float(goal_neutral["time_sec"]),
        "conditions": goal_conditions,
        "adjusted_time_sec": goal_adjusted,
        "low_sec": goal_adjusted / sigma,
        "high_sec": goal_adjusted * sigma,
    }

    # Predictions at all times: the entire run history through race day.
    start = runs[0].ts
    end = datetime.fromisoformat(MARATHON_GOAL.race_date).replace(tzinfo=UTC).timestamp()
    series = build_series(runs, model, beta, means, scales, medians, start, end, as_of=as_of)

    artifact = {
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of": datetime.fromtimestamp(as_of, UTC).isoformat(),
        "schema_version": 2,
        "label_count": len(labels),
        "labels": labels,
        "model": model,
        "current_factors": current_factors,
        "current_predictions": current_predictions,
        "goal_prediction": goal_prediction,
        "prediction_components": prediction_components(current_factors, model, medians),
        "series": series,
        "notes": [
            "Race labels are manually registered in data/entered/races.json.",
            (
                "Training features for each race use only runs before the race start and exclude "
                "the race activity itself."
            ),
            (
                "Each race time is normalized to neutral conditions (flat road, ~10 C) before "
                "the fit using course profile, temperature and surface; predictions are neutral "
                "and the goal race is re-adjusted to its own conditions."
            ),
            (
                "Temperature comes from a per-race override when present, otherwise Montreal "
                "monthly climatology, since Strava activities here carry no temperature."
            ),
            (
                "series spans every prediction distance daily from the first run through race "
                "day; days after the last run hold current fitness flat (maintenance projection)."
            ),
        ],
    }
    save_json(MODEL_PATH, artifact)


if __name__ == "__main__":
    main()
