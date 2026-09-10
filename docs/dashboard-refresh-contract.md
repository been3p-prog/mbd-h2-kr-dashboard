# Dashboard refresh contract

The production artifact is index.html, deployed by the existing GitHub Pages workflow.
The original full renderer is not in this repository; the three refresh scripts maintain
the current-month surfaces. Do not regenerate from an unrelated template.

## Revenue and quality

- RAW is three-team recognized revenue through the source snapshot date. General ads
  retain the existing strict 3P/type/cancellation filters; Live RAW uses strict 3P AF.
- Current forecasts consume the existing canonical `revenue.v_revenue_forecast_monthly`:
  general ads use non-cancel general-ad bookings (not the RAW-only party filter),
  attributed contracts use contract amount by start month, and Live uses confirmed
  bookings' package fees. RAW recognition remains separate and unchanged.
  Only aggregate forecast columns are copied into the snapshot. Missing teams,
  duplicate rows, non-finite/negative amounts, wrong provenance or total mismatch
  fail the refresh and retain the last-good publication; no RAW/cost fallback is used.
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
# Two-card header contract (2026-09-10)

- The header always has two direct KPI cards: current forecast/MTD, closed actual/target+GAP, or future booking/target.
- Daily refresh and canonical close share `dashboard_kpi_cards.py`; an accepted current-month close is not reopened by the daily renderer.
- Current MTD MoM compares the previous calendar month from day 1 to the same day, clamped to that month's end. January crosses into the previous year.
- Both MTD amounts use identical approved revenue filters on the same read-only snapshot. The comparison is a restated prior period, not an archived “as known then” snapshot.
- A missing or non-positive comparison denominator is unavailable, not 0%. Forecast MoM compares the canonical forecast total with the prior full-month three-team canonical actual, not prior bookings or prior MTD.
- Closed-month MoM uses the prior full-month canonical actual; target and signed GAP occupy the second card.
- The surrounding layout and quality sections are preserved. The current-month chart and team cards use the same canonical forecast total/components as the header. The chart fails closed if forecast exceeds the retained annual scale; a larger range requires a separately verified rescale.
- The current sidebar's `차월 부킹` and the next month's header, team cards and chart are refreshed together from the same canonical forecast snapshot and next-month targets. Missing/invalid next-month team or target data fails the whole refresh before saving, retaining the last-good publication rather than showing missing data as zero or 미편성. December does not fabricate a next-year surface.
