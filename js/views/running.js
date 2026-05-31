// Running pane — historic running graph on real Strava data.
// Dots = individual runs (x=date, y=distance, left axis km). Stacks per day.
// Line = smoothed WEEKLY VOLUME (km/week, right axis) via a Gaussian kernel rate
// estimate with edge renormalization (boundary mass corrected so the ends aren't
// biased low). Horizontally zoomable. Hover a run for stats; click for detail.
// Best-efforts table pulls records.json; clicking a row frames + highlights it.

const ACTS_URL = './data/strava-activities.json';
const DETAILS_URL = './data/strava-run-details.json';
const GEAR_URL = './data/strava-gear.json';
const RECORDS_URL = './data/records.json';

const RUN_TYPES = new Set(['Run', 'TrailRun']);
const DAY = 86400000;
const SERIOUS_START = new Date(2024, 5, 5).getTime(); // Jun 5, 2024 — first 6am block

// Plot margins (in the same pixel units as the measured svg).
const padL = 48, padR = 56, padT = 22, padB = 34;

let RUNS = null;
let DETAILS = {}, GEAR = {}, RECORDS = {};
let view = 'serious';
let detailTab = 'clubs';
let highlightedRecordKey = null;
let highlightId = null;
let highlightedShoeId = null;
let highlightedClubId = null;

// per-draw state shared with drawGraph / pointer handlers
let currentRoot = null, curStart = 0, curNow = 0, winPts = [], curW = 0, curH = 0;
let plotted = [];
let resizeWired = false, rzT = null;

// Gaussian bandwidth (σ) for the km/week trend line — one value for every zoom.
const SMOOTH_SIGMA_DAYS = 9;
const WINDOWS = {
  d30:     { start: () => Date.now() - 30 * DAY },
  d60:     { start: () => Date.now() - 60 * DAY },
  d90:     { start: () => Date.now() - 90 * DAY },
  year:    { start: () => Date.now() - 365 * DAY },
  serious: { start: () => SERIOUS_START },
  all:     { start: () => RUNS[0].t },
};
const BTN_ORDER = ['d30', 'd60', 'd90', 'year', 'serious', 'all'];
const BTN_LABEL = { d30: '30d', d60: '60d', d90: '90d', year: 'Year', serious: 'Serious', all: 'All' };
const DETAIL_TABS = [
  { id: 'clubs', label: 'Clubs' },
  { id: 'shoes', label: 'Shoes' },
  { id: 'best', label: 'Best' },
];

const REC_ORDER = ['400m', '1k', '5k', '10k', 'half', '30k', 'marathon'];
const REC_LABEL = { '400m': '400 m', '1k': '1 km', '5k': '5 km', '10k': '10 km',
                    half: 'half marathon', '30k': '30 km', marathon: 'marathon' };
const CLUBS = [
  { id: 'mrrc', label: 'MRRC', re: /\bMRRC\b/i },
  { id: 'cose', label: 'Cosé', re: /\bCos[ée]\b/i },
  { id: '6am-mile-end', label: '6am Mile End', re: /\b6\s*am\s+Mile\s+End\b/i },
  { id: '6am-villeray', label: '6am Villeray', re: /\b6\s*am\s+Villeray\b/i },
  { id: '6am-outremont', label: '6am Outremont', re: /\b6\s*am\s+Outrem[eo]nt\b/i },
  { id: '6am-rosemont', label: '6am Rosemont', re: /\b6\s*am\s+Rosemont\b/i },
  { id: '6am-plateau', label: '6am Plateau', re: /\b6\s*am\s+Plateau\b/i },
  { id: '6am-laurier-east', label: '6am Laurier East', re: /\b6\s*am\s+Laurier\s+East\b/i },
];

