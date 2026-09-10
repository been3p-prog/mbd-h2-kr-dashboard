# Dashboard refresh contract

The production artifact is index.html, deployed by the existing GitHub Pages workflow.
The original full renderer is not in this repository; the three refresh scripts maintain
the current-month surfaces. Do not regenerate from an unrelated template.

## Revenue and quality

- RAW is three-team recognized revenue through the source snapshot date. General ads
  retain the existing strict 3P/type/cancellation filters; Live RAW uses strict 3P AF.
- Current general-ad and attributed-contract forecasts use full-month versions of those
  filters. The Live forecast fee/party policy is unresolved. It is unavailable, not
  zero; the overall forecast, achievement and GAP remain unavailable in cards and chart.
  Do not enable the draft all-booked package-fee rule without owner confirmation.
- Completed revenue remains closed; current and prior-month Live/YouTube quality,
  content metrics, averages and MoM may receive late facts without reopening revenue.
- Other YouTube formats (for example LIVE) count in overall publication/performance,
  but not in LF/SF-only denominators. They are disclosed as 기타.
- Older/future booking surfaces retain their existing snapshot and are not evidence of
  a fresh forecast. January uses previous December for MoM; cross-year template reuse
  is blocked until a matching-year artifact exists.

## Freshness and public scope

Build time is not data freshness. MBD freshness uses the transport's timezone-aware
source-file modification time and records snapshot capture separately. Naive business
ingest timezone is not inferred. Per-stage payload hashes survive independent refreshes;
the legacy hash remains the YouTube contract hash after a complete pipeline.
Optional YouTube failure retains last-good data/hash with an explicit stale disclosure.

Public Live rows do not publish PD names, cost or margin. The already-visible hourly
measurement is 1H, not 3H; untouched historical 3H columns retain their prior basis.
Main/detail Live count, 1D total and average are cross-checked before deployment.

## Operating sequence

Use the existing isolated cron runner; do not change source DBs, credentials, or schedules.
Its daily sequence is snapshot validation → Live daily → Live window → YouTube →
unit/static/browser/contract guards → commit/push → exact-revision Pages readback.
The existing daily refresh is 10:20 KST; the scheduled Pages freshness guard is 09:00 KST.
Next natural-run evidence is separate from a manually verified deployment.

Local verification:

    python3 -B -m unittest discover -s scripts -p 'test_*.py'
    python3 scripts/verify_dashboard.py index.html --require-fresh
    python3 scripts/verify_live_window_contract.py index.html
    git diff --check

Rollback is a reviewed revert of the deployment commit followed by the normal Pages
workflow, not a destructive reset of operator worktrees.
