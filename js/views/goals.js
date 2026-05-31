// Goals pane — sub-3:00 marathon (Montreal Beneva, Oct 11 2026).
// Familiar layout: one big graph (fixed range Jan 1 → race day) on the left,
// a selectable list of metric sections on the right. Selecting a section swaps
// the main graph to that metric's smooth curve + target.
//
// Every metric is a time-decayed accumulator or trailing aggregate — NOT a max —
// so the curves are smooth and reward frequency + magnitude, not a single best
// run. (A 30 km run twice in a week beats once; "longest run" couldn't see that.)

const ACTS_URL = './data/strava-activities.json';
const RACE_MODEL_URL = './data/race-model.json';
const RUN_TYPES = new Set(['Run', 'TrailRun']);
const DAY = 86400000;
const RANGE_START = new Date(2026, 0, 1).getTime();   // Jan 1, 2026
const MARATHON = new Date(2026, 9, 11).getTime();      // Oct 11, 2026
const MP_SEC = 256;                                    // 4:16/km marathon pace

let RUNS = null;
let RACE_MODEL = null;
let GRID = null;          // daily timestamps RANGE_START .. min(today, race)
let SERIES = {};          // sectionId -> [{ t, v }]
let selected = 'race';
let currentRoot = null, resizeWired = false, rzT = null;

// ---- metric sections ------------------------------------------------------
// compute(runs, grid) -> [{t,v}] (smooth). target(t) -> number. band(t) -> [lo,hi].
// fmt(v) -> label string. higherBetter drives the on-track check.
// Every section carries three target trajectories — A (orange), B (green),
// C (blue) — drawn as dotted diagonals from a higher start to the goal value.
const SECTIONS = [
  {
    id: 'race', label: 'Race prediction', unit: '',
    higherBetter: false, fmt: hms,
    compute: racePrediction,
    targets: [
      { at: rampLine(12600, 10800), tier: 'a' },  // 3:30 → 3:00 (A)
      { at: rampLine(12600, 11400), tier: 'b' },   // 3:30 → 3:10 (B)
      { at: rampLine(12600, 12000), tier: 'c' },    // 3:30 → 3:20 (C)
    ],
  },
  {
    id: 'volume', label: 'Volume', unit: '',
    higherBetter: true, fmt: v => `${v.toFixed(0)} km/wk`,
    compute: (r, g) => ewmaRate(r, g, 9, run => run.km),
    targets: [
      { at: rampTarget(70, 95), tier: 'a' },  // A
      { at: rampTarget(62, 85), tier: 'b' },   // B
      { at: rampTarget(55, 75), tier: 'c' },    // C
    ],
  },
  {
    id: 'durability', label: 'Long-run durability', unit: 'km/week beyond 20 km',
    higherBetter: true, fmt: v => `${v.toFixed(1)} km/wk`,
    compute: (r, g) => ewmaRate(r, g, 20, run => Math.max(0, run.km - 20)),
    targets: [
      { at: rampTarget(8, 20), tier: 'a' },   // A
      { at: rampTarget(7, 16), tier: 'b' },    // B
      { at: rampTarget(6, 12), tier: 'c' },     // C
    ],
  },
  {
    id: 'mp', label: 'Marathon-pace control', unit: 'km/week at marathon pace',
    higherBetter: true, fmt: v => `${v.toFixed(1)} km/wk`,
    compute: (r, g) => ewmaRate(r, g, 14, mpKm),
    targets: [
      { at: rampTarget(4, 20), tier: 'a' },   // A
      { at: rampTarget(3, 14), tier: 'b' },    // B
      { at: rampTarget(2, 8), tier: 'c' },      // C
    ],
  },
  {
    id: 'fitness', label: 'Threshold / race fitness', unit: 'equivalent 5K',
    higherBetter: false, fmt: clock,
    compute: raceFitness,
    targets: [
      { at: rampLine(1380, 1200), tier: 'a' },  // 23:00 → 20:00 (A)
      { at: rampLine(1380, 1260), tier: 'b' },   // → 21:00 (B)
      { at: rampLine(1380, 1320), tier: 'c' },    // → 22:00 (C)
    ],
  },
  {
    id: 'recovery', label: 'Recovery / injury resistance', unit: 'distance/heartbeat',
    higherBetter: true, fmt: v => `${v.toFixed(1)} m/beat`,
    compute: recoveryEff,
    targets: [
      { at: t => recoveryBaseline() * (1 + 0.10 * frac(t)), tier: 'a' },  // A
      { at: t => recoveryBaseline() * (1 + 0.06 * frac(t)), tier: 'b' },   // B
      { at: t => recoveryBaseline() * (1 + 0.02 * frac(t)), tier: 'c' },    // C
    ],
  },
];

