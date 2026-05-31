// Stride entry point — 3-tab shell (Running · Fitness · Goals).
// Running is real; Fitness and Goals are honest placeholders until we build them.

import { renderRunning } from './views/running.js';
import { renderFitness } from './views/fitness.js';
import { renderGoals } from './views/goals.js';

export const MARATHON = new Date(2026, 9, 11); // Sun Oct 11, 2026 — Montreal Beneva

const RENDER = {
  running: renderRunning,
  fitness: renderFitness,
  goals: renderGoals,
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

renderRunning();
