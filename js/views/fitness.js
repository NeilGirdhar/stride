// Fitness pane — running-only load model.
// Fitness is slow-decaying load, fatigue is fast-decaying load, and form is the
// difference between them. Values are shown as weekly km-equivalent load.

const ACTS_URL = "./data/imported/strava-activities.json";
const TRAINING_CONFIG_URL = "./data/entered/training-config.json";
const RUN_TYPES = new Set(["Run", "TrailRun"]);
const DAY = 86400000;
const FITNESS_TAU = 42;
const FATIGUE_TAU = 7;
const FITNESS_MOMENTUM_TAU = 14;
const FATIGUE_MOMENTUM_TAU = 5;
// Gaussian display smooth (σ, symmetric/non-causal) — purely cosmetic, and
// distinct from the model's exponential time constants (τ) above.
const CHART_SMOOTH_SIGMA_DAYS = 2;
const WINDOWS = {
  d30: { start: () => Date.now() - 30 * DAY },
  d60: { start: () => Date.now() - 60 * DAY },
  d90: { start: () => Date.now() - 90 * DAY },
  year: { start: () => Date.now() - 365 * DAY },
  serious: { start: () => SERIOUS_START },
  all: { start: () => RUNS[0].t },
};
const BTN_ORDER = ["d30", "d60", "d90", "year", "serious", "all"];
const BTN_LABEL = {
  d30: "30d",
  d60: "60d",
  d90: "90d",
  year: "Year",
  serious: "Serious",
  all: "All",
};

let RUNS = null;
let MODEL = null;
let SERIOUS_START = null;
let view = "serious";
let currentRoot = null;
let resizeWired = false,
  rzT = null;

export async function renderFitness() {
  const root = document.getElementById("page-fitness");
  if (!root) return;

  if (RUNS === null) {
    root.innerHTML = `<div class="hero"><div class="date">Loading runs…</div></div>`;
    try {
      const [acts, trainingConfig] = await Promise.all([
        fetchJSON(ACTS_URL),
        fetchJSON(TRAINING_CONFIG_URL),
      ]);
      SERIOUS_START = parseLocalDate(trainingConfig.serious_start);
      RUNS = normalizeRuns(acts || []);
      MODEL = buildModel(RUNS);
    } catch {
      RUNS = [];
    }
  }

  if (!RUNS.length) {
    root.innerHTML = `<div class="hero">
      <div class="date">Run <code>python scripts/sync_strava.py sync</code> first.</div></div>`;
    return;
  }

  currentRoot = root;
  const today = MODEL[MODEL.length - 1];
  const recentRuns = RUNS.filter((r) => r.t >= Date.now() - 7 * DAY);
  const recentKm = recentRuns.reduce((sum, r) => sum + r.km, 0);
  const signal = runSignal(today);

  root.innerHTML = `
    <div class="hero">
      <div class="date">${fmtDate(today.t)} · ${recentKm.toFixed(1)} km last 7 days</div>
    </div>

    <section class="rg-shell">
      <div class="rg-controls">
        ${BTN_ORDER.map(
          (v) =>
            `<button class="rg-btn ${v === view ? "on" : ""}" data-view="${v}"${
              v === "serious"
                ? ` title="Since start of serious running · ${fmtDate(SERIOUS_START)}"`
                : ""
            }>${BTN_LABEL[v]}</button>`,
        ).join("")}
      </div>

      <div class="rg-layout">
        <div class="rg-wrap fit-chart card">
          <div id="fit-graph" class="fit-graph"></div>
          <div class="fit-legend">
            <span><i class="fit-key fit-fitness"></i>Fitness</span>
            <span><i class="fit-key fit-fatigue"></i>Fatigue</span>
            <span><i class="fit-key fit-form"></i>Form</span>
          </div>
        </div>

        <aside class="rg-side fit-side card">
          <div class="fit-score-main">
            <div class="fit-score-num">${signal.score}</div>
            <div>
              <div class="fit-score-label">${signal.label}</div>
              <div class="fit-score-sub">${signal.sub}</div>
            </div>
          </div>
          <div class="fit-metrics">
            ${metricCard("Form (mildly encourages running)", today.form, "fit-form")}
            ${metricCard("Fitness momentum (encourages running)", today.fitnessMomentum, "fit-fitness")}
            ${metricCard("Fatigue momentum (discourages running)", today.fatigueMomentum, "fit-fatigue")}
          </div>
        </aside>
      </div>
    </section>`;

  root.querySelectorAll(".rg-btn").forEach((b) =>
    b.addEventListener("click", () => {
      view = b.dataset.view;
      renderFitness();
    }),
  );

  if (!resizeWired) {
    window.addEventListener("resize", () => {
      clearTimeout(rzT);
      rzT = setTimeout(() => {
        const page = document.getElementById("page-fitness");
        if (currentRoot && page && page.classList.contains("active"))
          drawFitnessChart(currentRoot);
      }, 150);
    });
    resizeWired = true;
  }
  drawFitnessChart(root);
}

