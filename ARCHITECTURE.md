# Stride architecture

This document describes the current implementation. Stride is a static,
single-runner dashboard backed by committed Strava-derived JSON and offline
Python computations. It is not the removed 17-week planner application.

## System overview

```text
Strava API
   |
   v
stride-sync auth/sync/details/streams
   |
   +-- data/imported/   slim activities, run details, and shoes
   +-- data/generated/  summary, records, HR zones, and zone metrics
   +-- data/private/    credentials, tokens, and the large stream cache
   |
   +--> stride-race-model ------> data/generated/race-model.json
   +--> stride-durability-model -> data/generated/durability-model.json
                                    |
                                    v
index.html -> js/app.js -> three view modules -> SVG/HTML in the browser
```

The browser performs no model fitting and writes no state. Each view fetches its
JSON inputs once, stores them in module-level variables, and rebuilds its HTML
when the user changes a range, tab, or selection. JSON requests carry a timestamp
query parameter to avoid stale browser caches.

## Frontend

`index.html` contains the fixed three-tab shell. `js/app.js` switches the active
page and calls its renderer:

- `js/views/running.js` renders the Log pane. Runs are dots positioned by date
  and distance; a Gaussian weekly-volume estimate is overlaid. The side panel
  groups visible runs by club or shoe and lists best efforts. Clicking a run
  opens its details and Strava link.
- `js/views/fitness.js` renders Fitness. It computes the causal load model in the
  browser, derives a heuristic training signal, and charts fitness, fatigue, and
  form.
- `js/views/goals.js` renders Progression. It combines imported runs with
  precomputed zone, race, and durability artifacts, constructs metric series,
  and compares them with configured target trajectories.

Shared modules are deliberately small:

- `js/lib/ranges.js` defines the selectable date ranges and local-date parsing.
- `js/lib/records.js` defines record keys, labels, and standard distances.
- `js/lib/smoothing.js` provides Gaussian rate and observation smoothers.
- `js/lib/time-axis.js` chooses and formats adaptive time-axis ticks.

The stylesheets are split by responsibility. `css/theme.css` defines Catppuccin
Latte and Mocha palettes and semantic tokens. `css/styles.css` defines the shell,
navigation, typography, and shared components. `css/app.css` contains the chart,
table, modal, fitness, progression, and responsive layout rules.

`manifest.json` provides install metadata and icons. There is no service worker
or offline cache, so the application is not currently offline-capable.

## Offline data pipeline

The Python package requires Python 3.14. The Strava client uses the standard
library; NumPy is used by the two models.

### Authentication and requests

`stride-sync auth` starts a temporary localhost HTTP server, opens Strava's OAuth
authorization page, exchanges the returned code, and saves credentials and
tokens under `data/private/`. Long-running requests refresh expired tokens.
HTTP 429 responses wait until just after Strava's next 15-minute rate-limit
boundary and retry.

### Activity sync

`stride-sync sync` fetches all sport types. The first run retrieves the full
history; later runs use the newest stored activity as an incremental checkpoint.
Activities are deduplicated by Strava ID, sorted by start date, and reduced to
the fields used by this project. The command writes:

- `data/imported/strava-activities.json`
- `data/generated/fitness-summary.json`

It also removes manual club overrides that have become redundant because the
activity name now matches its configured club pattern. The fitness summary is a
useful side artifact but is not currently fetched by the browser.

### Details and records

`stride-sync details` processes runs and trail runs. It fetches activity details
and time, distance, grade, and heart-rate streams, then stores:

- gear ID and an optional photo URL
- Strava best efforts
- grade-adjusted distance
- aerobic efficiency and aerobic power from in-band stream segments
- best 60-second grade-adjusted speed
- fallback fastest 30 km and marathon windows for sufficiently long runs

The command writes `strava-run-details.json`, fetches previously unseen shoes
into `strava-gear.json`, and recomputes `records.json`. It is resumable because
already complete activity IDs are skipped.

### Segment cache, maximum HR, and zone metrics

`stride-sync streams` fetches the same four stream types for the durability and
zone-economy pipeline. It filters out warm-up samples, implausible speeds and HR,
large sample gaps, and abrupt speed jumps, then stores only segment duration,
speed, grade-adjusted speed, and HR in the private cache.

Measured maximum HR is the best rolling average over 10-, 30-, and 60-second
windows across cached runs, not a one-sample peak. Configured fractions turn it
into HR-zone dividers and narrower economy bands.

Per-run zone economy is grade-adjusted metres per heartbeat over sustained
in-band blocks. A block must last at least 60 seconds, and its first 20 seconds
are removed to reduce inflation from HR lag. The three bands are aerobic
efficiency, aerobic power, and anaerobic power above its configured HR floor.

### Artifact dependency table

| File | Produced or maintained by | Browser consumer |
| --- | --- | --- |
| `data/imported/strava-activities.json` | `stride-sync sync` | all panes |
| `data/imported/strava-run-details.json` | `stride-sync details` | Log, Progression |
| `data/imported/strava-gear.json` | `stride-sync details` | Log |
| `data/generated/records.json` | `stride-sync details` | Log, Progression |
| `data/generated/hr-zones.json` | `stride-sync streams` | offline model code |
| `data/generated/zone-metrics.json` | `stride-sync streams` | Progression |
| `data/generated/race-model.json` | `stride-race-model` | predictions |
| `data/generated/durability-model.json` | `stride-durability-model` | durability |
| `data/generated/fitness-summary.json` | `stride-sync sync` | currently unused |
| `data/entered/marathon-goal.json` | hand maintained | Progression, race model |
| `data/entered/races.json` | hand maintained | Log classification, race model |
| `data/entered/training-config.json` | hand maintained | sync/model code, all panes |