export async function renderRunning() {
  const root = document.getElementById('page-running');
  if (!root) return;

  if (RUNS === null) {
    root.innerHTML = `<div class="hero"><h1>Loading runs…</h1></div>`;
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
          gearId: DETAILS[a.id]?.gear_id || null,
          club: classifyClub(a.name),
        }))
        .sort((x, y) => x.t - y.t);
    } catch (e) {
      RUNS = [];
    }
  }

  if (!RUNS.length) {
    root.innerHTML = `<div class="hero"><h1>No run data</h1>
      <div class="date">Run <code>python3 scripts/sync_strava.py sync</code> first.</div></div>`;
    return;
  }

  if (!resizeWired) {
    window.addEventListener('resize', () => {
      clearTimeout(rzT);
      rzT = setTimeout(() => {
        const page = document.getElementById('page-running');
        if (currentRoot && page && page.classList.contains('active')) drawGraph(currentRoot);
      }, 150);
    });
    resizeWired = true;
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
  currentRoot = root;
  curNow = Date.now();
  curStart = Math.max(WINDOWS[view].start(), RUNS[0].t);
  winPts = RUNS.filter(r => r.t >= curStart && r.t <= curNow);
  const total = winPts.reduce((s, p) => s + p.km, 0);
  const longest = winPts.reduce((m, p) => Math.max(m, p.km), 0);

  root.innerHTML = `
    <div class="hero">
      <div class="date">${winPts.length} runs · ${total.toFixed(0)} km · longest ${longest.toFixed(1)} km</div>
    </div>

    <div class="rg-shell">
      <div class="rg-controls">
        ${BTN_ORDER.map(v => `<button class="rg-btn ${v === view ? 'on' : ''}" data-view="${v}"${
          v === 'serious' ? ' title="Since start of serious running · Jun 5, 2024"' : ''}>${BTN_LABEL[v]}</button>`).join('')}
      </div>

      <div class="rg-layout">
        <div class="rg-wrap card">
          <div id="rg-graph" class="rg-graph"></div>
          <div class="rg-tip" id="rg-tip" hidden></div>
        </div>

        <aside class="rg-side card">
          <div class="rg-detail-tabs">
            ${DETAIL_TABS.map(tab => `<button class="rg-detail-tab ${tab.id === detailTab ? 'on' : ''}" data-detail="${tab.id}">${tab.label}</button>`).join('')}
          </div>
          <div class="rg-detail-body">${detailPanel()}</div>
        </aside>
      </div>
    </div>
  `;

  root.querySelectorAll('.rg-btn').forEach(b =>
    b.addEventListener('click', () => { view = b.dataset.view; clearHighlights(); draw(root); }));
  root.querySelectorAll('.rg-detail-tab').forEach(b =>
    b.addEventListener('click', () => { detailTab = b.dataset.detail; clearHighlights(); draw(root); }));
  root.querySelectorAll('tr.rg-rec').forEach(tr =>
    tr.addEventListener('click', () => {
      const record = RECORDS[tr.dataset.rec];
      if (!record) return;
      highlightedRecordKey = highlightedRecordKey === tr.dataset.rec ? null : tr.dataset.rec;
      highlightId = highlightedRecordKey ? record.activity_id : null;
      highlightedShoeId = null;
      highlightedClubId = null;
      draw(root);
    }));
  root.querySelectorAll('tr.rg-shoe').forEach(tr =>
    tr.addEventListener('click', () => {
      highlightedShoeId = highlightedShoeId === tr.dataset.gear ? null : tr.dataset.gear;
      highlightId = null;
      highlightedClubId = null;
      draw(root);
    }));
  root.querySelectorAll('tr.rg-club').forEach(tr =>
    tr.addEventListener('click', () => {
      highlightedClubId = highlightedClubId === tr.dataset.club ? null : tr.dataset.club;
      highlightId = null;
      highlightedShoeId = null;
      draw(root);
    }));

  drawGraph(root);
}

