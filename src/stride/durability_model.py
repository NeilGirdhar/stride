"""Build a runner-specific durability model from cached Strava stream samples.

Pure computation: reads the segment cache written by `uv run stride-sync streams`
(data/private/strava-durability-samples.json) and does no network I/O.
"""

from __future__ import annotations

import math
import operator
import pathlib
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
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
ROLLING_SERIES_DAYS = 180
SERIES_STEP_DAYS = 7
MIN_WINDOW_SAMPLES = 2000
# Durability is reported as the fraction of fresh efficiency still held after this
# much cumulative load. A fixed reference (most runs reach it) keeps the metric
# scale-stable over time, unlike "CAE at which efficiency drops 10%", which is
# bounded by run length and swings wildly as the efficiency curve flattens.
REFERENCE_CAE = 4000.0

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
    arr = np.array(points, dtype=float)
    order = np.argsort(arr[:, 0])
    xs = arr[order, 0]
    ys = arr[order, 1]
    edges = np.quantile(xs, np.linspace(0, 1, bins + 1))
    out = []
    for lo, hi in pairwise(edges):
        left = int(np.searchsorted(xs, lo, side="left"))
        right = int(np.searchsorted(xs, hi, side="right"))
        if right - left < min_bin_size:
            continue
        out.append(
            (
                float(np.median(xs[left:right])),
                float(np.median(ys[left:right])),
                right - left,
            )
        )
    return out


def retained_at(curve: list[JsonDict], reference_cae: float) -> float | None:
    """Fraction of fresh efficiency retained at `reference_cae`, interpolated on the curve.

    Scale-stable: a fixed reference load most runs reach, so the value compares
    across time instead of riding the run-length tail like a drop-off threshold.
    """
    if not curve:
        return None
    points = [(float(p["cae"]), float(p["retained_efficiency"])) for p in curve]
    if reference_cae <= points[0][0]:
        return points[0][1]
    for (cae0, ret0), (cae1, ret1) in pairwise(points):
        if cae0 <= reference_cae <= cae1:
            frac = (reference_cae - cae0) / (cae1 - cae0) if cae1 > cae0 else 0.0
            return ret0 + (ret1 - ret0) * frac
    return points[-1][1]


def durability_series(retained: list[tuple[Sample, float]], window_days: int) -> list[JsonDict]:
    """Durability over time, on the summary's scale.

    For each weekly date it fits the efficiency-vs-load curve on a trailing window
    of samples and reads retained efficiency at REFERENCE_CAE. Series and headline
    number therefore share one scale, so the line tracks durability as it improves.
    Only full windows are emitted, so a partial window at the start of the history
    cannot read as artificially low.
    """
    dated = sorted(retained, key=lambda item: item[0].date)
    if not dated:
        return []
    ordinals = [date.fromisoformat(sample.date).toordinal() for sample, _ in dated]
    out: list[JsonDict] = []
    for end_ordinal in range(ordinals[0] + window_days, ordinals[-1] + 1, SERIES_STEP_DAYS):
        lo = bisect_left(ordinals, end_ordinal - window_days)
        hi = bisect_right(ordinals, end_ordinal)
        window = dated[lo:hi]
        if len(window) < MIN_WINDOW_SAMPLES:
            continue
        retained_fraction = retained_at(durability_curve(window), REFERENCE_CAE)
        if retained_fraction is not None:
            out.append(
                {
                    "date": date.fromordinal(end_ordinal).isoformat(),
                    "retained": round(retained_fraction, 4),
                }
            )
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
    series = durability_series(retained, ROLLING_SERIES_DAYS)
    # The headline number is the latest point on the series, so the two never
    # disagree (the Goals pane plots the series and pins this value as "today").
    current = series[-1]["retained"] if series else retained_at(curve, REFERENCE_CAE)
    runs_used = len({sample.run_id for sample in samples})

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "durability_retained": round(current, 4) if current is not None else None,
            "reference_cae": REFERENCE_CAE,
            "unit": "fraction of fresh efficiency",
            "definition": (
                f"Fraction of fresh grade-adjusted metres per heartbeat still held after "
                f"{REFERENCE_CAE:.0f} CAE of cumulative load in a run."
            ),
            "runs_used": runs_used,
            "sample_count": len(samples),
            "retained_sample_count": len(retained),
        },
        "fresh_efficiency_curve": [
            {"effort_rate": round(x, 4), "efficiency_m_per_beat": round(y, 4)} for x, y in baseline
        ],
        "durability_curve": curve,
        "series": series,
    }


def main() -> None:
    samples = load_samples()
    model = build_model(samples)
    save_json(MODEL_PATH, model)
    summary = cast("JsonDict", model["summary"])
    print(
        "Durability: "
        f"{summary['durability_retained'] * 100:.1f}% of fresh efficiency held at "
        f"{summary['reference_cae']:.0f} CAE "
        f"from {summary['runs_used']} runs / {summary['sample_count']} samples"
    )


if __name__ == "__main__":
    main()
