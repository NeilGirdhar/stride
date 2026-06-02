// Progression pane.
// Familiar layout: one big graph (fixed range start → race day) on the left,
// a selectable list of metric sections on the right. Selecting a section swaps
// the main graph to that metric's smooth curve + target.
//
// Every metric is a time-decayed accumulator or trailing aggregate — NOT a max —
// so the curves are smooth and reward frequency + magnitude, not a single best
// run. (A 30 km run twice in a week beats once; "longest run" couldn't see that.)

import { REC_KM, REC_LABEL, REC_ORDER } from "../lib/records.js";
import {
  DAY,
  parseLocalDate,
  rangeButtonsHTML,
  rangeStart,
} from "../lib/ranges.js";
import {
  gaussianObservationLine,
  gaussianRateLine,
  VOLUME_SMOOTH_SIGMA_DAYS,
} from "../lib/smoothing.js";
import { timeAxisLabels } from "../lib/time-axis.js";

const ACTS_URL = "./data/imported/strava-activities.json";
const DETAILS_URL = "./data/imported/strava-run-details.json";
const RACE_MODEL_URL = "./data/generated/race-model.json";
const DURABILITY_MODEL_URL = "./data/generated/durability-model.json";
const MARATHON_GOAL_URL = "./data/entered/marathon-goal.json";
const RECORDS_URL = "./data/generated/records.json";
const TRAINING_CONFIG_URL = "./data/entered/training-config.json";
const ZONE_METRICS_URL = "./data/generated/zone-metrics.json";
const RUN_TYPES = new Set(["Run", "TrailRun"]);
// Per-run grade-adjusted m/beat by HR zone (aerobic efficiency / power, anaerobic
// power), computed offline from stream segments by `stride-sync streams`.
let ZONE_METRICS = {};

let RUNS = null;
let DETAILS = {};
let RACE_MODEL = null;
let DURABILITY_MODEL = null;
let GOAL = null;
let RECORDS = {};
let RANGE_START = null;
let MARATHON = null;
let MP_SEC = null;
let RACE_DISTANCE_KM = null;
let TARGET_RAMP_START = null;
let TARGET_RAMP_PEAK = null;
let RECOVERY_BASELINE_END = null;
let SERIOUS_START = null;
let SECTIONS = [];
let GRID = null; // daily timestamps first run .. min(today, race)
let SERIES = {}; // sectionId -> [{ t, v }]
let progressionTab = "fitness";
let selectedFitness = "volume";
let selectedPrediction = "5k";
let view = "serious";
let goalOn = true;
let currentRoot = null,
  resizeWired = false,
  rzT = null;

