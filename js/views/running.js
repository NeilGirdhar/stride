// Running pane — historic running graph on real Strava data.
// Dots = individual runs (x=date, y=distance, left axis km). Stacks per day.
// Line = smoothed WEEKLY VOLUME (km/week, right axis) via a Gaussian kernel rate
// estimate with edge renormalization (boundary mass corrected so the ends aren't
// biased low). Horizontally zoomable. Hover a run for stats; click for detail.
// Best-efforts strip pulls records.json; clicking a record frames + highlights it.

const ACTS_URL = './data/strava-activities.json';
const DETAILS_URL = './data/strava-run-details.json';
const GEAR_URL = './data/strava-gear.json';
const RECORDS_URL = './data/records.json';

const RUN_TYPES = new Set(['Run', 'TrailRun']);
const DAY = 86400000;

// viewBox geometry (taller than before so it reads bigger on a phone).
const W = 380, H = 440, padL = 44, padR = 50, padT = 18, padB = 34;
const plotH = H - padT - padB;

let RUNS = null;
let DETAILS = {}, GEAR = {}, RECORDS = {};
let view = 'year';        // week | month | year | all
let plotted = [];         // window points w/ pixel coords for hit-testing
let highlightId = null;

const WINDOWS = {
  week:  { days: 7,   sigmaDays: 2 },
  month: { days: 31,  sigmaDays: 4 },
  year:  { days: 365, sigmaDays: 9 },
  all:   { days: null, sigmaDays: 16 },
};
const REC_ORDER = ['400m', '1k', '5k', '10k', 'half', '30k', 'marathon'];
const REC_LABEL = { '400m': '400m', '1k': '1K', '5k': '5K', '10k': '10K',
                    half: 'Half', '30k': '30K', marathon: 'Marathon' };

export async function renderRunning() {
  const root = document.getElementById('page-running');
  if (!root) return;

  if (RUNS === null) {
    root.innerHTML = `<div class="hero"><div class="eyebrow">Running</div><h1>Loading…</h1></div>`;
    try {
      const [acts, details, gear, records] = await Promise.all([
        fetchJSON(ACTS_URL), fetchJSON(DETAILS_URL, {}),
        fetchJSON(GEAR_URL, {}), fetchJSON(RECORDS_URL, {}),
      ]);
      DETAILS = details || {}; GEAR = gear || {}; RECORDS = records || {};
      RUNS = (acts || [])
        .filter(a => RUN_TYPES.has(a.sport_type || a.type) && a.distance > 0)
        .map(a => ({
          id: a.id,
          name: a.name,
          t: new Date(a.start_date_local || a.start_date).getTime(),
          km: a.distance / 1000,
          paceSec: a.moving_time ? a.moving_time / (a.distance / 1000) : null,
          hr: a.average_heartrate || null,
          cad: a.average_cadence ? Math.round(a.average_cadence * 2) : null,
        }))
        .sort((x, y) => x.t - y.t);
    } catch (e) {
      RUNS = [];
    }
  }

  if (!RUNS.length) {
    root.innerHTML = `<div class="hero"><div class="eyebrow">Running</div><h1>No run data</h1>
      <div class="date">Run <code>python3 scripts/sync_strava.py sync</code> first.</div></div>`;
    return;
  }
  draw(root);
}

async function fetchJSON(url, fallback) {
  try {
    const res = await fetch(`${url}?t=${Date.now()}`);
    if (!res.ok) throw new Error(res.status);
    return await res.json();
  } catch (e) {
    if (fallback !== undefined) return fallback;
    throw e;
  }
}

