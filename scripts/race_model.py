#!/usr/bin/env python
"""Build Stride's supervised race prediction artifact.

Runtime stays static: this script reads Strava exports plus a manually editable
race registry and writes data/race-model.json for the browser.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
ACTIVITIES_PATH = os.path.join(DATA_DIR, "strava-activities.json")
RACES_PATH = os.path.join(DATA_DIR, "races.json")
MODEL_PATH = os.path.join(DATA_DIR, "race-model.json")

RUN_TYPES = {"Run", "TrailRun"}
DAY_SEC = 86400
FITNESS_TAU = 42.0
FATIGUE_TAU = 7.0
RIEGEL_EXPONENT = 1.06
MARATHON_PACE_SEC = 256.0

PREDICTION_DISTANCES = {
    "5k": 5.0,
    "10k": 10.0,
    "half": 21.097,
    "30k": 30.0,
    "marathon": 42.195,
}

MODEL_FEATURES = ["fitness_score"]

FITNESS_SCORE_WEIGHTS = {
    "fitness": 1.0,
    "form": 0.25,
    "volume_28d_km_per_week": 0.18,
    "long_run_durability_log": 1.5,
    "consistency_runs_per_week": 0.8,
}


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
    hr: float | None
    load: float


def load_json(path: str, default: Any) -> Any:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")


def parse_ts(value: str) -> float:
    text = value.replace("Z", "+00:00")
    return datetime.fromisoformat(text).timestamp()


def date_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def normalize_runs(activities: list[dict[str, Any]]) -> list[Run]:
    raw = []
    for activity in activities:
        sport = activity.get("sport_type") or activity.get("type")
        distance = float(activity.get("distance") or 0)
        moving_time = float(activity.get("moving_time") or 0)
        if sport not in RUN_TYPES or distance <= 0 or moving_time <= 0:
            continue
        km = distance / 1000.0
        raw.append(
            {
                "id": int(activity["id"]),
                "name": activity.get("name") or "",
                "ts": parse_ts(
                    activity.get("start_date_local") or activity["start_date"]
                ),
                "km": km,
                "moving_time": moving_time,
                "pace_sec": moving_time / km,
                "elevation_m": float(activity.get("total_elevation_gain") or 0),
                "hr": activity.get("average_heartrate"),
            }
        )
    raw.sort(key=lambda run: run["ts"])
    paces = [run["pace_sec"] for run in raw if run["pace_sec"] > 0]
    baseline = float(np.median(paces)) if paces else 0.0

    runs = []
    for run in raw:
        pace_boost = 0.0
        if baseline:
            pace_boost = clamp((baseline - run["pace_sec"]) / baseline, -0.12, 0.22)
        load = (run["km"] + run["elevation_m"] / 100.0) * (1.0 + pace_boost)
        runs.append(
            Run(
                id=run["id"],
                name=run["name"],
                ts=run["ts"],
                date=date_from_ts(run["ts"]),
                km=run["km"],
                moving_time=run["moving_time"],
                pace_sec=run["pace_sec"],
                elevation_m=run["elevation_m"],
                hr=float(run["hr"]) if run["hr"] else None,
                load=load,
            )
        )
    return runs


def state_at(
    runs: list[Run], ts: float, exclude_id: int | None = None
) -> dict[str, float]:
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
    contribution,
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


def recent_runs(
    runs: list[Run], ts: float, days: int, exclude_id: int | None = None
) -> list[Run]:
    start = ts - days * DAY_SEC
    return [run for run in runs if start <= run.ts < ts and run.id != exclude_id]


def factors_at(
    runs: list[Run], ts: float, exclude_id: int | None = None
) -> dict[str, float]:
    state = state_at(runs, ts, exclude_id)
    r28 = recent_runs(runs, ts, 28, exclude_id)
    r56 = recent_runs(runs, ts, 56, exclude_id)
    easy_hr = [
        1_000_000.0 / (run.pace_sec * run.hr)
        for run in recent_runs(runs, ts, 75, exclude_id)
        if run.hr and run.pace_sec > 300
    ]

    volume_28 = sum(run.km for run in r28) / 4.0
    consistency = len({date_from_ts(run.ts) for run in r56}) / 8.0
    longest_56 = max((run.km for run in r56), default=0.0)
    mp_specificity = ewma_rate_before(
        runs,
        ts,
        21.0,
        lambda run: (
            run.km
            * math.exp(-0.5 * ((run.pace_sec - MARATHON_PACE_SEC) / 12.0) ** 2)
            * clamp((run.km - 12.0) / 4.0, 0.0, 1.0)
        ),
        exclude_id,
    )
    long_run_durability = ewma_rate_before(
        runs,
        ts,
        28.0,
        lambda run: max(0.0, run.km - 20.0),
        exclude_id,
    )

    factors = {
        **state,
        "volume_28d_km_per_week": volume_28,
        "volume_56d_km_per_week": sum(run.km for run in r56) / 8.0,
        "longest_run_56d_km": longest_56,
        "long_run_durability": long_run_durability,
        "mp_specificity": mp_specificity,
        "aerobic_efficiency": float(np.median(easy_hr)) if easy_hr else 0.0,
        "consistency_runs_per_week": consistency,
    }
    factors["fitness_score"] = fitness_score(factors)
    return factors


def fitness_score(factors: dict[str, float]) -> float:
    """Single low-variance score used by the first supervised fit.

    The component factors are still exported for inspection. With only a few
    race labels, learning every component independently is unstable, so this
    first version learns how strongly this aggregate score moves race times.
    """
    return (
        FITNESS_SCORE_WEIGHTS["fitness"] * factors["fitness"]
        + FITNESS_SCORE_WEIGHTS["form"] * factors["form"]
        + FITNESS_SCORE_WEIGHTS["volume_28d_km_per_week"]
        * factors["volume_28d_km_per_week"]
        + FITNESS_SCORE_WEIGHTS["long_run_durability_log"]
        * math.log1p(factors["long_run_durability"])
        + FITNESS_SCORE_WEIGHTS["consistency_runs_per_week"]
        * factors["consistency_runs_per_week"]
    )


def build_dataset(runs: list[Run], races: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {run.id: run for run in runs}
    rows = []
    for race in races:
        activity_id = int(race["activity_id"])
        activity = by_id.get(activity_id)
        if activity is None:
            continue
        race_ts = activity.ts
        features = factors_at(runs, race_ts, exclude_id=activity_id)
        rows.append(
            {
                "activity_id": activity_id,
                "name": race["name"],
                "date": race["date"],
                "distance_km": float(race["distance_km"]),
                "time_sec": float(race["time_sec"]),
                "type": race.get("type", "race"),
                "effort": race.get("effort"),
                "features": features,
            }
        )
    rows.sort(key=lambda row: row["date"])
    return rows


def fit_ridge(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise SystemExit("No race labels available. Add races to data/races.json.")

    x_raw = np.array(
        [[row["features"][name] for name in MODEL_FEATURES] for row in rows],
        dtype=float,
    )
    means = x_raw.mean(axis=0)
    scales = x_raw.std(axis=0)
    scales = np.where(scales < 1e-6, 1.0, scales)
    x = (x_raw - means) / scales
    y = np.array(
        [
            math.log(row["time_sec"]) - RIEGEL_EXPONENT * math.log(row["distance_km"])
            for row in rows
        ],
        dtype=float,
    )

    design = np.column_stack([np.ones(len(rows)), x])
    penalty = np.eye(design.shape[1]) * 6.0
    penalty[0, 0] = 0.0
    beta = np.linalg.solve(design.T @ design + penalty, design.T @ y)
    fitted = design @ beta
    residuals = y - fitted
    sigma = float(np.sqrt(np.mean(residuals**2))) if len(rows) > 1 else 0.08

    return {
        "kind": "ridge_log_time_residual",
        "distance_curve": {"type": "riegel", "exponent": RIEGEL_EXPONENT},
        "regularization": {"l2": 6.0, "intercept_penalized": False},
        "features": MODEL_FEATURES,
        "fixed_factor_score_weights": FITNESS_SCORE_WEIGHTS,
        "feature_means": dict(zip(MODEL_FEATURES, map(float, means))),
        "feature_scales": dict(zip(MODEL_FEATURES, map(float, scales))),
        "intercept": float(beta[0]),
        "coefficients": dict(zip(MODEL_FEATURES, map(float, beta[1:]))),
        "training_rmse_log": sigma,
        "training_rmse_pct": math.expm1(sigma),
        "rows": [
            {
                **row,
                "predicted_time_sec": predict_time(
                    row["distance_km"], row["features"], beta, means, scales
                ),
                "residual_sec": row["time_sec"]
                - predict_time(
                    row["distance_km"], row["features"], beta, means, scales
                ),
            }
            for row in rows
        ],
    }


def predict_time(
    distance_km: float,
    factors: dict[str, float],
    beta: np.ndarray,
    means: np.ndarray,
    scales: np.ndarray,
) -> float:
    x = np.array([factors[name] for name in MODEL_FEATURES], dtype=float)
    z = (x - means) / scales
    y = beta[0] + float(z @ beta[1:]) + RIEGEL_EXPONENT * math.log(distance_km)
    return float(math.exp(y))


def race_evidence_prediction(
    distance_km: float,
    factors: dict[str, float],
    rows: list[dict[str, Any]],
    model: dict[str, Any],
    as_of_ts: float,
) -> dict[str, Any] | None:
    if not rows:
        return None
    score = factors["fitness_score"]
    mean = model["feature_means"]["fitness_score"]
    scale = model["feature_scales"]["fitness_score"]
    coef = model["coefficients"]["fitness_score"]
    z_now = (score - mean) / scale

    candidates = []
    for row in rows:
        row_score = row["features"]["fitness_score"]
        z_race = (row_score - mean) / scale
        adjusted = (
            row["time_sec"]
            * (distance_km / row["distance_km"]) ** RIEGEL_EXPONENT
            * math.exp(coef * (z_now - z_race))
        )
        race_ts = (
            datetime.fromisoformat(row["date"]).replace(tzinfo=timezone.utc).timestamp()
        )
        age_days = max(0.0, (as_of_ts - race_ts) / DAY_SEC)
        support = clamp(score / max(row_score, 1e-6), 0.75, 1.25)
        confidence = math.exp(-age_days / 365.0) * support
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
    best = min(candidates, key=lambda item: item["adjusted_time_sec"])
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
    model: dict[str, Any],
    as_of_ts: float,
) -> dict[str, Any]:
    model_sec = predict_time(distance_km, factors, beta, means, scales)
    evidence = race_evidence_prediction(
        distance_km, factors, model["rows"], model, as_of_ts
    )
    if evidence is None:
        time_sec = model_sec
        evidence_sec = None
        blend_weight = 0.0
    else:
        evidence_sec = evidence["time_sec"]
        blend_weight = 0.65
        time_sec = math.exp(
            (1.0 - blend_weight) * math.log(model_sec)
            + blend_weight * math.log(evidence_sec)
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
    model: dict[str, Any],
) -> dict[str, float]:
    out = {}
    for name in MODEL_FEATURES:
        z = (factors[name] - model["feature_means"][name]) / model["feature_scales"][
            name
        ]
        out[name] = z * model["coefficients"][name]
    return out


def build_series(
    runs: list[Run],
    model: dict[str, Any],
    beta: np.ndarray,
    means: np.ndarray,
    scales: np.ndarray,
    start: float,
    end: float,
) -> dict[str, list[dict[str, float | str]]]:
    out = {key: [] for key in PREDICTION_DISTANCES}
    t = start
    while t <= end:
        factors = factors_at(runs, t)
        date = date_from_ts(t)
        for key, km in PREDICTION_DISTANCES.items():
            pred = blended_prediction(km, factors, beta, means, scales, model, t)
            out[key].append({"date": date, "time_sec": pred["time_sec"]})
        t += DAY_SEC
    return out


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def main() -> None:
    activities = load_json(ACTIVITIES_PATH, [])
    races = load_json(RACES_PATH, [])
    runs = normalize_runs(activities)
    if not runs:
        raise SystemExit("No runs found. Run scripts/sync_strava.py first.")

    labels = build_dataset(runs, races)
    model = fit_ridge(labels)

    beta = np.array(
        [model["intercept"]] + [model["coefficients"][name] for name in MODEL_FEATURES],
        dtype=float,
    )
    means = np.array(
        [model["feature_means"][name] for name in MODEL_FEATURES], dtype=float
    )
    scales = np.array(
        [model["feature_scales"][name] for name in MODEL_FEATURES], dtype=float
    )

    now = datetime.now(timezone.utc).timestamp()
    as_of = max(now, runs[-1].ts)
    current_factors = factors_at(runs, as_of)
    current_predictions = {
        key: blended_prediction(km, current_factors, beta, means, scales, model, as_of)
        for key, km in PREDICTION_DISTANCES.items()
    }
    for pred in current_predictions.values():
        pred["low_sec"] = pred["time_sec"] / math.exp(model["training_rmse_log"])
        pred["high_sec"] = pred["time_sec"] * math.exp(model["training_rmse_log"])

    start = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    series = build_series(runs, model, beta, means, scales, start, as_of)

    artifact = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": datetime.fromtimestamp(as_of, timezone.utc).isoformat(),
        "schema_version": 1,
        "label_count": len(labels),
        "labels": labels,
        "model": model,
        "current_factors": current_factors,
        "current_predictions": current_predictions,
        "prediction_components": prediction_components(current_factors, model),
        "series": series,
        "notes": [
            "Race labels are manually registered in data/races.json.",
            "Training features for each race use only runs before the race start and exclude the race activity itself.",
            "Ordinary run pace is used only inside load/specificity factors, not as a race-performance label.",
        ],
    }
    save_json(MODEL_PATH, artifact)
    print(
        f"Wrote {MODEL_PATH} with {len(labels)} labels; "
        f"current marathon {current_predictions['marathon']['time_sec'] / 3600:.2f}h"
    )


if __name__ == "__main__":
    main()
