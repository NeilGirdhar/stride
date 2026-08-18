# Stride

Stride is a personal, Strava-backed running dashboard and marathon-progression
tool. A static browser application renders committed JSON data; bundled Python
commands pull that data from Strava and build the heavier model artifacts
offline.

The application has three panes:

- **Log** plots running history and weekly volume, with club, shoe, race, and
  best-effort details.
- **Fitness** estimates slow-decaying fitness, fast-decaying fatigue, form, and a
  simple Rest/Jog/Run/Push signal.
- **Progression** tracks load tolerance, running economy, durability, and race
  predictions against a configured marathon goal.

There is no runtime backend, database, framework, or frontend build step. See
[`ARCHITECTURE.md`](ARCHITECTURE.md) for the data flow, module map, and metric and
model definitions.

## Set up Strava

Create API credentials at <https://www.strava.com/settings/api>. Set the
**Authorization Callback Domain** to exactly `localhost`, then run:

```sh
uv run stride-sync auth
```

The command prompts for the client ID and secret, opens Strava authorization,
and stores the credentials and OAuth tokens under the gitignored
`data/private/` directory.

## Refresh the dashboard

Run the pipeline in this order:

```sh
uv run stride-sync sync
uv run stride-sync details
uv run stride-sync streams
uv run stride-race-model
uv run stride-durability-model
```

- `sync` incrementally fetches activities and rebuilds the fitness summary. Use
  `sync --full` to ignore the stored checkpoint.
- `details` refreshes run details, shoes, best efforts, grade-adjusted metrics,
  and records.
- `streams` refreshes the resumable segment cache, measured maximum HR, HR-zone
  boundaries, and sustained-zone economy metrics.
- The final two commands rebuild the race-prediction and durability artifacts.

`details` and `streams` accept `--limit=N` for rate-limited backfills. Model
artifacts are not rebuilt automatically by the sync commands.

True race efforts are registered manually in `data/entered/races.json`. The
marathon goal, conditions, prediction distances, and progression targets live in
`data/entered/marathon-goal.json`.

## Run locally

ES modules and JSON fetches require an HTTP origin:

```sh
python -m http.server 8000
```

Then open <http://localhost:8000>.

## Project layout

- `index.html`, `js/`, `css/` — static three-pane browser application
- `src/stride/` — Strava sync and offline model commands
- `data/imported/` — slimmed Strava activities, run details, and gear
- `data/generated/` — reproducible browser-facing summaries and models
- `data/entered/` — hand-maintained goals, race labels, clubs, and training config
- `data/private/` — credentials, tokens, and the large stream cache; never commit
- `ARCHITECTURE.md` — technical design and model semantics
- `AGENTS.md` — repository instructions for coding agents

Imported and generated JSON is intentionally committed so the deployed static
site has data. It omits GPS coordinates but still contains personal activity
names, dates, Strava IDs, performance data, shoe names, and some photo URLs.
Treat changes to these files as user data and do not discard or regenerate them
unless the task calls for it.

## Verify changes

```sh
npm test
npx eslint .
uv run ruff check .
uv run ty check
uv lock --check
```

`npm test` currently performs JavaScript syntax checks. There is no behavioral
JavaScript suite or Python `tests/` directory.

## License

Apache-2.0. See `LICENSE`.