async function fetchJSON(url) {
  const res = await fetch(`${url}?t=${Date.now()}`);
  if (!res.ok) throw new Error(res.status);
  return res.json();
}

function parseLocalDate(value) {
  const [year, month, day] = String(value).split("-").map(Number);
  return new Date(year, month - 1, day).getTime();
}

function normalizeRuns(acts) {
  const runs = acts
    .filter(
      (a) =>
        RUN_TYPES.has(a.sport_type || a.type) &&
        a.distance > 0 &&
        a.moving_time,
    )
    .map((a) => ({
      t: new Date(a.start_date_local || a.start_date).getTime(),
      km: a.distance / 1000,
      movingTime: a.moving_time,
      elevation: a.total_elevation_gain || 0,
    }))
    .sort((a, b) => a.t - b.t);
  const baseline = median(runs.map((r) => r.movingTime / r.km).filter(Boolean));
  return runs.map((r) => ({ ...r, load: runLoad(r, baseline) }));
}

function runLoad(run, baselinePaceSec) {
  const paceSec = run.movingTime / run.km;
  const paceBoost = baselinePaceSec
    ? clamp((baselinePaceSec - paceSec) / baselinePaceSec, -0.12, 0.22)
    : 0;
  const elevationKm = run.elevation / 100;
  return (run.km + elevationKm) * (1 + paceBoost);
}

function buildModel(runs) {
  const first = dayStart(runs[0].t);
  const now = Date.now();
  const times = [];
  for (let t = first; t <= now; t += DAY) times.push(t);
  if (times[times.length - 1] !== now) times.push(now);
  const states = statesAtTimes(runs, times);
  let prevFitness = 0;
  let prevFatigue = 0;
  let fitnessMomentum = 0;
  let fatigueMomentum = 0;

  return states.map((state, i) => {
    const dtDays = i ? Math.max(0, (state.t - states[i - 1].t) / DAY) : 1;
    const fitnessAlpha = 1 - Math.exp(-dtDays / FITNESS_MOMENTUM_TAU);
    const fatigueAlpha = 1 - Math.exp(-dtDays / FATIGUE_MOMENTUM_TAU);
    fitnessMomentum +=
      (state.fitness - prevFitness - fitnessMomentum) * fitnessAlpha;
    fatigueMomentum +=
      (state.fatigue - prevFatigue - fatigueMomentum) * fatigueAlpha;
    prevFitness = state.fitness;
    prevFatigue = state.fatigue;
    return {
      ...state,
      fitnessMomentum,
      fatigueMomentum,
    };
  });
}

function runSignal(today) {
  const raw =
    50 +
    today.form * 1.4 +
    today.fitnessMomentum * 4 -
    today.fatigueMomentum * 7;
  const score = Math.round(clamp(raw, 0, 100));
  if (score < 25) return { score, label: "Rest", sub: "Absorb load" };
  if (score < 50) return { score, label: "Jog", sub: "Short and easy" };
  if (score < 75) return { score, label: "Run", sub: "Normal aerobic work" };
  return { score, label: "Push", sub: "Good room for stress" };
}

function drawFitnessChart(root) {
  const host = root.querySelector("#fit-graph");
  if (!host) return;
  const start = Math.max(WINDOWS[view].start(), MODEL[0].t);
  const end = MODEL[MODEL.length - 1].t;
  const points = smoothChartPoints(
    chartPoints(RUNS, start, end),
    CHART_SMOOTH_SIGMA_DAYS * DAY,
  );
  if (points.length < 2) {
    host.innerHTML = fitnessChart(
      smoothChartPoints(MODEL, CHART_SMOOTH_SIGMA_DAYS * DAY),
      Math.max(300, Math.round(host.clientWidth)),
    );
    return;
  }
  host.innerHTML = fitnessChart(
    points,
    Math.max(300, Math.round(host.clientWidth)),
  );
}

function chartPoints(runs, start, end) {
  const step = chartStep(end - start);
  const times = [start];
  for (let t = start + step; t < end; t += step) times.push(t);
  for (const run of runs) {
    if (run.t < start || run.t > end) continue;
    times.push(Math.max(start, run.t - 1), run.t);
  }
  times.push(end);
  return statesAtTimes(
    runs,
    [...new Set(times)].sort((a, b) => a - b),
  );
}