function drawGraph(root) {
  const host = root.querySelector('#rg-graph');
  if (!host) return;
  const W = curW = Math.max(300, Math.round(host.clientWidth));
  const H = curH = Math.round(Math.min(560, Math.max(340, window.innerHeight * 0.5)));
  const plotH = H - padT - padB;
  const now = curNow, start = curStart, pts = winPts;

  const maxKm = niceTop(Math.max(10, ...pts.map(p => p.km)));
  const x = t => padL + (W - padL - padR) * (t - start) / Math.max(1, now - start);
  const yKm = km => H - padB - plotH * (km / maxKm);

  const line = weeklyVolumeLine(RUNS, start, now, SMOOTH_SIGMA_DAYS * DAY);
  const maxWk = niceTop(Math.max(20, ...line.map(p => p.kmwk)));
  const yWk = kmwk => H - padB - plotH * (kmwk / maxWk);
  const linePath = line.length
    ? 'M ' + line.map(p => `${x(p.t).toFixed(1)} ${yWk(p.kmwk).toFixed(1)}`).join(' L ')
    : '';

  plotted = pts.map(p => ({ ...p, px: x(p.t), py: yKm(p.km) }));
  const dots = plotted.map(p => {
    const hl = p.id === highlightId;
    const shoeHl = highlightedShoeId && p.gearId === highlightedShoeId;
    const clubHl = highlightedClubId && p.club?.id === highlightedClubId;
    return `<circle class="rg-dot${hl ? ' hl' : ''}${shoeHl ? ' shoe-hl' : ''}${clubHl ? ' club-hl' : ''}" cx="${p.px.toFixed(1)}" cy="${p.py.toFixed(1)}" r="3" />`;
  }).join('');

  const yL = ticks(maxKm).map(km =>
    `<line class="rg-grid" x1="${padL}" y1="${yKm(km)}" x2="${W - padR}" y2="${yKm(km)}" />
     <text class="rg-ylab" x="${padL - 6}" y="${(yKm(km) + 3).toFixed(1)}">${km}</text>`).join('');
  const yR = ticks(maxWk).map(v =>
    `<text class="rg-ylab rg-wk" x="${W - padR + 6}" y="${(yWk(v) + 3).toFixed(1)}">${v}</text>`).join('');
  const xT = axisDates(start, now).map(t =>
    `<text class="rg-xlab" x="${x(t).toFixed(1)}" y="${H - 10}">${fmtTick(t, now - start)}</text>`).join('');

  host.innerHTML = `
    <svg class="rg" id="rg-svg" viewBox="0 0 ${W} ${H}">
      ${yL}${yR}
      <text class="rg-axis-title" x="${padL - 6}" y="${padT - 6}">km/run</text>
      <text class="rg-axis-title rg-wk" x="${W - padR + 6}" y="${padT - 6}" text-anchor="end">km/week</text>
      ${linePath ? `<path class="rg-line" d="${linePath}" />` : ''}
      ${dots}
      ${xT}
    </svg>`;

  wirePointer(root);
}

// Gaussian kernel weekly-volume rate with edge renormalization.
// rate(t) = [Σ km_i·G_h(t−t_i)] / [Φ((t1−t)/h) − Φ((t0−t)/h)] ; ×7d → km/week.
function weeklyVolumeLine(pts, start, end, h) {
  if (!pts.length) return [];
  const t0 = pts[0].t, t1 = pts[pts.length - 1].t;
  const norm = 1 / (h * Math.sqrt(2 * Math.PI));
  const out = [];
  const N = 200;
  for (let i = 0; i <= N; i++) {
    const t = start + (end - start) * (i / N);
    let s = 0;
    for (const p of pts) s += p.km * Math.exp(-0.5 * ((t - p.t) / h) ** 2);
    s *= norm;
    const edge = Phi((t1 - t) / h) - Phi((t0 - t) / h);
    if (edge < 0.05) continue;
    out.push({ t, kmwk: (s / edge) * 7 * DAY });
  }
  return out;
}

