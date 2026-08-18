# Repository instructions

## Current system

Stride is a static, single-runner Strava dashboard plus offline Python data and
model commands. Read `README.md` for operation and `ARCHITECTURE.md` for the
technical design. Do not infer architecture from old planner code or history.

The browser has three views: Log (`js/views/running.js`), Fitness
(`js/views/fitness.js`), and Progression (`js/views/goals.js`). There is no
runtime backend, framework, bundler, `localStorage` state, or service worker.

## Preserve data and secrets

- Imported and generated JSON is committed user data. Preserve unrelated changes
  and never discard or regenerate it unless the user asks.
- `data/private/` contains secrets and a large stream cache. It is gitignored;
  never expose or commit its values.
- Inspect large JSON artifacts through targeted `jq` queries rather than dumping
  them in full.
- Generated model files may legitimately lag the activity import. Check their
  `generated_at` and `as_of` fields before treating results as current.

## Pipeline

The refresh order is:

```sh
uv run stride-sync sync
uv run stride-sync details
uv run stride-sync streams
uv run stride-race-model
uv run stride-durability-model
```

The last two steps are explicit and are not triggered by sync. `details` and
`streams` are resumable and accept `--limit=N`; they make external Strava
requests and write local data, so do not run them merely to verify a code change.

## Important semantics

- Runs include `Run` and `TrailRun` unless a specific existing computation says
  otherwise.
- Fitness/fatigue are causal exponential load models with 42-/7-day constants;
  Gaussian smoothing is only for displayed curves and descriptive progression
  rates.
- The race model's named economy features are not identical to the Progression
  pane metrics. In particular, model `anaerobic_power` is best 60-second
  grade-adjusted speed, while the UI uses high-HR metres per heartbeat. See
  `ARCHITECTURE.md` before changing either.
- Treat the race model as small-sample, runner-specific estimation. Its RMSE is
  in-sample, and future series hold current fitness constant.
- Escape dynamic text inserted into HTML.

## Development

Python targets 3.14 and uses `uv`; frontend code is native ES modules with no
build step. Serve the repository over HTTP for browser testing.

Use the narrowest relevant checks:

```sh
npm test
npx eslint .
uv run ruff check .
uv run ty check
uv lock --check
git diff --check
```

There is currently no behavioral JavaScript suite or Python `tests/` directory.
Do not claim browser behavior or model output was verified unless it was actually
run with the required data.
