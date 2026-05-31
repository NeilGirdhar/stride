import { DAY } from "./ranges.js";

export const VOLUME_SMOOTH_SIGMA_DAYS = 9;

export function gaussianRateLine(
  points,
  start,
  end,
  sigmaMs = VOLUME_SMOOTH_SIGMA_DAYS * DAY,
  valueFor = (point) => point.km,
  samples = 200,
) {
  if (!points.length) return [];
  const t0 = points[0].t;
  const t1 = points[points.length - 1].t;
  const norm = 1 / (sigmaMs * Math.sqrt(2 * Math.PI));
  const out = [];

  for (let i = 0; i <= samples; i++) {
    const t = start + (end - start) * (i / samples);
    let s = 0;
    for (const p of points) {
      s += valueFor(p) * Math.exp(-0.5 * ((t - p.t) / sigmaMs) ** 2);
    }
    s *= norm;
    const edge = phi((t1 - t) / sigmaMs) - phi((t0 - t) / sigmaMs);
    if (edge < 0.05) continue;
    out.push({ t, v: (s / edge) * 7 * DAY });
  }
  return out;
}

export function gaussianObservationLine(
  points,
  grid,
  sigmaMs = VOLUME_SMOOTH_SIGMA_DAYS * DAY,
  valueFor = (point) => point.v,
) {
  const observations = points.filter((point) => valueFor(point) != null);
  if (!observations.length) return [];

  return grid
    .map((t) => {
      let sw = 0;
      let swv = 0;
      for (const p of observations) {
        const z = (t - p.t) / sigmaMs;
        if (Math.abs(z) > 4) continue;
        const w = Math.exp(-0.5 * z * z);
        sw += w;
        swv += w * valueFor(p);
      }
      return { t, v: sw ? swv / sw : null };
    })
    .filter((point) => point.v != null);
}

export function gaussianSmoothFields(points, sigmaMs, fields) {
  if (points.length < 2) return points;
  return points.map((p) => {
    const sums = Object.fromEntries(fields.map((field) => [field, 0]));
    let weight = 0;
    for (const q of points) {
      const z = (p.t - q.t) / sigmaMs;
      if (Math.abs(z) > 4) continue;
      const w = Math.exp(-0.5 * z * z);
      for (const field of fields) sums[field] += q[field] * w;
      weight += w;
    }
    return {
      ...p,
      ...Object.fromEntries(
        fields.map((field) => [field, sums[field] / weight]),
      ),
    };
  });
}

function erf(x) {
  const sign = x < 0 ? -1 : 1;
  const a1 = 0.254829592;
  const a2 = -0.284496736;
  const a3 = 1.421413741;
  const a4 = -1.453152027;
  const a5 = 1.061405429;
  const p = 0.3275911;
  const ax = Math.abs(x);
  const t = 1 / (1 + p * ax);
  const y =
    1 - ((((a5 * t + a4) * t + a3) * t + a2) * t + a1) * t * Math.exp(-ax * ax);
  return sign * y;
}

function phi(z) {
  return 0.5 * (1 + erf(z / Math.SQRT2));
}
