# Stride

A running-first, multi-sport training app — a static, client-side PWA (vanilla
JS ES modules, no build step, no backend, state-free). Three panes:

- **Log** — your full running history: distance over time with a smoothed
  weekly-volume trend, per-run details, shoes, and best efforts.
- **Fitness** — a running Fitness & Form (PMC-style) model from per-run load.
- **Goals** — progress toward a sub-3:00 marathon across six tracked buckets
  (volume, long-run durability, marathon-pace control, race fitness, recovery,
  and the resulting race prediction).

Colour follows the viewer's OS light/dark setting (Catppuccin Latte / Mocha).
Training data is your own Strava history, pulled locally by the script below.

## Run the app

It's fully static — serve the folder over HTTP (ES modules + `fetch` of the
data files need a real origin, not `file://`):

```sh
python -m http.server 8000   # then open http://localhost:8000
```

## Sync your Strava data

`stride-sync` pulls your full Strava history (all sports) once, then
incrementally fetches new activities. Python 3 standard library only — no
third-party packages. Data is grouped by source:

- `data/imported/` — Strava data downloaded by `sync` / `details`
- `data/generated/` — reproducible outputs from local scripts
- `data/entered/` — manually maintained labels and overrides
- `data/private/` — OAuth tokens and other local secrets (gitignored)

Key files:

- `data/imported/strava-activities.json` — raw activity list, deduped by id
- `data/imported/strava-run-details.json`, `data/imported/strava-gear.json`
  — per-run shoes, photos, and gear details
- `data/generated/fitness-summary.json`, `data/generated/records.json`
  — compact derived metrics and best-effort PRs
- `data/entered/races.json`, `data/entered/club-overrides.json`
  — manually entered race labels and club overrides
- `data/private/strava-config.json`, `data/private/strava-tokens.json`
  — API credentials and OAuth tokens (gitignored, never committed)

### One-time setup

1. **Get Strava API credentials** at <https://www.strava.com/settings/api>. If
   you already have an app, note its **Client ID** and **Client Secret**; if
   not, create one (any name; website can be `http://localhost`). Set the
   **Authorization Callback Domain** to exactly `localhost`.

2. **Authorize.** `auth` prompts for your Client ID and Secret (saved to
   `data/private/strava-config.json`, gitignored), then opens your browser to
   click *Authorize*:

   ```sh
   uv run stride-sync auth
   ```

### Pulling data

```sh
uv run stride-sync sync            # new activities + recompute summary
uv run stride-sync sync --full     # ignore checkpoint, refetch all
uv run stride-sync details         # backfill shoes + best-effort PRs (resumable)
uv run stride-durability-model     # build stream-based durability metric
```

The first `sync` does a full history pull; later runs are incremental. Re-run
`sync` (and occasionally `details`) to pull in recent activities.

## Update race predictions

The Goals pane reads a precomputed supervised race model from
`data/generated/race-model.json`. The browser does not fit the model at runtime;
rebuild the JSON after syncing Strava or editing race labels:

```sh
uv run stride-sync sync
uv run stride-race-model
uv run stride-durability-model
```

Race labels live in `data/entered/races.json`. Add only true performance labels
there: races, time trials, or deliberate benchmark efforts. Ordinary easy,
workout, and long runs should stay out of the registry; they are used only as
training-load features for the model.

Update the model:

- after each race or time trial, by adding/checking the race row and running
  `uv run stride-race-model`
- weekly during training, after syncing Strava, so current load factors and
  predictions stay fresh
- before relying on the Goals race prediction if recent activities have not yet
  been synced and rebuilt