function chartStep(span) {
  if (span <= 7 * DAY) return 30 * 60 * 1000;
  if (span <= 31 * DAY) return 2 * 60 * 60 * 1000;
  if (span <= 365 * DAY) return 12 * 60 * 60 * 1000;
  return DAY;
}

function statesAtTimes(runs, times) {
  const out = [];
  let fitness = 0;
  let fatigue = 0;
  let lastT = runs[0]?.t || times[0];
  let runIdx = 0;

  function decayTo(t) {
    const dtDays = Math.max(0, (t - lastT) / DAY);
    fitness *= Math.exp(-dtDays / FITNESS_TAU);
    fatigue *= Math.exp(-dtDays / FATIGUE_TAU);
    lastT = t;
  }

  for (const t of times) {
    while (runIdx < runs.length && runs[runIdx].t <= t) {
      decayTo(runs[runIdx].t);
      fitness += runs[runIdx].load / FITNESS_TAU;
      fatigue += runs[runIdx].load / FATIGUE_TAU;
      runIdx += 1;
    }
    decayTo(t);
    const fitnessWeekly = fitness * 7;
    const fatigueWeekly = fatigue * 7;
    out.push({
      t,
      fitness: fitnessWeekly,
      fatigue: fatigueWeekly,
      form: fitnessWeekly - fatigueWeekly,
    });
  }
  return out;
}

function smoothChartPoints(points, sigmaMs) {
  if (points.length < 2) return points;
  return points.map((p) => {
    const sums = { fitness: 0, fatigue: 0, form: 0, weight: 0 };
    for (const q of points) {
      const z = (p.t - q.t) / sigmaMs;
      if (Math.abs(z) > 4) continue;
      const w = Math.exp(-0.5 * z * z);
      sums.fitness += q.fitness * w;
      sums.fatigue += q.fatigue * w;
      sums.form += q.form * w;
      sums.weight += w;
    }
    return {
      ...p,
      fitness: sums.fitness / sums.weight,
      fatigue: sums.fatigue / sums.weight,
      form: sums.form / sums.weight,
    };
  });
}

function fitnessChart(points, W) {
  const H = Math.round(Math.min(560, Math.max(340, window.innerHeight * 0.5)));
  const padL = 44,
    padR = 16,
    padT = 18,
    padB = 28;
  const vals = points.flatMap((p) => [p.fitness, p.fatigue, p.form]);
  const lo = Math.min(-10, ...vals);
  const hi = Math.max(20, ...vals);
  const x = (t) =>
    padL +
    ((W - padL - padR) * (t - points[0].t)) /
      Math.max(1, points[points.length - 1].t - points[0].t);
  const y = (v) =>
    H - padB - ((H - padT - padB) * (v - lo)) / Math.max(1, hi - lo);
  const path = (key) =>
    "M " +
    points
      .map((p) => `${x(p.t).toFixed(1)} ${y(p[key]).toFixed(1)}`)
      .join(" L ");
  const zero = y(0);
  const ticks = axisTicks(lo, hi);
  return `
    <svg class="fit-svg" viewBox="0 0 ${W} ${H}">
      ${ticks
        .map(
          (
            v,
          ) => `<line class="fit-grid" x1="${padL}" x2="${W - padR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" />
        <text class="fit-ylab" x="${padL - 8}" y="${(y(v) + 3).toFixed(1)}">${v}</text>`,
        )
        .join("")}
      <line class="fit-zero" x1="${padL}" x2="${W - padR}" y1="${zero.toFixed(1)}" y2="${zero.toFixed(1)}" />
      <path class="fit-line fit-fatigue" d="${path("fatigue")}" />
      <path class="fit-line fit-form" d="${path("form")}" />
      <path class="fit-line fit-fitness" d="${path("fitness")}" />
      <text class="fit-xlab" x="${padL}" y="${H - 6}">${fmtShort(points[0].t)}</text>
      <text class="fit-xlab" x="${W - padR}" y="${H - 6}" text-anchor="end">${fmtShort(points[points.length - 1].t)}</text>
    </svg>`;
}

function metricCard(label, value, cls) {
  return `
    <div class="fit-metric card">
      <div class="fit-metric-label">${label}</div>
      <div class="fit-metric-value ${cls}">${value.toFixed(1)}</div>
    </div>`;
}

function axisTicks(lo, hi) {
  const step = hi - lo > 80 ? 20 : 10;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
  return out;
}

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function dayStart(t) {
  const d = new Date(t);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}
function fmtDate(t) {
  return new Date(t).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
function fmtShort(t) {
  return new Date(t).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}
