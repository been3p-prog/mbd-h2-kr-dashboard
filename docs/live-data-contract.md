# Live data usage contract

This contract is the release rule for the MBD H2 KR dashboard Live window. It was added after the 2026-08-11 Downing GMV incident so that visual copy cannot silently switch metric basis.

## Source of truth

- Primary source: `[MBD] 라이브커머스 마스터시트` → `[DB]구좌 RAW`
- Spreadsheet ID: `1Kw-IMgnP_kj0qY3q8thqsrPQ_KQvypTAX3hT5J-Gp4Q`
- Header row: `25`
- Current Live-window slice: rows `302–308`, period `2026-08-03`–`2026-08-06`
- Public dashboard reads a static snapshot. If source rows change, update `data/live_window_contract.json` from the sheet before editing copy.

## Mandatory metric-basis rules

| Surface | Visible label | Required source column | Rule |
|---|---|---|---|
| Broadcast card visible amount | `거래액` | `일 전체 GMV (라이브 브랜드 전체)` | Always display as `1D ...`. This is the same basis as the existing live ledger/average/target surface. |
| Weekly total / efficiency diagnosis | `방송별 GMV`, `방당 GMV` | `방송별 데이터 GMV` | Use only for weekly aggregation, per-broadcast efficiency, or explicit broadcast-efficiency diagnosis. |
| During-live operations | `1H` | `라이브 1H GMV` | Use only for real-time/live-window operational performance. |
| Generic copy | never `GMV` alone | none | Forbidden. Every GMV/거래액 mention needs a visible basis or an approved label. |

## Downing incident rule

- Downing row `306` has two different valid values:
  - `일 전체 GMV (라이브 브랜드 전체)`: `141,286,085` → visible card value `1D 1.41억`
  - `방송별 데이터 GMV`: `60,449,660` → **must not** be surfaced as the card headline
- The strings `6,045만`, `방송GMV`, and `방송GMV 6,045만` are forbidden in the Live window unless 빈님 explicitly changes the contract.

## Release gate

Every release must pass:

```bash
python3 -m unittest scripts/test_verify_dashboard.py -v
python3 scripts/verify_dashboard.py index.html
python3 scripts/smoke_dashboard.py index.html
python3 scripts/verify_live_window_contract.py index.html
```

The live-window contract checker validates:

1. contract/basis copy is visible;
2. weekly KPI numbers match the contract;
3. package lens percentages match the contract;
4. each of the seven broadcast cards has the expected brand, metadata, viewer count, click rate, buyers, and `1D` amount;
5. forbidden ambiguous/incorrect strings are absent.

Do not report “numeric reconciliation complete” unless this contract check has passed against the deployed/public HTML or a public readback copy.
