# Strava sync

Pulls your full Strava history (all sports) once, then incrementally fetches
new activities. Writes everything under `data/` (gitignored — it's your
personal data):

- `data/strava-activities.json` — raw activity list, deduped by id
- `data/fitness-summary.json` — compact derived metrics (volume by sport,
  12-week trend, Riegel-equivalent 5K, running cadence + HR-efficiency by month)
- `data/strava-tokens.json` — OAuth tokens

No third-party packages — Python 3 standard library only.

## One-time setup

1. **Get Strava API credentials.** Go to <https://www.strava.com/settings/api>.
   If you already have an app, note its **Client ID** and **Client Secret**.
   If not, create one (any name; website can be `http://localhost`).
   Set **Authorization Callback Domain** to exactly:

   ```
   localhost
   ```

2. **Add your credentials** (kept out of git):

   ```sh
   cp scripts/strava_config.example.json scripts/strava_config.json
   # then edit scripts/strava_config.json and fill in client_id + client_secret
   ```

3. **Authorize** (opens your browser; click *Authorize*):

   ```sh
   python3 scripts/sync_strava.py auth
   ```

## Pulling data

First run does a full history pull; every run after that is incremental:

```sh
python3 scripts/sync_strava.py sync          # new activities + recompute summary
python3 scripts/sync_strava.py sync --full   # ignore checkpoint, refetch all
```

Re-run `sync` whenever you want to pull in recent activities.