const PROGRESSION_TABS = [
  { id: "fitness", label: "Fitness" },
  { id: "prediction", label: "Prediction" },
];
export async function renderGoals() {
  const root = document.getElementById("page-goals");
  if (!root) return;

  if (RUNS === null) {
    root.innerHTML = `<div class="hero"><div class="date">Loading…</div></div>`;
    try {
      const [
        acts,
        details,
        raceModel,
        durabilityModel,
        goal,
        records,
        trainingConfig,
        zoneMetrics,
      ] = await Promise.all([
        fetchJSON(ACTS_URL),
        fetchJSON(DETAILS_URL).catch(() => ({})),
        fetchJSON(RACE_MODEL_URL).catch(() => null),
        fetchJSON(DURABILITY_MODEL_URL).catch(() => null),
        fetchJSON(MARATHON_GOAL_URL),
        fetchJSON(RECORDS_URL).catch(() => ({})),
        fetchJSON(TRAINING_CONFIG_URL).catch(() => null),
        fetchJSON(ZONE_METRICS_URL).catch(() => ({})),
      ]);
      loadGoal(goal);
      DETAILS = details || {};
      RACE_MODEL = raceModel;
      DURABILITY_MODEL = durabilityModel;
      addGeneratedSections();
      RECORDS = records || {};
      SERIOUS_START = trainingConfig?.serious_start
        ? parseLocalDate(trainingConfig.serious_start)
        : RANGE_START;
      ZONE_METRICS = zoneMetrics || {};
      RUNS = normalizeRuns(acts || []);
      GRID = buildGrid();
      for (const s of SECTIONS) SERIES[s.id] = s.compute(RUNS, GRID);
    } catch {
      RUNS = [];
    }
  }
  if (!RUNS.length) {
    root.innerHTML = `<div class="hero"><div class="date">Run <code>python scripts/sync_strava.py sync</code> first.</div></div>`;
    return;
  }

  currentRoot = root;
  const days = Math.max(0, Math.round((MARATHON - Date.now()) / DAY));
  root.innerHTML = `
    <div class="hero">
      <div class="date">${esc(GOAL.name)} · ${esc(GOAL.summary_goal)} · ${days} days · ${paceLabel(MP_SEC)}</div>
    </div>

    <section class="rg-shell">
      <div class="rg-controls goal-controls">
        ${rangeButtonsHTML({ active: view, seriousStart: SERIOUS_START })}
        <button class="rg-btn goal-toggle ${goalOn ? "on" : ""}" data-goal="toggle">Goal</button>
      </div>
      <div class="rg-layout">
        <div class="rg-wrap card"><div id="goal-graph" class="rg-graph"></div></div>
        <aside class="rg-side card">
          <div class="rg-detail-tabs goal-tabs">
            ${PROGRESSION_TABS.map((tab) => `<button class="rg-detail-tab ${tab.id === progressionTab ? "on" : ""}" data-progression-tab="${tab.id}">${tab.label}</button>`).join("")}
          </div>
          <div class="rg-detail-body">${progressionPanel()}</div>
        </aside>
      </div>
    </section>`;

  root.querySelectorAll(".rg-btn[data-view]").forEach((b) =>
    b.addEventListener("click", () => {
      view = b.dataset.view;
      renderGoals();
    }),
  );
  root.querySelector(".goal-toggle")?.addEventListener("click", () => {
    goalOn = !goalOn;
    renderGoals();
  });
  root.querySelectorAll(".rg-detail-tab").forEach((b) =>
    b.addEventListener("click", () => {
      progressionTab = b.dataset.progressionTab;
      renderGoals();
    }),
  );
  root.querySelectorAll(".goal-row, .goal-row-table").forEach((el) =>
    el.addEventListener("click", () => {
      if (progressionTab === "prediction") selectedPrediction = el.dataset.id;
      else selectedFitness = el.dataset.id;
      renderGoals();
    }),
  );

  if (!resizeWired) {
    window.addEventListener("resize", () => {
      clearTimeout(rzT);
      rzT = setTimeout(() => {
        const page = document.getElementById("page-goals");
        if (currentRoot && page && page.classList.contains("active"))
          drawGoalChart(currentRoot);
      }, 150);
    });
    resizeWired = true;
  }
  drawGoalChart(root);
}

// Single biggest actionable weakness right now (race prediction is an outcome,
// not a thing to train, so it's excluded). Relative gap to today's A-goal.
const FOCUS_MSG = {
  volume: (pct) =>
    `<b>Load tolerance</b> is your biggest gap (~${pct}% under target). Add easy km — raise weekly volume before piling on intensity.`,
  recovery: (pct) =>
    `<b>Aerobic efficiency</b> is slipping (~${pct}% off). Keep easy runs steady in high zone 2.`,
  aerobic_power: (pct) =>
    `<b>Aerobic power</b> is slipping (~${pct}% off). Keep steady aerobic work controlled in high zone 3.`,
  anaerobic_power: (pct) =>
    `<b>Anaerobic power</b> is your biggest gap (~${pct}% off). Add short hard efforts above 85% HR (high Z4/Z5).`,
};