function draw(root) {
  const now = Date.now();
  const win = WINDOWS[view];
  const start = win.days ? now - win.days * DAY : RUNS[0].t;
  const pts = RUNS.filter(r => r.t >= start && r.t <= now);

  const maxKm = niceTop(Math.max(10, ...pts.map(p => p.km)));
  const x = t => padL + (W - padL - padR) * (t - start) / Math.max(1, now - start);
  const yKm = km => H - padB - plotH * (km / maxKm);

  // ---- smoothed weekly volume (km/week) ----
  const line = weeklyVolumeLine(pts, start, now, win.sigmaDays * DAY);
  const maxWk = niceTop(Math.max(20, ...line.map(p => p.kmwk)));
  const yWk = kmwk => H - padB - plotH * (kmwk / maxWk);
  const linePath = line.length
    ? 'M ' + line.map(p => `${x(p.t).toFixed(1)} ${yWk(p.kmwk).toFixed(1)}`).join(' L ')
    : '';

  // ---- dots ----
  plotted = pts.map(p => ({ ...p, px: x(p.t), py: yKm(p.km) }));
  const dots = plotted.map(p => {
    const hl = p.id === highlightId;
    return `<circle class="rg-dot${hl ? ' hl' : ''}" cx="${p.px.toFixed(1)}" cy="${p.py.toFixed(1)}" r="${hl ? 6 : 3}" />`;
  }).join('');

  // ---- axes ----
  const yL = ticks(maxKm).map(km =>
    `<line class="rg-grid" x1="${padL}" y1="${yKm(km)}" x2="${W - padR}" y2="${yKm(km)}" />
     <text class="rg-ylab" x="${padL - 6}" y="${(yKm(km) + 3).toFixed(1)}">${km}</text>`).join('');
  const yR = ticks(maxWk).map(v =>
    `<text class="rg-ylab rg-wk" x="${W - padR + 6}" y="${(yWk(v) + 3).toFixed(1)}">${v}</text>`).join('');
  const xT = axisDates(start, now).map(t =>
    `<text class="rg-xlab" x="${x(t).toFixed(1)}" y="${H - 10}">${fmtTick(t, now - start)}</text>`).join('');

  const total = pts.reduce((s, p) => s + p.km, 0);
  const longest = pts.reduce((m, p) => Math.max(m, p.km), 0);

  root.innerHTML = `
    <div class="hero">
      <div class="eyebrow">Running</div>
      <h1>Your runs</h1>
      <div class="date">${pts.length} runs · ${total.toFixed(0)} km · longest ${longest.toFixed(1)} km</div>
    </div>

    <div class="rg-controls">
      ${['week', 'month', 'year', 'all'].map(v =>
        `<button class="rg-btn ${v === view ? 'on' : ''}" data-view="${v}">${cap(v)}</button>`).join('')}
    </div>

    <div class="rg-wrap card">
      <svg class="rg" viewBox="0 0 ${W} ${H}" id="rg-svg">
        ${yL}${yR}
        <text class="rg-axis-title" x="${padL - 6}" y="${padT - 4}">km/run</text>
        <text class="rg-axis-title rg-wk" x="${W - padR + 6}" y="${padT - 4}" text-anchor="end">km/wk</text>
        ${linePath ? `<path class="rg-line" d="${linePath}" />` : ''}
        ${dots}
        ${xT}
      </svg>
      <div class="rg-tip" id="rg-tip" hidden></div>
    </div>

    ${bestEffortsStrip()}
  `;

  root.querySelectorAll('.rg-btn').forEach(b =>
    b.addEventListener('click', () => { view = b.dataset.view; highlightId = null; draw(root); }));
  root.querySelectorAll('.rg-rec').forEach(b =>
    b.addEventListener('click', () => showRecord(root, b.dataset.rec)));
  wirePointer(root);
}

// Gaussian kernel weekly-volume rate with edge renormalization.
// rate(t) = [Σ km_i·G_h(t−t_i)] / [Φ((t1−t)/h) − Φ((t0−t)/h)] ; ×7d → km/week.
function weeklyVolumeLine(pts, start, end, h) {
  if (!pts.length) return [];
  const t0 = pts[0].t, t1 = pts[pts.length - 1].t;
  const norm = 1 / (h * Math.sqrt(2 * Math.PI));
  const out = [];
  const N = 160;
  for (let i = 0; i <= N; i++) {
    const t = start + (end - start) * (i / N);
    let s = 0;
    for (const p of pts) s += p.km * Math.exp(-0.5 * ((t - p.t) / h) ** 2);
    s *= norm;
    const edge = Phi((t1 - t) / h) - Phi((t0 - t) / h);
    if (edge < 0.05) continue;            // too far from data — don't draw
    out.push({ t, kmwk: (s / edge) * 7 * DAY });
  }
  return out;
}

function bestEffortsStrip() {
  const present = REC_ORDER.filter(k => RECORDS[k]);
  if (!present.length) {
    return `<div class="rg-note">Best efforts (400m · 1K · 5K · 10K · Half · 30K · Marathon) appear here once
      <code>python3 scripts/sync_strava.py details</code> finishes.</div>`;
  }
  return `
    <div class="rg-recs">
      ${present.map(k => `
        <button class="rg-rec" data-rec="${k}" title="${esc(RECORDS[k].name || '')}">
          <span class="rg-rec-label">${REC_LABEL[k]}</span>
          <span class="rg-rec-time">${RECORDS[k].time}</span>
        </button>`).join('')}
    </div>
    <div class="rg-note">Tap a record to frame and highlight that run.</div>`;
}

function showRecord(root, key) {
  const r = RECORDS[key];
  if (!r) return;
  const t = new Date(r.date).getTime();
  const age = Date.now() - t;
  view = age <= 7 * DAY ? 'week' : age <= 31 * DAY ? 'month' : age <= 365 * DAY ? 'year' : 'all';
  highlightId = r.activity_id;
  draw(root);
  const run = RUNS.find(x => x.id === r.activity_id);
  if (run) openRun(run);
}