export async function renderGoals() {
  const root = document.getElementById('page-goals');
  if (!root) return;

  if (RUNS === null) {
    root.innerHTML = `<div class="hero"><div class="date">Loading…</div></div>`;
    try {
      const [acts, raceModel] = await Promise.all([
        fetchJSON(ACTS_URL),
        fetchJSON(RACE_MODEL_URL).catch(() => null),
      ]);
      RACE_MODEL = raceModel;
      RUNS = normalizeRuns(acts || []);
      GRID = buildGrid();
      for (const s of SECTIONS) SERIES[s.id] = s.compute(RUNS, GRID);
    } catch (e) {
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
      <div class="date">Montreal Beneva · sub-3:00 · ${days} days · 4:16/km</div>
    </div>

    <section class="rg-shell">
      <div class="rg-layout">
        <div class="rg-wrap card"><div id="goal-graph" class="rg-graph"></div></div>
        <aside class="rg-side card">
          ${focusBox()}
          ${SECTIONS.map(sectionRow).join('')}
        </aside>
      </div>
    </section>`;

  root.querySelectorAll('.goal-row').forEach(el =>
    el.addEventListener('click', () => { selected = el.dataset.id; renderGoals(); }));

  if (!resizeWired) {
    window.addEventListener('resize', () => {
      clearTimeout(rzT);
      rzT = setTimeout(() => {
        const page = document.getElementById('page-goals');
        if (currentRoot && page && page.classList.contains('active')) drawGoalChart(currentRoot);
      }, 150);
    });
    resizeWired = true;
  }
  drawGoalChart(root);
}

// Single biggest actionable weakness right now (race prediction is an outcome,
// not a thing to train, so it's excluded). Relative gap to today's A-goal.
const FOCUS_MSG = {
  volume: pct => `<b>Volume</b> is your biggest gap (~${pct}% under target). Add easy km — raise weekly volume before piling on intensity.`,
  durability: pct => `<b>Long-run durability</b> is lagging (~${pct}% under). Get a longer long run in this week — and repeat it, don't rely on one.`,
  mp: pct => `<b>Marathon-pace control</b> is the gap (~${pct}% under). Put a 4:16/km block inside your next long run.`,
  fitness: pct => `<b>Race fitness</b> is behind (~${pct}% off pace). Do a threshold session or a short time trial to move it.`,
  recovery: pct => `<b>Recovery</b> is slipping (~${pct}% off). Keep easy runs easy and protect sleep before adding load.`,
};

function focusBox() {
  let worst = null, worstGap = -Infinity;
  for (const s of SECTIONS) {
    if (s.id === 'race') continue;
    const series = SERIES[s.id];
    if (!series.length) continue;
    const cur = series[series.length - 1].v;
    const tgt = s.targets[0].at(Date.now());
    if (tgt == null) continue;
    const gap = s.higherBetter ? (tgt - cur) / tgt : (cur - tgt) / tgt;
    if (gap > worstGap) { worstGap = gap; worst = s; }
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
  const tgt = s.targets[0].at(Date.now());
  const ok = cur != null && tgt != null &&
    (s.higherBetter ? cur >= tgt * 0.92 : cur <= tgt * 1.05);
  return `
    <button class="goal-row ${s.id === selected ? 'on' : ''}" data-id="${s.id}">
      <span class="goal-dot ${ok ? 'ok' : ''}"></span>
      <span class="goal-row-main">
        <span class="goal-row-label">${s.label}</span>
        ${s.unit ? `<span class="goal-row-sub">${s.unit}</span>` : ''}
      </span>
      <span class="goal-row-val">${cur == null ? '—' : s.fmt(cur)}</span>
    </button>`;
}

function drawGoalChart(root) {
  const host = root.querySelector('#goal-graph');
  if (!host) return;
  const s = SECTIONS.find(x => x.id === selected);
  host.innerHTML = chart(s, Math.max(300, Math.round(host.clientWidth)));
}

// ---- chart ----------------------------------------------------------------

function chart(s, W) {
  const H = Math.round(Math.min(560, Math.max(340, window.innerHeight * 0.5)));
  const padL = 52, padR = 16, padT = 18, padB = 28;
  const pts = SERIES[s.id];
  const now = Math.min(Date.now(), MARATHON);

  // sample targets across the full range for the y-domain + drawing
  const tgtSamples = [];
  for (let t = RANGE_START; t <= MARATHON; t += 7 * DAY) {
    for (const tg of s.targets) tgtSamples.push(tg.at(t));
  }

  const vals = pts.map(p => p.v).concat(tgtSamples);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.12 || 1;
  lo -= pad; hi += pad;

  const x = t => padL + (W - padL - padR) * (t - RANGE_START) / (MARATHON - RANGE_START);
  const y = v => H - padB - (H - padT - padB) * (v - lo) / (hi - lo);
  const line = ps => 'M ' + ps.map(p => `${x(p.t).toFixed(1)} ${y(p.v).toFixed(1)}`).join(' L ');

  // target trajectories (dotted diagonals), one per A/B/C tier
  const targetPaths = s.targets.map(tg => {
    const tp = [];
    for (let t = RANGE_START; t <= MARATHON; t += 3.5 * DAY) tp.push({ t, v: tg.at(t) });
    return `<path class="goal-target tier-${tg.tier}" d="${line(tp)}" />`;
  }).join('');

  // legend: colour + each tier's race-day goal value, in the metric's own units
  const legend = `<div class="goal-legend">${s.targets.map(tg =>
    `<span><i class="goal-leg-dot tier-${tg.tier}"></i>${s.fmt(tg.at(MARATHON))}</span>`).join('')}</div>`;

  // y ticks
  const ticks = niceTicks(lo, hi, 5).map(v =>
    `<line class="goal-grid" x1="${padL}" x2="${W - padR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" />
     <text class="goal-ylab" x="${padL - 8}" y="${(y(v) + 3).toFixed(1)}">${s.fmt(v)}</text>`).join('');

  // month ticks
  const months = [];
  for (let m = 0; m <= 9; m++) {
    const t = new Date(2026, m, 1).getTime();
    if (t < RANGE_START || t > MARATHON) continue;
    months.push(`<text class="goal-xlab" x="${x(t).toFixed(1)}" y="${H - 8}">${new Date(t).toLocaleDateString(undefined, { month: 'short' })}</text>`);
  }

  const todayX = x(now);
  return `
    <svg class="goal-svg" viewBox="0 0 ${W} ${H}">
      ${ticks}${targetPaths}
      <line class="goal-today" x1="${todayX.toFixed(1)}" x2="${todayX.toFixed(1)}" y1="${padT}" y2="${H - padB}" />
      <path class="goal-line" d="${line(pts)}" />
      ${months.join('')}
    </svg>
    ${legend}`;
}

// ---- metric computations --------------------------------------------------

function buildGrid() {
  const end = Math.min(Date.now(), MARATHON);
  const grid = [];
  for (let t = RANGE_START; t <= end; t += DAY) grid.push(t);
  return grid;
}

// Causal EWMA rate (per-week). Smooth, frequency-aware: each run adds
// contrib/τ, the pool decays with τ. Sampled daily, ×7 → weekly units.
function ewmaRate(runs, grid, tau, contrib) {
  let v = 0, lastT = grid[0], ri = 0;
  const out = [];
  const advance = t => { v *= Math.exp(-(t - lastT) / DAY / tau); lastT = t; };
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

// MP-weighted km for one run: full credit near 4:16/km, ramped up by length
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
  const raw = grid.map(t => {
    const eqs = runs
      .filter(r => r.km >= 3 && r.t <= t && r.t >= t - win)
      .map(r => r.movingTime * (5 / r.km) ** 1.06)
      .sort((a, b) => a - b);
    if (!eqs.length) return { t, v: null };
    const k = Math.min(3, eqs.length);
    return { t, v: eqs.slice(0, k).reduce((a, b) => a + b, 0) / k };
  });
  return fillAndSmooth(raw, 7);
}

// Prefer the supervised offline artifact. Fallback keeps the static app usable
// before scripts/race_model.py has been run.
function racePrediction(runs, grid) {
  const modeled = raceModelSeries('marathon');
  if (modeled.length) return modeled;
  return raceFitness(runs, grid).map(p => ({ t: p.t, v: p.v * (42.195 / 5) ** 1.06 }));
}

function raceModelSeries(key) {
  const rows = RACE_MODEL?.series?.[key] || [];
  return rows
    .map(p => ({
      t: new Date(`${p.date}T00:00:00`).getTime(),
      v: p.time_sec,
    }))
    .filter(p => Number.isFinite(p.t) && Number.isFinite(p.v) && p.t >= RANGE_START && p.t <= MARATHON)
    .sort((a, b) => a.t - b.t);
}

// Aerobic efficiency on easy runs: metres per heartbeat (×1000), Gaussian
// smoothed. Pace-based (note: GAP would be better, but Neil rarely runs hills).
function recoveryEff(runs, grid) {
  const easy = runs.filter(r => r.hr && r.paceSec > 300); // slower than 5:00/km
  const sigma = 16 * DAY;
  const raw = grid.map(t => {
    let sw = 0, swv = 0;
    for (const r of easy) {
      const z = (t - r.t) / sigma;
      if (Math.abs(z) > 4) continue;
      const w = Math.exp(-0.5 * z * z);
      sw += w; swv += w * (1e6 / (r.paceSec * r.hr));   // (m/s)/hr ×1000
    }
    return { t, v: sw ? swv / sw : null };
  });
  return fillForward(raw);
}

// ---- targets --------------------------------------------------------------

// 0→1 fraction of the way from Jan 1 to race day.
function frac(t) { return clamp((t - RANGE_START) / (MARATHON - RANGE_START), 0, 1); }
// Straight diagonal from startVal (Jan 1) to endVal (race day).
function rampLine(startVal, endVal) { return t => startVal + (endVal - startVal) * frac(t); }

function rampTarget(startVal, endVal) {
  const t0 = new Date(2026, 5, 1).getTime();  // June 1
  const t1 = new Date(2026, 8, 7).getTime();  // Sep 7 (peak)
  return t => {
    const f = clamp((t - t0) / (t1 - t0), 0, 1);
    return startVal + (endVal - startVal) * f;
  };
}

function recoveryBaseline() {
  // "stay at or above" the Jan–Feb easy-efficiency baseline.
  const s = SERIES.recovery || [];
  const early = s.filter(p => p.v != null && p.t < new Date(2026, 2, 1).getTime());
  if (!early.length) return s.find(p => p.v != null)?.v ?? 0;
  return median(early.map(p => p.v));
}

// ---- helpers --------------------------------------------------------------

function normalizeRuns(acts) {
  return acts
    .filter(a => RUN_TYPES.has(a.sport_type || a.type) && a.distance > 0 && a.moving_time)
    .map(a => ({
      t: new Date(a.start_date_local || a.start_date).getTime(),
      km: a.distance / 1000,
      movingTime: a.moving_time,
      paceSec: a.moving_time / (a.distance / 1000),
      hr: a.average_heartrate || null,
    }))
    .sort((x, y) => x.t - y.t);
}

function fillForward(raw) {
  let last = null;
  return raw.map(p => (p.v == null ? { t: p.t, v: last } : (last = p.v, p)))
    .filter(p => p.v != null);
}

function fillAndSmooth(raw, sigmaDays) {
  const filled = fillForward(raw);
  const sigma = sigmaDays * DAY;
  return filled.map(p => {
    let sw = 0, swv = 0;
    for (const q of filled) {
      const z = (p.t - q.t) / sigma;
      if (Math.abs(z) > 4) continue;
      const w = Math.exp(-0.5 * z * z);
      sw += w; swv += w * q.v;
    }
    return { t: p.t, v: swv / sw };
  });
}

function niceTicks(lo, hi, n) {
  const raw = (hi - lo) / n;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(s => s * mag).find(s => s >= raw) || mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
  return out;
}

function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
function median(xs) { const s = [...xs].sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; }
function clock(sec) { const m = Math.floor(sec / 60), s = Math.round(sec % 60); return `${m}:${String(s).padStart(2, '0')}`; }
function hms(sec) { const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.round(sec % 60); return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`; }

async function fetchJSON(url) {
  const res = await fetch(`${url}?t=${Date.now()}`);
  if (!res.ok) throw new Error(res.status);
  return res.json();
}
