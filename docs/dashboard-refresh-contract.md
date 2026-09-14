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
- Forecast team-card hover details refresh from the corresponding monthly
  booking/contract sources: general ads show paid, free-support and government-support
  item/slot rows; attributed ads show contract-type rows; Live shows confirmed-slot
  package rows. The detail-source subtotal is displayed beside the canonical forecast.
  Any difference remains visible as a reconciliation item and never changes the card total.
- Completed revenue remains closed; current and prior-month Live/YouTube quality,
  content metrics, averages and MoM may receive late facts without reopening revenue.
- Published YouTube D7/PIS rows show the verified accumulating D0-D6 prefix before
  completion, labeled with actual coverage. The first complete seven-day metrics
  freeze and are carried in the last-good snapshot across source-copy replacement
  and month rollover; later API/public lifetime growth cannot change them. Preserve
  this cache during recovery. Unpublished/unmatched rows remain unavailable, and
  partial rows stay outside completed-D7 averages. API processing delay can defer
  the final freeze beyond the calendar seventh day; it never extends the window.
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
Main/detail Live performance count, 1D total and average are cross-checked before deployment.
The full schedule count is separate from the positive-GMV performance population.
Completed Live cards may publish the escaped `공식 회고`, `회고 발송`, and
`타사 라이브 이력` fields in a hover/tap overlay. `내부회고` stays outside the
public query, rendered HTML, manifest allowlist, and release artifact. Missing
official reviews show `회고 입력 대기`; they are never filled from another field.

## Full weekly content visibility (2026-09-10)

- The current Live ledger is rebuilt from all non-cancelled `live.raw_slots` rows
  for the selected current month on each daily refresh. New, moved and removed
  slots reconcile with the source; same-day/same-brand broadcasts are not merged.
  Free slots remain visible with an exclusion label; quality/revenue filters are unchanged.
- All calendar weeks are open by default, with chronological rows. Future metrics
  remain unavailable even if the source contains a value. Empty metrics are not
  presented as zero; unmeasured elapsed slots say 실적 집계 대기.
- A reconciled previous-month ledger also receives late facts without reopening revenue.
  Source snapshot date and the historical month-end classification cutoff stay separate.
- YouTube includes every active published video through the refresh cutoff, with
  current cumulative views and available D+7 Analytics. Full planning coverage uses
  the schedule source declared in `data/source_contract.json`: tab
  `편성/개별 성과 아카이빙`, gid `1570456410`, header row 6, A:G from row 7.
  Schedule collection, validated cache and rendered coverage require verification;
  source registration alone is not proof of a live connection. Unassigned slots
  remain visible or separately counted, and unpublished/unmatched metrics are
  unavailable. Publication and quality denominators retain their published-only
  definitions. All month weeks are open; empty weeks imply no plans only after
  successful schedule-source coverage checks. See `docs/source-of-truth.md`.
- Public source counts, dates, full-week coverage and Live status counts are guarded.
  This remains the existing daily batch, not realtime; upstream metric delays remain visible.

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
- Current team cards use those same two comparisons: `마감예상 전월 대비` uses each team's prior full-month canonical actual; `RAW 전월 동일기간` uses each team's prior MTD under identical filters. Both show the baseline period and amount. Missing prior teams remain unavailable, while a verified zero explicitly makes the rate unavailable. Exact team components reconcile to the headline totals and are checked against the visible rates before publication.
- Closed-month MoM uses the prior full-month canonical actual; target and signed GAP occupy the second card.
- The surrounding layout and quality sections are preserved. The current-month chart and team cards use the same canonical forecast total/components as the header. The chart fails closed if forecast exceeds the retained annual scale; a larger range requires a separately verified rescale.
- The current sidebar's `차월 부킹` and the next month's header, team cards and chart are refreshed together from the same canonical forecast snapshot and next-month targets. Missing/invalid next-month team or target data fails the whole refresh before saving, retaining the last-good publication rather than showing missing data as zero or 미편성. December does not fabricate a next-year surface.

## Automatic D7 recovery and regression checks

The existing daily 10:20 KST Hermes job clones main and invokes its installed orchestration wrapper, which uses the repository’s current collection/rendering code and validates the generated artifact. The repository runner also executes the YouTube progressive/freeze and dashboard baseline/readback regressions before collecting; GitHub Actions enforces these regressions before either path can publish to Pages. The verified payload is atomically backed up outside `/tmp` at `~/Library/Application Support/MBD H2 Dashboard/youtube-d7-archive.json` (override: `MBD_H2_D7_ARCHIVE`). Durable freezes take precedence over a replaceable snapshot. API coverage regression, missing registered publication identities, changed freezes, and corrupt archives fail closed; they never erase the last verified YouTube surface. Existing bounded retries and failure notifications remain in effect. GitHub Actions runs the same YouTube regression family before Pages deployment. Public readback checks the current RAW card by its semantic role and exact deployed bytes.
