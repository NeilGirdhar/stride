// Stride entry point — new 4-tab shell (Running · Fitness · Goals · Settings).
// The old fixed-plan app (main.js + js/views/*) is being replaced pane by pane;
// Running is real, the rest are honest placeholders until we build them.

import { renderRunning } from './views/running.js';
import { renderFitness } from './views/fitness.js';
import { renderGoals } from './views/goals.js';
import { renderSettings } from './views/settings_new.js';

export const MARATHON = new Date(2026, 9, 11); // Sun Oct 11, 2026 — Montreal Beneva

function renderGoalCountdown() {
  const el = document.getElementById('goal-countdown');
  if (!el) return;
  const days = Math.round((MARATHON - new Date()) / 86400000);
  el.innerHTML = days > 0
    ? `<b>${days}</b> days · sub-3 marathon`
    : days === 0 ? '<b>RACE DAY</b>' : 'Marathon done';
}

const RENDER = {
  running: renderRunning,
  fitness: renderFitness,
  goals: renderGoals,
  settings: renderSettings,
};

function show(page) {
  document.querySelectorAll('nav.tabs button').forEach(b =>
    b.classList.toggle('active', b.dataset.page === page));
  document.querySelectorAll('.page').forEach(p =>
    p.classList.toggle('active', p.id === `page-${page}`));
  RENDER[page]?.();
  window.scrollTo(0, 0);
}

document.querySelectorAll('nav.tabs button').forEach(b =>
  b.addEventListener('click', () => show(b.dataset.page)));

renderGoalCountdown();
renderRunning();
setInterval(renderGoalCountdown, 60 * 60 * 1000);