## Metric semantics

### Log volume

The volume curve is a Gaussian kernel estimate of weekly kilometres with a
9-day standard deviation. It is symmetric and descriptive rather than causal.
Boundary mass is renormalized so the start and end of the available history are
not biased downward.

### Fitness, fatigue, and form

Each run contributes kilometre-equivalent load:

```text
(distance_km + elevation_gain_m / 100) * pace_adjustment
```

The pace adjustment compares the run with the all-history median pace and is
capped between modest slow-run and fast-run adjustments. Fitness and fatigue
are causal exponential accumulators with 42- and 7-day time constants, expressed
as weekly load. Form is fitness minus fatigue.

Fitness and fatigue momentum are separately smoothed changes. A heuristic score
combines form, fitness momentum, and fatigue momentum into Rest, Jog, Run, or
Push. This is not a medical readiness assessment. The displayed lines receive a
two-day symmetric Gaussian smooth for appearance only; that does not change the
causal model or current score.

### Progression metrics

- **Load tolerance** is a Gaussian weekly rate of grade-adjusted kilometres.
- **Aerobic efficiency** is grade-adjusted metres per heartbeat in the configured
  high-zone-2 band.
- **Aerobic power** is the same measure in the configured high-zone-3 band.
- **Anaerobic power** in the UI is the same measure above the configured HR
  floor.
- **Durability** is the fraction of fresh grade-adjusted metres per heartbeat
  retained after 4000 units of cumulative adjusted effort within a run.

The three economy observations receive a 28-day Gaussian smooth because the
qualifying per-run samples are sparse and noisy. The durability artifact is
fitted in trailing 180-day windows and sampled weekly.

Target lines come from `data/entered/marathon-goal.json`. Line targets span the
configured range; ramp targets change between configured ramp dates; goal-ramp
targets start from an early measured baseline. The Focus box selects the largest
relative shortfall among metrics with targets. Durability currently has no
target, so it is displayed but excluded from Focus.

## Race model

Only manually registered true efforts in `data/entered/races.json` are labels.
For each label, features use only earlier runs and explicitly exclude the race
activity itself.

Race times are normalized to neutral conditions before fitting:

- distance uses a Riegel exponent of 1.06
- course cost uses grade-adjusted distance or an elevation-gain fallback
- temperature uses a race override or Montreal monthly climatology
- trail races receive a fixed surface multiplier

The model fits ridge regression to log-time residuals using five standardized
features: aerobic efficiency, aerobic power, anaerobic power, load tolerance,
and durability. Missing stream-derived features are median-imputed. A current
prediction blends the fitted model with the strongest normalized prior-race
evidence, then reapplies the configured goal-course conditions to the marathon
estimate.

This is a small, runner-specific model. Its reported RMSE is in-sample training
error, not validated out-of-sample accuracy. Generated daily series extend to
race day by holding the latest fitness constant, representing maintenance rather
than a forecast of a planned training block.

### Known metric mismatch

The race model and Progression pane currently attach the same names to different
sources:

- The model reads aerobic values from `strava-run-details.json`; Progression
  reads sustained-block values from `zone-metrics.json`.
- The model's `anaerobic_power` is best 60-second grade-adjusted speed;
  Progression's is sustained high-HR grade-adjusted metres per heartbeat.

Preserve this behavior unless a task deliberately unifies the measurements. Do
not assume that identically named fields are semantically interchangeable.

## Durability model

Stream segments are grouped by run. Cumulative adjusted effort combines
grade-adjusted speed, segment time, relative speed, and relative HR. A fresh
efficiency curve controls for effort rate. Each segment's observed efficiency is
divided by that baseline and normalized to fresh samples.

The model bins retention by cumulative load and enforces a non-increasing
durability curve. The headline reads the retained fraction at a fixed 4000 CAE,
which is more comparable over time than a threshold tied to each run's maximum
length. The historical series refits that measurement in full trailing 180-day
windows and requires at least 2000 samples per window.

## Hand-maintained inputs

- `races.json` should contain only races, time trials, and deliberate benchmarks.
- `club-patterns.json` contains automatic name regular expressions;
  `club-overrides.json` contains exceptions only.
- `training-config.json` is the source for the serious-running date, HR-zone
  fractions, and economy bands.
- `marathon-goal.json` controls race date and conditions, prediction distances,
  model score weights, progression sections, and A/B/C trajectories.

## Known implementation edges

- Empty-state messages still name the removed `scripts/sync_strava.py`; the real
  command is `uv run stride-sync sync`.
- The package metadata links to a missing `CHANGELOG.md`.
- Model artifacts are not automatically refreshed after sync and can be older
  than activity data. Inspect `generated_at` and `as_of` before interpreting
  predictions.
- There is no behavioral JavaScript suite or Python test directory. `npm test`
  performs JavaScript syntax checks.
- User-supplied or dynamic strings rendered into HTML must be escaped. The Log
  pane provides a local `esc` helper; other views mostly render trusted entered
  or generated configuration.