function focusBox() {
  let worst = null,
    worstGap = -Infinity;
  for (const s of SECTIONS) {
    if (s.id === "race") continue;
    const series = SERIES[s.id];
    if (!series.length) continue;
    if (!s.targets.length) continue;
    const cur = series[series.length - 1].v;
    const tgt = s.targets[0].at(Date.now());
    if (tgt == null) continue;
    const gap = s.higherBetter ? (tgt - cur) / tgt : (cur - tgt) / tgt;
    if (gap > worstGap) {
      worstGap = gap;
      worst = s;
    }
  }
  if (!worst || worstGap < 0.03) {
    return `<div class="goal-focus on-track">
      <div class="goal-focus-label">Focus</div>
      <div class="goal-focus-msg">On track across the board — hold the routine.</div></div>`;
  }
  return `<div class="goal-focus">
    <div class="goal-focus-label">Focus now</div>
    <div class="goal-focus-msg">${FOCUS_MSG[worst.id](Math.round(worstGap * 100))}</div></div>`;
}

function sectionRow(s) {
  const series = SERIES[s.id];
  const cur = series.length ? series[series.length - 1].v : null;
  const tgt = s.targets[0]?.at(Date.now());
  const ok =
    cur != null &&
    tgt != null &&
    (s.higherBetter ? cur >= tgt * 0.92 : cur <= tgt * 1.05);
  return `
    <button class="goal-row ${s.id === selectedFitness ? "on" : ""}" data-id="${s.id}">
      <span class="goal-dot ${ok ? "ok" : ""}"></span>
      <span class="goal-row-main">
        <span class="goal-row-label">${s.label}</span>
        ${s.unit ? `<span class="goal-row-sub">${s.unit}</span>` : ""}
      </span>
      <span class="goal-row-val">${cur == null ? "—" : s.fmt(cur)}</span>
    </button>`;
}

function progressionPanel() {
  if (progressionTab === "prediction") return predictionRows();
  return `${focusBox()}${fitnessSections().map(sectionRow).join("")}`;
}

function fitnessSections() {
  return SECTIONS.filter((s) => s.id !== "race");
}