function bestEffortsTable() {
  const present = REC_ORDER.filter(k => RECORDS[k]);
  if (!present.length) {
    return `<div class="rg-note">Best efforts (400 m · 1 km · 5 km · 10 km · half · 30 km · marathon) appear here once
      <code>python3 scripts/sync_strava.py details</code> finishes.</div>`;
  }
  return `
    <table class="rg-table">
      <thead><tr><th>distance</th><th>time</th><th>pace</th><th>when</th></tr></thead>
      <tbody>
        ${present.map(k => {
          const r = RECORDS[k];
          return `<tr class="rg-rec${k === highlightedRecordKey ? ' active' : ''}" data-rec="${k}" title="${esc(r.name || '')}">
            <td>${REC_LABEL[k]}</td>
            <td class="rg-rec-time">${r.time}</td>
            <td>${fmtPace(r.sec / recordDistanceKm(k)).replace('/km', '')}</td>
            <td class="rg-rec-when">${fmtDate(new Date(r.date).getTime())}</td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>
    <div class="rg-note">Tap a record to frame and highlight that run.</div>`;
}

function shoesTable() {
  const shoes = new Map();
  for (const run of winPts) {
    if (!run.gearId || !GEAR[run.gearId]) continue;
    const current = shoes.get(run.gearId) || { id: run.gearId, km: 0, runs: 0, gear: GEAR[run.gearId] };
    current.km += run.km;
    current.runs += 1;
    shoes.set(run.gearId, current);
  }
  const rows = [...shoes.values()].sort((a, b) => b.km - a.km);
  if (!rows.length) return '';
  return `
    <table class="rg-table">
      <thead><tr><th>shoe</th><th>visible km</th><th>runs</th></tr></thead>
      <tbody>
        ${rows.map(row => `<tr class="rg-shoe${row.id === highlightedShoeId ? ' active' : ''}" data-gear="${esc(row.id)}" title="${esc(row.gear.name)}">
          <td>${esc(row.gear.name)}${row.gear.retired ? ' <span class="rg-shoe-retired">retired</span>' : ''}</td>
          <td class="rg-rec-time">${row.km.toFixed(1)}</td>
          <td class="rg-rec-when">${row.runs}</td>
        </tr>`).join('')}
      </tbody>
    </table>
    <div class="rg-note">Tap a shoe to highlight matching runs in the graph.</div>`;
}

function clubsTable() {
  const clubs = new Map();
  for (const run of winPts) {
    if (!run.club) continue;
    const current = clubs.get(run.club.id) || { ...run.club, km: 0, runs: 0 };
    current.km += run.km;
    current.runs += 1;
    clubs.set(run.club.id, current);
  }
  const rows = [...clubs.values()].sort((a, b) => b.runs - a.runs || b.km - a.km);
  if (!rows.length) return '';
  return `
    <table class="rg-table">
      <thead><tr><th>club</th><th>visible km</th><th>runs</th></tr></thead>
      <tbody>
        ${rows.map(row => `<tr class="rg-club${row.id === highlightedClubId ? ' active' : ''}" data-club="${esc(row.id)}">
          <td>${esc(row.label)}</td>
          <td class="rg-rec-time">${row.km.toFixed(1)}</td>
          <td class="rg-rec-when">${row.runs}</td>
        </tr>`).join('')}
      </tbody>
    </table>
    <div class="rg-note">Tap a club to highlight matching runs in the graph.</div>`;
}

function detailPanel() {
  if (detailTab === 'shoes') return shoesTable();
  if (detailTab === 'clubs') return clubsTable();
  return bestEffortsTable();
}

function classifyClub(name) {
  return CLUBS.find(club => club.re.test(name || '')) || null;
}

function clearHighlights() {
  highlightId = null;
  highlightedRecordKey = null;
  highlightedShoeId = null;
  highlightedClubId = null;
}

// ---- pointer (hover tooltip + click detail) ----

function wirePointer(root) {
  const svg = root.querySelector('#rg-svg');
  const tip = root.querySelector('#rg-tip');
  if (!svg) return;

  const nearest = (clientX) => {
    const rect = svg.getBoundingClientRect();
    const vx = (clientX - rect.left) * (curW / rect.width);
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
    tip.style.left = `${p.px * (rect.width / curW)}px`;
    tip.style.top = `${p.py * (rect.height / curH)}px`;
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
function recordDistanceKm(key) { return ({ '400m': 0.4, '1k': 1, '5k': 5, '10k': 10, half: 21.0975, '30k': 30, marathon: 42.195 })[key]; }
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
