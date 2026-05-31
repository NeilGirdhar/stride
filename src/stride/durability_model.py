"""Build a runner-specific durability model from cached Strava stream samples.

Pure computation: reads the segment cache written by `uv run stride-sync streams`
(data/private/strava-durability-samples.json) and does no network I/O.
"""

from __future__ import annotations

import math
import operator
import pathlib
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, cast

import numpy as np

from stride.sync_strava import (
    SAMPLES_PATH,
    Segment,
    load_json,
    save_json,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
GENERATED_DIR = DATA_DIR / "generated"
MODEL_PATH = GENERATED_DIR / "durability-model.json"

FRESH_CAE_FLOOR = 1200.0
FRESH_CAE_CEILING = 4500.0
RETENTION_THRESHOLD = 0.9
ROLLING_SERIES_DAYS = 180

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class Sample:
    run_id: int
    date: str
    cae: float
    effort_rate: float
    efficiency: float


def samples_from_segments(segments: list[Segment]) -> list[Sample]:
    if not segments:
        return []
    median_speed = float(np.median([segment.speed for segment in segments]))
    median_hr = float(np.median([segment.hr for segment in segments]))
    by_run: dict[int, list[Segment]] = defaultdict(list)
    for segment in segments:
        by_run[segment.run_id].append(segment)

    samples: list[Sample] = []
    for run_segments in by_run.values():
        cae = 0.0
        for segment in run_segments:
            speed_factor = (segment.speed / median_speed) ** 1.1 if median_speed else 1.0
            hr_factor = (segment.hr / median_hr) ** 1.6 if median_hr else 1.0
            delta_cae = segment.adjusted_speed * segment.dt * speed_factor * hr_factor
            cae += delta_cae
            effort_rate = delta_cae / segment.dt
            efficiency = segment.adjusted_speed * 60.0 / segment.hr
            if math.isfinite(cae + effort_rate + efficiency):
                samples.append(
                    Sample(
                        run_id=segment.run_id,
                        date=segment.date,
                        cae=cae,
                        effort_rate=effort_rate,
                        efficiency=efficiency,
                    )
                )
    return samples


def fit_baseline(samples: list[Sample]) -> list[tuple[float, float]]:
    cae_values = np.array([sample.cae for sample in samples], dtype=float)
    fresh_limit = float(min(FRESH_CAE_CEILING, max(FRESH_CAE_FLOOR, np.quantile(cae_values, 0.2))))
    fresh = [sample for sample in samples if sample.cae <= fresh_limit]
    if len(fresh) < 100:
        fresh = sorted(samples, key=operator.attrgetter("cae"))[: max(100, len(samples) // 5)]
    return binned_medians(
        [(sample.effort_rate, sample.efficiency) for sample in fresh],
        bins=14,
        min_bin_size=25,
    )


def binned_medians(
    points: list[tuple[float, float]], bins: int, min_bin_size: int
) -> list[tuple[float, float]]:
    if not points:
        return []
    xs = np.array([point[0] for point in points], dtype=float)
    edges = np.quantile(xs, np.linspace(0, 1, bins + 1))
    out = []
    for lo, hi in pairwise(edges):
        bucket = [point for point in points if lo <= point[0] <= hi]
        if len(bucket) < min_bin_size:
            continue
        out.append(
            (
                float(np.median([point[0] for point in bucket])),
                float(np.median([point[1] for point in bucket])),
            )
        )
    return dedupe_curve(out)


def dedupe_curve(curve: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = []
    last_x = None
    for x, y in sorted(curve):
        if last_x is None or abs(x - last_x) > 1e-9:
            out.append((x, y))
            last_x = x
    return out


def interp_curve(curve: list[tuple[float, float]], x: float) -> float | None:
    if not curve:
        return None
    if x <= curve[0][0]:
        return curve[0][1]
    if x >= curve[-1][0]:
        return curve[-1][1]
    for (x0, y0), (x1, y1) in pairwise(curve):
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            frac = (x - x0) / (x1 - x0)
            return y0 + (y1 - y0) * frac
    return None


def retained_samples(
    samples: list[Sample], baseline: list[tuple[float, float]]
) -> list[tuple[Sample, float]]:
    retained = []
    for sample in samples:
        expected = interp_curve(baseline, sample.effort_rate)
        if not expected or expected <= 0:
            continue
        value = sample.efficiency / expected
        if 0.45 <= value <= 1.35:
            retained.append((sample, value))
    if not retained:
        return []
    fresh_values = [value for sample, value in retained if sample.cae <= FRESH_CAE_FLOOR]
    norm = float(np.median(fresh_values)) if fresh_values else 1.0
    return [(sample, value / norm) for sample, value in retained if norm > 0]


def durability_curve(retained: list[tuple[Sample, float]]) -> list[JsonDict]:
    points = [(sample.cae, value) for sample, value in retained]
    bins = binned_retention(points, bins=28, min_bin_size=40)
    out = []
    best = 2.0
    for cae, retained_efficiency, count in bins:
        best = min(best, retained_efficiency)
        out.append(
            {
                "cae": round(cae, 1),
                "retained_efficiency": round(best, 4),
                "samples": count,
            }
        )
    return out


def binned_retention(
    points: list[tuple[float, float]], bins: int, min_bin_size: int
) -> list[tuple[float, float, int]]:
    if not points:
        return []
    xs = np.array([point[0] for point in points], dtype=float)
    edges = np.quantile(xs, np.linspace(0, 1, bins + 1))
    out = []
    for lo, hi in pairwise(edges):
        bucket = [point for point in points if lo <= point[0] <= hi]
        if len(bucket) < min_bin_size:
            continue
        out.append(
            (
                float(np.median([point[0] for point in bucket])),
                float(np.median([point[1] for point in bucket])),
                len(bucket),
            )
        )
    return out


def threshold_cae(curve: list[JsonDict]) -> float | None:
    previous = None
    for point in curve:
        cae = float(point["cae"])
        retained = float(point["retained_efficiency"])
        if retained <= RETENTION_THRESHOLD:
            if previous is None:
                return cae
            prev_cae, prev_retained = previous
            if prev_retained == retained:
                return cae
            frac = (prev_retained - RETENTION_THRESHOLD) / (prev_retained - retained)
            return prev_cae + (cae - prev_cae) * frac
        previous = (cae, retained)
    return None


def per_run_thresholds(retained: list[tuple[Sample, float]]) -> list[JsonDict]:
    by_run: dict[int, list[tuple[Sample, float]]] = defaultdict(list)
    for sample, value in retained:
        by_run[sample.run_id].append((sample, value))

    rows = []
    for run_id, values in by_run.items():
        if len(values) < 30:
            continue
        values.sort(key=lambda item: item[0].cae)
        smoothed = rolling_median(values, window=15)
        for sample, value in smoothed:
            if sample.cae >= FRESH_CAE_FLOOR and value <= RETENTION_THRESHOLD:
                rows.append(
                    {
                        "activity_id": run_id,
                        "date": sample.date,
                        "cae_90": round(sample.cae, 1),
                    }
                )
                break
    return sorted(rows, key=operator.itemgetter("date"))


def rolling_median(values: list[tuple[Sample, float]], window: int) -> list[tuple[Sample, float]]:
    half = window // 2
    out = []
    for i, (sample, _value) in enumerate(values):
        lo = max(0, i - half)
        hi = min(len(values), i + half + 1)
        out.append((sample, float(np.median([value for _sample, value in values[lo:hi]]))))
    return out


def rolling_series(rows: list[JsonDict]) -> list[JsonDict]:
    if not rows:
        return []
    out = []
    for row in rows:
        end = datetime.fromisoformat(str(row["date"])).date()
        start = end - timedelta(days=ROLLING_SERIES_DAYS)
        values = [
            float(other["cae_90"])
            for other in rows
            if start.isoformat() <= str(other["date"]) <= end.isoformat()
        ]
        if len(values) >= 2:
            out.append({"date": row["date"], "cae_90": round(float(np.median(values)), 1)})
    return out


def load_samples() -> list[Sample]:
    """Build samples from the cached stream segments (no network I/O).

    The cache is filled by `uv run stride-sync streams`.
    """
    cache = cast("dict[str, JsonDict]", load_json(SAMPLES_PATH, {}) or {})
    if not cache:
        sys.exit("No durability streams cached yet. Run:  uv run stride-sync streams")

    segments = [
        Segment(
            run_id=int(run_id),
            date=str(row["date"]),
            dt=float(segment["dt"]),
            speed=float(segment["speed"]),
            adjusted_speed=float(segment["adjusted_speed"]),
            hr=float(segment["hr"]),
        )
        for run_id, row in cache.items()
        for segment in cast("list[JsonDict]", row.get("segments") or [])
    ]
    return samples_from_segments(segments)


def build_model(samples: list[Sample]) -> JsonDict:
    if len(samples) < 500:
        msg = (
            "Not enough durability stream samples. "
            "Run `uv run stride-durability-model` again after more runs are synced."
        )
        raise SystemExit(msg)

    baseline = fit_baseline(samples)
    retained = retained_samples(samples, baseline)
    curve = durability_curve(retained)
    cae90 = threshold_cae(curve)
    run_thresholds = per_run_thresholds(retained)
    series = rolling_series(run_thresholds)
    runs_used = len({sample.run_id for sample in samples})

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "durability_cae_90": round(cae90, 1) if cae90 is not None else None,
            "unit": "CAE",
            "definition": (
                "How much cumulative active exertion you can absorb before "
                "grade-adjusted metres per heartbeat drops by 10%."
            ),
            "runs_used": runs_used,
            "sample_count": len(samples),
            "retained_sample_count": len(retained),
        },
        "fresh_efficiency_curve": [
            {"effort_rate": round(x, 4), "efficiency_m_per_beat": round(y, 4)} for x, y in baseline
        ],
        "durability_curve": curve,
        "run_thresholds": run_thresholds,
        "series": series,
    }


def main() -> None:
    samples = load_samples()
    model = build_model(samples)
    save_json(MODEL_PATH, model)
    summary = cast("JsonDict", model["summary"])
    print(
        "Durability: "
        f"{summary['durability_cae_90']} {summary['unit']} "
        f"from {summary['runs_used']} runs / {summary['sample_count']} samples"
    )


if __name__ == "__main__":
    main()
