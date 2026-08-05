# MBD H2 KR Dashboard

Stable GitHub Pages permalink for the dashboard.

## Release guard

```bash
python3 -m unittest scripts/test_verify_dashboard.py -v
python3 scripts/verify_dashboard.py index.html
python3 scripts/smoke_dashboard.py index.html
```

The guard verifies selected-period diagnosis wiring, RAW/forecast semantic separation, the B22N revenue scope allowlist (`ad_gen`, `ad_int`, `live` only; `owned_youtube_ad` and `ogam` excluded), and visible stale-snapshot disclosure. GitHub Pages deploys only after the guard passes. A scheduled probe additionally fails when the published snapshot is older than 48 hours.
