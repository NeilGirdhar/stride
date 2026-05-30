// Fitness pane — placeholder. Will become a running-only Fitness & Freshness
// (PMC-style) chart: Fitness = slow-decaying load, Fatigue = fast-decaying load,
// Form = Fitness − Fatigue. Model to be co-developed; needs a per-run load proxy.

export function renderFitness() {
  const root = document.getElementById('page-fitness');
  if (!root) return;
  root.innerHTML = `
    <div class="hero">
      <h1>Fitness &amp; Freshness</h1>
      <div class="date">Running-only load model — coming next.</div>
    </div>
    <div class="card stub-card">
      <p>This will chart, from your runs:</p>
      <ul>
        <li><b>Fitness</b> — slow-decaying training load (rises as you run).</li>
        <li><b>Fatigue</b> — fast-decaying load (decays quicker).</li>
        <li><b>Form</b> — Fitness − Fatigue (are you fresh or buried?).</li>
      </ul>
      <p class="stub-meta">We still need to choose the per-run load measure (HR- or pace-based) and the decay constants. That's the "make it smarter" conversation.</p>
    </div>`;
}