function predictionRows() {
  const rows = predictionKeys();
  if (!rows.length) {
    return `<div class="rg-note">Predictions appear here once race-model data is generated.</div>`;
  }
  if (!rows.includes(selectedPrediction)) selectedPrediction = rows[0];
  return `
    <table class="rg-table goal-predictions">
      <thead><tr><th>distance</th><th>prediction</th><th>best</th></tr></thead>
      <tbody>
        ${rows
          .map((key) => {
            const predicted = currentPrediction(key);
            const record = RECORDS[key];
            return `<tr class="goal-row-table ${key === selectedPrediction ? "active" : ""}" data-id="${key}">
              <td>${REC_LABEL[key]}</td>
              <td class="rg-rec-time">${predicted == null ? "—" : timeLabel(predicted)}</td>
              <td class="rg-rec-when">${record?.time || "—"}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`;
}

function drawGoalChart(root) {
  const host = root.querySelector("#goal-graph");
  if (!host) return;
  const s = selectedSection();
  host.innerHTML = chart(s, Math.max(300, Math.round(host.clientWidth)));
}

function selectedSection() {
  const section =
    progressionTab === "prediction"
      ? predictionSection(selectedPrediction)
      : SECTIONS.find((x) => x.id === selectedFitness) || fitnessSections()[0];
  return { ...section, targets: goalOn ? section.targets : [] };
}

function predictionKeys() {
  const modeled = new Set(Object.keys(RACE_MODEL?.series || {}));
  return REC_ORDER.filter((key) => RECORDS[key] || modeled.has(key));
}

function predictionSection(key) {
  const series = predictionSeries(key);
  const raceSection = SECTIONS.find((s) => s.id === "race");
  return {
    id: `prediction-${key}`,
    label: REC_LABEL[key],
    higherBetter: false,
    fmt: timeLabel,
    targets: key === "marathon" && raceSection ? raceSection.targets : [],
    directSeries: series,
  };
}

function predictionSeries(key) {
  const modeled = raceModelSeries(key);
  if (modeled.length) return modeled;
  const base5k = raceModelSeries("5k");
  const base = base5k.length ? base5k : raceFitness(RUNS, GRID);
  const km = REC_KM[key];
  if (!km) return [];
  return base.map((p) => ({ t: p.t, v: p.v * (km / 5) ** 1.06 }));
}

function currentPrediction(key) {
  const prediction = RACE_MODEL?.current_predictions?.[key]?.time_sec;
  if (Number.isFinite(prediction)) return prediction;
  const series = predictionSeries(key);
  return series.length ? series[series.length - 1].v : null;
}

// ---- chart ----------------------------------------------------------------

function chart(s, W) {
  const H = Math.round(Math.min(560, Math.max(340, window.innerHeight * 0.5)));
  const padL = 52,
    padR = 16,
    padT = 18,
    padB = 28;
  const sourcePts = s.directSeries || SERIES[s.id] || [];
  const end = goalOn ? MARATHON : Math.min(Date.now(), MARATHON);
  // The x-axis spans the selected window over all run history (GRID[0] = first
  // run), not just where this metric has data. The line below simply cuts out
  // where the metric is empty, so widening to "All" expands the axis regardless.
  const allStart = GRID?.[0] ?? sourcePts[0]?.t ?? RANGE_START;
  const start = Math.max(
    allStart,
    rangeStart(view, {
      seriousStart: SERIOUS_START,
      allStart,
      fallbackStart: RANGE_START,
    }),
  );
  const pts = sourcePts.filter((p) => p.t >= start && p.t <= end);
  const now = Math.min(Date.now(), end);

  // sample targets across the full range for the y-domain + drawing
  const tgtSamples = [];
  for (let t = start; t <= end; t += 7 * DAY) {
    for (const tg of s.targets) tgtSamples.push(tg.at(t));
  }

  const vals = pts.map((p) => p.v).concat(tgtSamples);
  if (!vals.length) vals.push(0, 1);
  let lo = Math.min(...vals),
    hi = Math.max(...vals);
  const pad = (hi - lo) * 0.12 || 1;
  lo -= pad;
  hi += pad;

  const x = (t) =>
    padL + ((W - padL - padR) * (t - start)) / Math.max(1, end - start);
  const y = (v) => H - padB - ((H - padT - padB) * (v - lo)) / (hi - lo);
  const line = (ps) =>
    "M " +
    ps.map((p) => `${x(p.t).toFixed(1)} ${y(p.v).toFixed(1)}`).join(" L ");

  // target trajectories (dotted diagonals), one per A/B/C tier
  const targetPaths = s.targets
    .map((tg) => {
      const tp = [];
      for (let t = start; t <= end; t += 3.5 * DAY) tp.push({ t, v: tg.at(t) });
      return `<path class="goal-target tier-${tg.tier}" d="${line(tp)}" />`;
    })
    .join("");

  // legend: colour + each tier's race-day goal value, in the metric's own units
  const legend = s.targets.length
    ? `<div class="goal-legend">${s.targets
        .map(
          (tg) =>
            `<span><i class="goal-leg-dot tier-${tg.tier}"></i>${s.fmt(tg.at(MARATHON))}</span>`,
        )
        .join("")}</div>`
    : "";

  // y ticks
  const ticks = niceTicks(lo, hi, 5)
    .map(
      (v) =>
        `<line class="goal-grid" x1="${padL}" x2="${W - padR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" />
     <text class="goal-ylab" x="${padL - 8}" y="${(y(v) + 3).toFixed(1)}">${s.fmt(v)}</text>`,
    )
    .join("");

  const xTicks = timeAxisLabels(start, end, W, { padL, padR });

  const todayX = x(now);
  return `
    <svg class="goal-svg" viewBox="0 0 ${W} ${H}">
      ${ticks}${targetPaths}
      ${now >= start && now <= end ? `<line class="goal-today" x1="${todayX.toFixed(1)}" x2="${todayX.toFixed(1)}" y1="${padT}" y2="${H - padB}" />` : ""}
      ${pts.length ? `<path class="goal-line" d="${line(pts)}" />` : ""}
      ${xTicks
        .map(
          (tick) =>
            `<text class="goal-xlab" x="${x(tick.t).toFixed(1)}" y="${H - 8}" text-anchor="${tick.anchor}">${tick.label}</text>`,
        )
        .join("")}
    </svg>
    ${legend}`;
}

// ---- metric computations --------------------------------------------------

function buildGrid() {
  const end = Math.min(Date.now(), MARATHON);
  const start = RUNS[0]?.t ?? RANGE_START;
  const grid = [];
  for (let t = start; t <= end; t += DAY) grid.push(t);
  return grid;
}

// Causal EWMA rate (per-week). Smooth, frequency-aware: each run adds
// contrib/τ, the pool decays with τ. Sampled daily, ×7 → weekly units.
function ewmaRate(runs, grid, tau, contrib) {
  let v = 0,
    lastT = grid[0],
    ri = 0;
  const out = [];
  const advance = (t) => {
    v *= Math.exp(-(t - lastT) / DAY / tau);
    lastT = t;
  };
  for (const t of grid) {
    while (ri < runs.length && runs[ri].t <= t) {
      advance(runs[ri].t);
      v += contrib(runs[ri]) / tau;
      ri += 1;
    }
    advance(t);
    out.push({ t, v: v * 7 });
  }
  return out;
}

function gaussianWeeklyRate(runs, grid, valueFor) {
  return gaussianRateLine(
    runs,
    grid[0],
    grid[grid.length - 1],
    VOLUME_SMOOTH_SIGMA_DAYS * DAY,
    valueFor,
    grid.length - 1,
  );
}

// MP-weighted km for one run: full credit near configured pace, ramped up by length
// (MP control is about holding pace in *long* efforts). Proxy — without splits
// it misses MP blocks buried inside slower-average runs; streams would fix it.
function mpKm(run) {
  const wPace = Math.exp(-0.5 * ((run.paceSec - MP_SEC) / 12) ** 2);
  const wLong = clamp((run.km - 12) / 4, 0, 1);
  return run.km * wPace * wLong;
}

// Race fitness: avg of the top-3 Riegel-5Ks in a trailing 35 d, then Gaussian
// smoothed. Several quality runs move it; one fluke can't.
function raceFitness(runs, grid) {
  const win = 35 * DAY;
  const raw = grid.map((t) => {
    const eqs = runs
      .filter((r) => r.km >= 3 && r.t <= t && r.t >= t - win)
      .map((r) => r.movingTime * (5 / r.km) ** 1.06)
      .sort((a, b) => a - b);
    if (!eqs.length) return { t, v: null };
    const k = Math.min(3, eqs.length);
    return { t, v: eqs.slice(0, k).reduce((a, b) => a + b, 0) / k };
  });
  const filled = fillForward(raw);
  return gaussianObservationLine(
    filled,
    filled.map((p) => p.t),
    7 * DAY,
  );
}

// Prefer the supervised offline artifact. Fallback keeps the static app usable
// before scripts/race_model.py has been run.
function racePrediction(runs, grid) {
  const modeled = raceModelSeries("marathon");
  if (modeled.length) return modeled;
  return raceFitness(runs, grid).map((p) => ({
    t: p.t,
    v: p.v * (RACE_DISTANCE_KM / 5) ** 1.06,
  }));
}

function raceModelSeries(key) {
  const rows = RACE_MODEL?.series?.[key] || [];
  return rows
    .map((p) => ({
      t: new Date(`${p.date}T00:00:00`).getTime(),
      v: p.time_sec,
    }))
    .filter(
      (p) =>
        Number.isFinite(p.t) &&
        Number.isFinite(p.v) &&
        p.t >= RANGE_START &&
        p.t <= MARATHON,
    )
    .sort((a, b) => a.t - b.t);
}

// Grade-adjusted metres per heartbeat in an HR zone — precomputed per run from the
// stream segments (sustained in-band blocks) and smoothed across runs here.
// Per-run economy has ~10% day-to-day variance and only a few qualifying runs land
// per week, so this needs a wider window than volume to average the noise out.
const ECONOMY_SMOOTH_SIGMA_DAYS = 28;
function zoneEconomy(runs, grid, valueFor) {
  return gaussianObservationLine(
    runs,
    grid,
    ECONOMY_SMOOTH_SIGMA_DAYS * DAY,
    valueFor,
  );
}

function recoveryEff(runs, grid) {
  return zoneEconomy(runs, grid, (r) => r.aerobicEfficiency);
}

function aerobicPower(runs, grid) {
  return zoneEconomy(runs, grid, (r) => r.aerobicPower);
}

function anaerobicPower(runs, grid) {
  return zoneEconomy(runs, grid, (r) => r.anaerobicPower);
}

function caeDurability() {
  const series = (DURABILITY_MODEL?.series || [])
    .map((point) => ({
      t: parseLocalDate(point.date),
      v: Number(point.retained),
    }))
    .filter((point) => Number.isFinite(point.t) && Number.isFinite(point.v))
    .sort((a, b) => a.t - b.t);
  const current = Number(DURABILITY_MODEL?.summary?.durability_retained);
  if (Number.isFinite(current) && !series.length) {
    return [{ t: Math.min(Date.now(), MARATHON), v: current }];
  }
  if (
    Number.isFinite(current) &&
    series.length &&
    series[series.length - 1].t < Date.now() - DAY
  ) {
    series.push({ t: Math.min(Date.now(), MARATHON), v: current });
  }
  return series;
}

// ---- targets --------------------------------------------------------------

// 0→1 fraction of the way from range start to race day.
function frac(t) {
  return clamp((t - RANGE_START) / (MARATHON - RANGE_START), 0, 1);
}
// Straight diagonal from startVal at range start to endVal on race day.
function rampLine(startVal, endVal) {
  return (t) => startVal + (endVal - startVal) * frac(t);
}

function rampTarget(startVal, endVal) {
  return (t) => {
    const f = clamp(
      (t - TARGET_RAMP_START) / (TARGET_RAMP_PEAK - TARGET_RAMP_START),
      0,
      1,
    );
    return startVal + (endVal - startVal) * f;
  };
}

function recoveryBaseline(sectionId = "recovery") {
  const s = SERIES[sectionId] || [];
  const early = s.filter((p) => p.v != null && p.t < RECOVERY_BASELINE_END);
  if (!early.length) return s.find((p) => p.v != null)?.v ?? 0;
  return median(early.map((p) => p.v));
}

// ---- helpers --------------------------------------------------------------

function loadGoal(goal) {
  GOAL = goal;
  RANGE_START = parseLocalDate(goal.range_start);
  MARATHON = parseLocalDate(goal.race_date);
  MP_SEC = Number(goal.marathon_pace_sec);
  RACE_DISTANCE_KM = Number(goal.race_distance_km);
  TARGET_RAMP_START = parseLocalDate(goal.target_ramp.start);
  TARGET_RAMP_PEAK = parseLocalDate(goal.target_ramp.peak);
  RECOVERY_BASELINE_END = parseLocalDate(goal.recovery_baseline_end);
  SECTIONS = goal.sections.map(buildSection);
}

function buildSection(section) {
  return {
    id: section.id,
    label: section.label,
    unit: section.unit,
    higherBetter: section.higher_better,
    fmt: formatterFor(section.id),
    compute: computeFor(section),
    targets: section.targets.map((target) => buildTarget(target, section.id)),
  };
}

function formatterFor(id) {
  if (id === "race") return hms;
  if (id === "fitness") return clock;
  if (id === "anaerobic_power") return (v) => `${v.toFixed(2)} m/beat`;
  if (id === "cae_durability") return (v) => `${(v * 100).toFixed(0)}%`;
  if (id === "volume") return (v) => `${v.toFixed(0)} km/wk`;
  if (id === "recovery" || id === "aerobic_power")
    return (v) => `${v.toFixed(2)} m/beat`;
  return (v) => `${v.toFixed(1)} km/wk`;
}

function computeFor(section) {
  if (section.compute === "race_prediction") return racePrediction;
  if (section.compute === "race_fitness") return raceFitness;
  if (section.compute === "recovery_efficiency") return recoveryEff;
  if (section.compute === "aerobic_power") return aerobicPower;
  if (section.compute === "anaerobic_power") return anaerobicPower;
  if (section.compute === "cae_durability") return caeDurability;
  if (section.compute === "ewma_marathon_pace") {
    return (runs, grid) => ewmaRate(runs, grid, section.tau_days, mpKm);
  }
  if (section.compute === "ewma_km_beyond") {
    return (runs, grid) =>
      ewmaRate(runs, grid, section.tau_days, (run) =>
        Math.max(0, run.km - section.threshold_km),
      );
  }
  if (section.compute === "ewma_grade_adjusted_km") {
    return (runs, grid) =>
      gaussianWeeklyRate(runs, grid, (run) => run.gradeAdjustedKm);
  }
  return (runs, grid) =>
    ewmaRate(runs, grid, section.tau_days, (run) => run.km);
}

function addGeneratedSections() {
  if (!DURABILITY_MODEL?.summary) return;
  const section = buildSection({
    id: "cae_durability",
    label: "Durability",
    unit: "Share of fresh grade-adjusted metres per heartbeat still held deep into a run (after 4000 CAE of cumulative load).",
    higher_better: true,
    compute: "cae_durability",
    targets: [],
  });
  const existing = SECTIONS.findIndex((s) => s.id === section.id);
  if (existing >= 0) {
    SECTIONS[existing] = section;
    return;
  }
  const volumeIndex = SECTIONS.findIndex((s) => s.id === "volume");
  SECTIONS.splice(
    volumeIndex >= 0 ? volumeIndex + 1 : SECTIONS.length,
    0,
    section,
  );
}

function buildTarget(target, sectionId) {
  if (target.type === "line") {
    return { tier: target.tier, at: rampLine(target.start, target.end) };
  }
  if (target.type === "ramp") {
    return { tier: target.tier, at: rampTarget(target.start, target.end) };
  }
  // goal_ramp: a diagonal from the metric's measured baseline up to `end` on race
  // day, so the slope is the improvement actually required from here. Baseline is
  // read lazily — SERIES isn't built yet when targets are constructed.
  return {
    tier: target.tier,
    at: (t) => {
      const base = recoveryBaseline(sectionId);
      return base + (target.end - base) * frac(t);
    },
  };
}

function normalizeRuns(acts) {
  return acts
    .filter(
      (a) =>
        RUN_TYPES.has(a.sport_type || a.type) &&
        a.distance > 0 &&
        a.moving_time,
    )
    .map((a) => {
      const zone = ZONE_METRICS[String(a.id)] || {};
      return {
        t: new Date(a.start_date_local || a.start_date).getTime(),
        id: a.id,
        km: a.distance / 1000,
        gradeAdjustedKm:
          finite(detail(a.id, "grade_adjusted_distance_km")) ??
          a.distance / 1000,
        movingTime: a.moving_time,
        paceSec: a.moving_time / (a.distance / 1000),
        hr: a.average_heartrate || null,
        aerobicEfficiency: finite(zone.aerobic_efficiency),
        aerobicPower: finite(zone.aerobic_power),
        anaerobicPower: finite(zone.anaerobic_power),
      };
    })
    .sort((x, y) => x.t - y.t);
}

function detail(activityId, key) {
  return DETAILS[String(activityId)]?.[key];
}

function finite(value) {
  return Number.isFinite(value) ? value : null;
}

function fillForward(raw) {
  let last = null;
  return raw
    .map((p) => (p.v == null ? { t: p.t, v: last } : ((last = p.v), p)))
    .filter((p) => p.v != null);
}

function niceTicks(lo, hi, n) {
  const raw = (hi - lo) / n;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step =
    [1, 2, 2.5, 5, 10].map((s) => s * mag).find((s) => s >= raw) || mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
  return out;
}

function clamp(v, a, b) {
  return Math.max(a, Math.min(b, v));
}
function median(xs) {
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
function clock(sec) {
  const m = Math.floor(sec / 60),
    s = Math.round(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}
function timeLabel(sec) {
  return sec >= 3600 ? hms(sec) : clock(sec);
}
function hms(sec) {
  const h = Math.floor(sec / 3600),
    m = Math.floor((sec % 3600) / 60),
    s = Math.round(sec % 60);
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
function paceLabel(sec) {
  return `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, "0")}/km`;
}
function esc(s) {
  return String(s).replace(
    /[&<>"']/g,
    (c) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[c],
  );
}

async function fetchJSON(url) {
  const res = await fetch(`${url}?t=${Date.now()}`);
  if (!res.ok) throw new Error(res.status);
  return res.json();
}
