# MBD H2 KR Dashboard

Stable GitHub Pages permalink for the dashboard.

## Release guard

```bash
python3 -m unittest scripts/test_verify_dashboard.py -v
python3 -m unittest scripts/test_refresh_current_raw.py -v
python3 scripts/verify_dashboard.py index.html
python3 scripts/smoke_dashboard.py index.html
python3 scripts/verify_live_window_contract.py index.html
```

The guard verifies selected-period diagnosis wiring, RAW/forecast semantic separation, the B22N revenue scope allowlist (`ad_gen`, `ad_int`, `live` only; `owned_youtube_ad` and `ogam` excluded), current-month RAW reconciliation, and per-source freshness isolation. The daily refresher excludes future, cancelled, 1P, and free-support rows from current RAW. GitHub Pages deploys only after the guard passes. A scheduled probe additionally fails when the published snapshot is older than 48 hours.

## Live data contract

Live-window numeric copy is governed by `docs/live-data-contract.md` and `data/live_window_contract.json`. Broadcast-card visible `거래액` must use `[DB]구좌 RAW` `일 전체 GMV (라이브 브랜드 전체)` as `1D ...`; weekly efficiency surfaces may use `방송별 데이터 GMV` only when explicitly labeled as `방송별 GMV` / `방당 GMV`. The contract checker blocks ambiguous `GMV` copy and the Downing `6,045만` incident marker from the Live window.
