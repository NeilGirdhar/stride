// Goals pane — placeholder. Will compute the next long-run distance adaptively
// from recent runs + the marathon goal + the week's planned 6am runs (including
// run-commute to the farther ones). Needs Neil's weekly schedule first.

export function renderGoals() {
  const root = document.getElementById('page-goals');
  if (!root) return;
  root.innerHTML = `
    <div class="hero">
      <div class="date">Montreal Beneva · Oct 11, 2026 · 4:16/km</div>
    </div>
    <div class="card stub-card">
      <p>This pane will pick your <b>next long run</b> — usually the coming Sat/Sun, date editable — from:</p>
      <ul>
        <li>your recent running volume,</li>
        <li>the rest of this week's 6am runs (incl. running <em>to</em> the farther ones),</li>
        <li>and what the marathon plan needs.</li>
      </ul>
      <p class="stub-meta">To build the engine I still need your normal week: which days you run at 6am and how far, which you run to (and the extra km), your usual long-run day, and your recent longest run.</p>
    </div>`;
}
