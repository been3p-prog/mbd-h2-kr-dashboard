# Dashboard / Slack shared metrics

The public dashboard remains on GitHub Pages. No new public metrics dataset is
published. `scripts/export_bot_metrics.py` creates a **private derived cache** from
the same read-only MBD/YouTube snapshots used by the HTML refresh.

## Contract

- Version: `mbd-bot-metrics-v1`; bound to SHA256 of actual `index.html` bytes.
- Verified question coverage: previous month through current month; next-month
  forecast/schedule when present. Earlier unrefreshed static months fail closed.
- Monthly revenue: explicit canonical forecast versus closed canonical actual
  versus RAW MTD. Daily/weekly revenue is a difference of the existing RAW MTD
  function, not a different booking query or an allocated monthly close.
- Live: exact brand scope, calendar dates, cancellation exclusion, positive-1D
  quality population with free rows excluded. 1D and broadcast GMV remain distinct.
  Unknown 1P/3P AF remains outside RAW and is disclosed, never imputed as 3P.
- YouTube: official complete/partial periods, actual publication cohort, completed
  Analytics D7 and latest cumulative values remain distinct. No verified daily
  channel total exists in this source contract; daily answers explicitly say so
  and separately report publication/D7 details. No daily total is synthesized.
- Weeks default to Monday–Sunday. `9월 1주차` explicitly requests the dashboard's
  month-week (days 1–7). Answers show exact start/end dates.
- Unsupported brand/period constraints are rejected, never dropped.
- Source freshness and cache freshness are separately checked (48h), along with
  the live public HTML hash. Failed checks return unavailable, never legacy or LLM
  numeric fallback. Source memo, PD, cost, margin and credentials are excluded.

## Runtime

Private runtime root: `/Users/cnc-media/services/mbd-dashboard-metrics`.
The common engine is `dashboard_metrics_client.py`, with `data/metrics.json` (0600).
Python bots invoke the engine through `bot_metrics_adapter.py`; the ad booking bot
uses `bot-metrics-adapter.mjs`. All original channel/user authorization happens
before metric access; no authorization configuration is changed.

Ad booking operations retain the original lane. Explicit `부킹 기준 ... 매출` and
booking occupancy queries retain the admin source and are not described as
dashboard actuals. Generic revenue/forecast questions use the shared lane.

The existing daily runner calls `scripts/publish_bot_metrics.py` after exact Pages
readback (also on a verified HTML no-op). This uses the already configured SSH
transport to atomically replace only the private cache and verifies its checksum.
No new cron, auth or source writer is installed. If YouTube acquisition fails,
the old cache is retained and a mismatching release fails closed at the client.

## Verification and rollback

Run `python3 -B -m unittest discover -s scripts -p 'test_*.py'`, static and browser
guards, export against the exact snapshot pair, adapter authorization tests and
then Slack E2E. **All test messages are restricted to been_jobs (C086M0WDKPC).**

Deployment backs up only the replaced runtime files before installation. Rollback
restores those exact files and the previous private cache, then restarts only the
three named bot processes. Dashboard rollback is a reviewed revert, never reset.
Do not revert unrelated worktrees or change upstream business classifications.
