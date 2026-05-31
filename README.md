# Stride

Stride is a running-training app with three panes:

- **Log** — past runs, with clubs, shoes, and best efforts.
- **Fitness** — how hard to run, based on recent training.
- **Progression** — progress tracked with advanced running metrics.

It's a client-side PWA — vanilla JS ES modules, no build step, no backend.
Training data comes from your Strava history, pulled locally by the bundled
Python tools (`uv run stride-…`).

## Setup

1. **Create Strava API credentials** at <https://www.strava.com/settings/api>
   (any app name; website `http://localhost`; set **Authorization Callback
   Domain** to exactly `localhost`). Note the **Client ID** and **Client Secret**.

2. **Authorize.** `auth` prompts for those two values (saved to
   `data/private/strava-config.json`, gitignored), then opens your browser to
   click *Authorize*:

   ```sh
   uv run stride-sync auth
   ```

## Pull your Strava data

```sh
uv run stride-sync sync       # activities — full history first run, incremental after
uv run stride-sync details    # per-run shoes, photos, best-effort PRs
uv run stride-sync streams    # per-run HR/grade streams for the durability model
```

Every command is resumable and fetches only what's missing, so the first run is
the slow one — `streams` most of all (one request per run, cached in
`data/private/strava-durability-samples.json`); if rate limits pause it, just
re-run. Re-run any command later to pull recent activity, or `stride-sync sync
--full` to ignore the checkpoint and refetch everything.

## Rebuild models

The Progression pane reads two precomputed artifacts rather than fitting them in
the browser: a race model (`data/generated/race-model.json`) and a durability
model (`data/generated/durability-model.json`). Both are offline computations
over already-pulled data, so rebuild them after syncing or editing race labels:

```sh
uv run stride-race-model
uv run stride-durability-model
```

Race labels live in `data/entered/races.json` — add only true efforts (races,
time trials, deliberate benchmarks); ordinary runs stay out and serve only as
training-load features. Rebuild the race model after each new race, and weekly
during training so load factors and predictions stay current.

## Run the app

Serve the folder over HTTP (it's fully static):

```sh
python -m http.server 8000   # then open http://localhost:8000
```

## Project layout

Data is grouped by source:

- `data/imported/` — pulled from Strava by `sync` / `details` / `streams`
- `data/generated/` — reproducible outputs from the model scripts
- `data/entered/` — hand-maintained labels and overrides
- `data/private/` — credentials, tokens, and stream cache (gitignored)

Key files:

- `data/imported/strava-activities.json` — activity list, deduped by id
- `data/imported/strava-run-details.json`, `strava-gear.json` — per-run shoes, photos, gear
- `data/generated/fitness-summary.json`, `records.json` — derived metrics and best-effort PRs
- `data/generated/race-model.json`, `durability-model.json` — the Progression-pane models
- `data/entered/races.json`, `club-overrides.json` — race labels and club overrides
- `data/private/strava-config.json`, `strava-tokens.json`, `strava-durability-samples.json`
  — secrets and the stream cache (never committed)