// ---- pointer (hover tooltip + click detail) ----

function wirePointer(root) {
  const svg = root.querySelector('#rg-svg');
  const tip = root.querySelector('#rg-tip');
  if (!svg) return;

  const nearest = (clientX) => {
    const rect = svg.getBoundingClientRect();
    const vx = (clientX - rect.left) * (W / rect.width);
    let best = null, bd = Infinity;
    for (const p of plotted) {
      const d = Math.abs(p.px - vx);
      if (d < bd) { bd = d; best = p; }
    }
    return bd <= 22 ? { p: best, rect } : null;
  };

  svg.addEventListener('mousemove', e => {
    const hit = nearest(e.clientX);
    if (!hit) { tip.hidden = true; return; }
    const { p, rect } = hit;
    tip.style.left = `${p.px * (rect.width / W)}px`;
    tip.style.top = `${p.py * (rect.height / H)}px`;
    tip.innerHTML = `
      <div class="rg-tip-name">${esc(p.name)}</div>
      <div class="rg-tip-row"><b>${p.km.toFixed(2)} km</b> · ${fmtDate(p.t)}</div>
      <div class="rg-tip-row">${p.paceSec ? fmtPace(p.paceSec) : '—'}${p.hr ? ` · ${Math.round(p.hr)} bpm` : ''}${p.cad ? ` · ${p.cad} spm` : ''}</div>`;
    tip.hidden = false;
  });
  svg.addEventListener('mouseleave', () => { tip.hidden = true; });
  svg.addEventListener('click', e => {
    const hit = nearest(e.clientX);
    if (hit) openRun(hit.p);
  });
}

// ---- run detail overlay ----

function openRun(run) {
  let el = document.getElementById('rg-modal');
  if (!el) {
    el = document.createElement('div');
    el.id = 'rg-modal';
    el.addEventListener('click', e => { if (e.target.id === 'rg-modal') el.classList.remove('show'); });
    document.body.appendChild(el);
  }
  const d = DETAILS[run.id] || {};
  const shoe = d.gear_id ? GEAR[d.gear_id] : null;
  el.innerHTML = `
    <div class="rg-modal-card">
      <button class="rg-modal-x" aria-label="Close">&times;</button>
      <div class="rg-modal-name">${esc(run.name)}</div>
      <div class="rg-modal-date">${fmtDate(run.t)}</div>
      ${d.photo ? `<img class="rg-modal-photo" src="${esc(d.photo)}" alt="">` : ''}
      <div class="rg-modal-grid">
        <div><b>${run.km.toFixed(2)}</b><span>km</span></div>
        <div><b>${run.paceSec ? fmtPace(run.paceSec).replace('/km', '') : '—'}</b><span>/km</span></div>
        <div><b>${run.hr ? Math.round(run.hr) : '—'}</b><span>bpm</span></div>
        <div><b>${run.cad || '—'}</b><span>spm</span></div>
      </div>
      ${shoe ? `<div class="rg-modal-shoe">👟 ${esc(shoe.name)} · ${shoe.distance_km} km</div>` : ''}
      <a class="rg-modal-link" href="https://www.strava.com/activities/${run.id}" target="_blank" rel="noopener">View on Strava →</a>
    </div>`;
  el.querySelector('.rg-modal-x').addEventListener('click', () => el.classList.remove('show'));
  el.classList.add('show');
}

// ---- math + format helpers ----

function erf(x) {
  const s = x < 0 ? -1 : 1; x = Math.abs(x);
  const t = 1 / (1 + 0.3275911 * x);
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
  return s * y;
}
function Phi(z) { return 0.5 * (1 + erf(z / Math.SQRT2)); }

function niceTop(v) {
  const step = v > 80 ? 20 : v > 40 ? 10 : v > 16 ? 5 : 2;
  return Math.ceil(v / step) * step;
}
function ticks(top) {
  const step = top > 80 ? 20 : top > 40 ? 10 : top > 16 ? 5 : 2;
  const out = [];
  for (let k = step; k <= top; k += step) out.push(k);
  return out;
}
function axisDates(start, end) {
  const out = [];
  for (let i = 1; i < 5; i++) out.push(start + (end - start) * (i / 5));
  return out;
}
function fmtTick(t, span) {
  const d = new Date(t);
  if (span > 200 * DAY) return d.toLocaleDateString(undefined, { month: 'short', year: '2-digit' });
  if (span > 40 * DAY) return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric' });
}
function fmtDate(t) { return new Date(t).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }); }
function fmtPace(s) { return `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}/km`; }
function cap(v) { return v[0].toUpperCase() + v.slice(1); }
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
