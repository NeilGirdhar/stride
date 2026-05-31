# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Stride is a static, client-side PWA for training planning — vanilla JS ES modules, **no backend, no build step, no package manager**. It began as a single-runner half-marathon app ("Sophie", sub-28 5K + HM on Sep 13 2026) and is being rebuilt into a multi-sport, running-first training app. State lives entirely in `localStorage`.

## Running & developing

There is no build, bundler, or test runner. Serve the directory over HTTP (ES modules + `fetch` of `data/*.json` require a real origin, not `file://`):

```sh
python -m http.server 8000   # then open http://localhost:8000
```

- **Keep all imports as bare relative paths** (`./state.js`). The whole app must share **one module graph so there is one `state` singleton**. Never add `?v=` cache-busting query strings to imports — that forks the graph and you get two divergent states.
- Cache freshness in production is handled by a no-cache dev server + bumping the `CACHE` version in the service worker. (Note: `sw.js` is currently referenced in `main.js` but missing from the tree.)

## Architecture

### One state singleton, global re-render
`js/state.js` owns `state`, hydrated once from `localStorage['sl-workout-plan-v4']` via `deepMerge(defaultState, stored)` so new fields in `defaultState` are backward-compatible with old saves. The universal mutation pattern is:

```js
state.foo = bar;        // mutate the singleton directly
save();                 // persist to localStorage
notify();               // pub/sub → main.js re-runs renderAll()
```

`main.js` calls `subscribe(renderAll)`, so any `notify()` re-renders every view. `subscribe`/`notify` is the only reactivity — there is no framework.

### The plan is data; paces resolve at render time
`js/plan.js` exports workout **builder functions** (`easy`, `longRun`, `swim`, `bike`, `brick`, `tempoSession`, `pilates`, `yoga`, `cross`, `restDay`, …) that each return a plain workout object:

```
{ type, title, rationale, totalKm?, sections:[ { name, repeats?, steps:[ {kind:'distance'|'duration', value, unit, zone?, cue?} ] } ] }
```

`buildBasePlan()` assembles `BASE_PLAN` (17 weeks × 7 days) **once at module load** from parallel arrays/maps keyed by week index (`EASY_KM`, `SPEED_WORKOUTS`, `LONG_RUNS`, `PHASES`, `TRI_WEDS`/`TRI_FRIDAYS`/`TRI_SUNDAYS`, `TRI_WEEK_OVERRIDES`). The plan stores **no paces or dates** — only structure.

- `getWeek(wi)` overlays `state.adjustments[wi].days[].replacement` onto the base week (this is how Tune edits a week non-destructively). `getPlan()` maps it over all weeks.
- `js/paces.js` derives Daniels-style zones (`E/M/T/I/R` + `goal`/`race`/`recovery`) from `state.paces.current5k` at render time. `js/format.js` (`stepDisplay`, `sectionDisplay`, `workoutCardHTML`, `sessionTotalMinutes`, `projectedSec`) turns a workout object + live paces into HTML and time/distance estimates.
- `js/dates.js` anchors the calendar: `PLAN_START`, `RACE_DATE`, and `findToday()` → `{weekIdx, dayIdx}` (or `{preplan}`/`{postplan}`). Day index 0 = Monday.

### Views read, never own, the plan
Each `js/views/*.js` renders from `getWeek`/`getPlan` + `state`:
- `today.js` — today's card, week wave-viz (SVG), adjustment banner.
- `plan.js` — all 17 weeks as sunburst tiles → expandable day tiles.
- `stats.js` — derives Sims-style gauges from readiness + paces + completion.
- `tune.js` — the adaptation loop (below).
- `settings.js` — paces, Riegel equivalent-5K from a Strava run, voice, integration tokens.

Writes funnel through `main.js` body-level **click delegation** on `data-*` attributes (`data-toggle`, `data-note`, `data-view`, `data-run`, `data-jump`, `data-goto`) plus `js/modal.js`.

### The adaptation loop (Tune)
`tune.js` gathers feel inputs (free-text → `textToInputs` keyword sentiment, or Oura readiness) and produces a suggestion via **one of two interchangeable backends that emit the same shape**:
- `js/adjust.js` `suggestForCurrentWeek()` — local rules engine (readiness score → load bucket → per-day swaps).
- `js/claude.js` `askClaude()` — real Anthropic API call from the browser (only if `state.claude.apiKey` set), normalized by `claudeResponseToSuggestion()`.

Both return `{ score, bucket, days:[{dayIdx, action, original, replacement, reason}] }`. `applyAdjustment(wi, …)` writes it to `state.adjustments[wi]`; every view then re-reads the adjusted week through `getWeek`. Keep these two backends shape-compatible.

### Integrations (`js/integrations/`)
- **oura.js** — *not* a live API call (Oura V2 has no CORS header). Fetches **baked `data/oura.json`** on boot → `state.oura.lastReadiness`; consumed via `getReadiness()` (falls back to a demo reading if the file is absent).
- **strava.js** — real OAuth, but token exchange goes through a Netlify function at `state.strava.endpoint` (default `/api/strava`). Activities → `state.strava.lastActivities`, used by Today and by Settings' Riegel equivalent-5K.
- **calendar.js** — same baked-JSON pattern: `data/calendar.json` (location segments) + `data/run-clubs.json` → Today location card.
- **claude.js** — direct browser call; **requires** the `anthropic-dangerous-direct-browser-access: true` header.

`data/*.json` files are gitignored/absent in a fresh checkout — all loaders are written to fail soft to demo data.

### Other modules
- `js/ics.js` — exports `BASE_PLAN` to a downloadable `.ics`.
- `js/voice.js` — Web Speech + Wake Lock wrapper for the guided-run view.
- `js/views/run.js` — guided-run engine, **dynamically imported** from `main.js`/`modal.js` via `data-run`. Currently missing from the tree, so "Start guided run" throws.

## Conventions

- HTML is built with template literals; **always run user/dynamic text through `escapeHtml`/`escapeAttr`** (`format.js`, or the local copies in `calendar.js`/`settings.js`).
- View modules guard one-time event wiring with a module-level `wiredOnce` flag and use body-level delegation for elements that re-render.
- When changing the persisted schema, add fields to `defaultState` in `state.js` (merge handles migration); bump `LS_KEY` and add the old key to `LEGACY_KEYS` only for breaking reshapes.

## Rebrand-in-progress notes

The original single user ("Sophie") and the single hardcoded goal are still baked in many places — e.g. `views/stats.js` (`<h1>Sophie</h1>`), `ics.js` `PRODID`, `index.html`/`manifest.json` titles, demo reasoning strings in `adjust.js`/`tune.js`, and the fixed 17-week tri+HM block in `plan.js`/`dates.js`. Treat these as legacy to generalize, not as canonical.
