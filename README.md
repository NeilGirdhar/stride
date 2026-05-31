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

`scripts/sync_strava.py` pulls your full Strava history (all sports) once, then
incrementally fetches new activities. Python 3 standard library only — no
third-party packages. It writes under `data/`:

- `data/strava-activities.json` — raw activity list, deduped by id
- `data/fitness-summary.json` — compact derived metrics
- `data/strava-run-details.json`, `data/strava-gear.json`, `data/records.json`
  — per-run shoes + best-effort PRs (written by `details`)
- `data/strava-tokens.json` — OAuth tokens (gitignored, never committed)

### One-time setup

1. **Get Strava API credentials** at <https://www.strava.com/settings/api>. If
   you already have an app, note its **Client ID** and **Client Secret**; if
   not, create one (any name; website can be `http://localhost`). Set the
   **Authorization Callback Domain** to exactly `localhost`.

2. **Add your credentials** (kept out of git):

   ```sh
   cp scripts/strava_config.example.json scripts/strava_config.json
   # then edit it and fill in client_id + client_secret
   ```

3. **Authorize** (opens your browser; click *Authorize*):

   ```sh
   python scripts/sync_strava.py auth
   ```

### Pulling data

```sh
python scripts/sync_strava.py sync            # new activities + recompute summary
python scripts/sync_strava.py sync --full     # ignore checkpoint, refetch all
python scripts/sync_strava.py details         # backfill shoes + best-effort PRs (resumable)
```

The first `sync` does a full history pull; later runs are incremental. Re-run
`sync` (and occasionally `details`) to pull in recent activities.

## Update race predictions

The Goals pane reads a precomputed supervised race model from
`data/race-model.json`. The browser does not fit the model at runtime; rebuild
the JSON after syncing Strava or editing race labels:

```sh
python scripts/sync_strava.py sync
python scripts/race_model.py
```

Race labels live in `data/races.json`. Add only true performance labels there:
races, time trials, or deliberate benchmark efforts. Ordinary easy, workout, and
long runs should stay out of the registry; they are used only as training-load
features for the model.

Update the model:

- after each race or time trial, by adding/checking the race row and running
  `python scripts/race_model.py`
- weekly during training, after syncing Strava, so current load factors and
  predictions stay fresh
- before relying on the Goals race prediction if recent activities have not yet
  been synced and rebuilt
