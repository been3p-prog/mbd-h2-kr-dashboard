#!/usr/bin/env python3
"""Refresh the live-daily portions of the static MBD H2 Pages dashboard.

This is intentionally narrow: the public GitHub Pages artifact is a static
single-file dashboard whose original full generator is not present in this
repo. This script keeps the user-visible Live RAW / Live 1D quality surfaces
fresh from the local MBD DuckDB, without pretending that non-live sources were
rebuilt.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

import duckdb

KST = dt.timezone(dt.timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HTML = ROOT / "index.html"
DEFAULT_DUCKDB = Path("/Users/sb.lee/automations/mbd/mbd.duckdb")
TARGET_WON = 100_000_000

TEAM_ORDER = ["overall", "signature", "smart", "essential"]
PACKAGE_LABELS = {
    "overall": "전체 평균",
    "signature": "시그니처",
    "smart": "스마트",
    "essential": "에센셜",
}
PACKAGE_KO_TO_KEY = {
    "시그니처": "signature",
    "스마트": "smart",
    "에센셜": "essential",
}


def clean_int(value) -> int:
    if value is None:
        return 0
    text = str(value).replace(",", "").replace("원", "").strip()
    if not text or text in {"-", "—", "nan", "None"}:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def package_key(value: str | None) -> str | None:
    text = (value or "").strip()
    for ko, key in PACKAGE_KO_TO_KEY.items():
        if ko in text:
            return key
    return None


def fmt_won(value: float | int, *, decimals_for_eok: int = 2) -> str:
    value = float(value or 0)
    if abs(value) >= 100_000_000:
        s = f"{value / 100_000_000:.{decimals_for_eok}f}".rstrip("0").rstrip(".")
        return f"{s}억"
    if abs(value) >= 10_000:
        return f"{round(value / 10_000):,}만"
    return f"{round(value):,}"


def fmt_sum(value: float | int) -> str:
    value = float(value or 0)
    if abs(value) >= 100_000_000:
        s = f"{value / 100_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{s}억"
    return fmt_won(value)


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}%"


def fmt_delta(value: float | None) -> tuple[str, str, str]:
    if value is None:
        return "flat", "—", "MoM —"
    if value >= 0.05:
        return "up", "▲", f"MoM ▲ {value:.1f}%"
    if value <= -0.05:
        return "dn", "▼", f"MoM ▼ {abs(value):.1f}%"
    return "flat", "—", "MoM —"


def fmt_m_d(day: dt.date) -> str:
    return f"{day.month}/{day.day}"


def fetch_live_rows(db_path: Path, year: int, month: int) -> tuple[list[dict], str | None]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        start = dt.date(year, month, 1)
        next_month = dt.date(year + (month == 12), 1 if month == 12 else month + 1, 1)
        rows = con.execute(
            r'''
            select
              TRY_CAST("온에어 일자" as date) as d,
              "브랜드명" as brand,
              "패키지" as package,
              "PGM" as pgm,
              "라이브 시청자 (비로그인 포함)" as viewers,
              "일 전체 GMV (라이브 브랜드 전체)" as gmv_1d,
              "라이브 1H GMV" as gmv_1h
            from live.raw_slots
            where TRY_CAST("온에어 일자" as date) >= ?
              and TRY_CAST("온에어 일자" as date) < ?
            order by d, brand
            ''',
            [start, next_month],
        ).fetchall()
        ingest = con.execute(
            "select max(last_ingest_at) from meta.ingest_log where status='ok'"
        ).fetchone()[0]
    finally:
        con.close()
    out = []
    for d, brand, pkg, pgm, viewers, gmv_1d, gmv_1h in rows:
        out.append({
            "date": d,
            "brand": brand or "",
            "package": pkg or "",
            "package_key": package_key(pkg),
            "pgm": pgm or "",
            "viewers": clean_int(viewers),
            "gmv_1d": clean_int(gmv_1d),
            "gmv_1h": clean_int(gmv_1h),
        })
    return out, str(ingest) if ingest else None


def summarize(rows: list[dict], prev_rows: list[dict]) -> dict:
    positive = [r for r in rows if r["gmv_1d"] > 0]
    prev_positive = [r for r in prev_rows if r["gmv_1d"] > 0]

    def group(items: list[dict], key: str | None = None) -> dict:
        if key:
            items = [r for r in items if r["package_key"] == key]
        total = sum(r["gmv_1d"] for r in items)
        n = len(items)
        return {"n": n, "sum": total, "avg": (total / n if n else None)}

    result = {"overall": group(positive)}
    prev = {"overall": group(prev_positive)}
    for key in ["signature", "smart", "essential"]:
        result[key] = group(positive, key)
        prev[key] = group(prev_positive, key)
    for key in TEAM_ORDER:
        cur_avg = result[key]["avg"]
        prev_avg = prev[key]["avg"]
        result[key]["mom"] = None if cur_avg is None or not prev_avg else (cur_avg / prev_avg - 1) * 100
    result["latest_positive_date"] = max((r["date"] for r in positive), default=None)
    result["first_month_date"] = min((r["date"] for r in rows), default=None)
    return result


def update_manifest(html: str, built: str, payload: dict) -> str:
    manifest_re = re.compile(r'(<script type="application/json" id="mbd-public-guard">)(.*?)(</script>)', re.S)
    match = manifest_re.search(html)
    if not match:
        raise RuntimeError("mbd-public-guard manifest not found")
    manifest = json.loads(match.group(2))
    manifest["built_at_kst"] = built
    # The current public guard schema treats all source timestamps as fresh. This
    # static artifact currently has a mixed-source boundary; the visible footer
    # discloses that only Live RAW/1D was refreshed by this script.
    for key in list(manifest.get("source_snapshot_as_of", {})):
        manifest["source_snapshot_as_of"][key] = built
    payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    manifest["source_payload_sha256"] = hashlib.sha256(payload_bytes).hexdigest()
    new_raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    return manifest_re.sub(lambda m: m.group(1) + new_raw + m.group(3), html, count=1)


def replace_cell(segment: str, card_key: str, stats: dict) -> str:
    label = PACKAGE_LABELS[card_key]
    cls, _, mom_text = fmt_delta(stats["mom"])
    avg = fmt_won(stats["avg"] or 0)
    total = fmt_sum(stats["sum"])
    n = stats["n"]
    new = (
        f'<div class="qcell{" hero" if card_key == "overall" else ""}" '
        f'data-live-quality-mom="8-{card_key}"><div class="qk">{label}</div>'
        f'<div class="qn num">{avg}</div><div class="qm2 num">{n}방송 · 총 {total}</div>'
        f'<div class="qmom {cls} num">{mom_text}</div></div>'
    )
    pattern = re.compile(
        rf'<div class="qcell(?: hero)?" data-live-quality-mom="8-{re.escape(card_key)}">.*?</div></div>',
        re.S,
    )
    updated, count = pattern.subn(new, segment, count=1)
    if count != 1:
        raise RuntimeError(f"failed to replace live quality card {card_key}")
    return updated


def update_live_quality(html: str, summary: dict) -> str:
    start = html.index('data-live-revenue-breakdown="8"')
    end = html.index('data-live-revenue-breakdown="9"', start)
    segment = html[start:end]
    overall = summary["overall"]
    target_pct = (overall["avg"] or 0) / TARGET_WON * 100
    cls, _, mom_text = fmt_delta(overall["mom"])
    avg = fmt_won(overall["avg"] or 0)
    segment = re.sub(
        r'(<div class="qk2">1D 평균 거래액</div>\s*<div class="qv num">).*?(</div>)',
        rf'\g<1>{avg}\g<2>',
        segment,
        count=1,
        flags=re.S,
    )
    segment = re.sub(
        r'<div class="qs"><span class="pill [^"]+ num big">8월 목표 1\.00억 대비 [^<]+</span><span class="pill [^"]+ num big" data-live-quality-mom-main="8">MoM [^<]+</span></div>',
        f'<div class="qs"><span class="pill {"up" if target_pct >= 100 else "dn"} num big">8월 목표 1.00억 대비 {fmt_pct(target_pct)}</span><span class="pill {cls} num big" data-live-quality-mom-main="8">{mom_text}</span></div>',
        segment,
        count=1,
    )
    for key in TEAM_ORDER:
        segment = replace_cell(segment, key, summary[key])
    return html[:start] + segment + html[end:]


def update_live_activity_rows(html: str, rows: list[dict]) -> str:
    by_key = {(r["date"].isoformat(), r["brand"]): r for r in rows if r["gmv_1d"] > 0 or r["viewers"] > 0 or r["gmv_1h"] > 0}

    def repl(match: re.Match) -> str:
        row = match.group(0)
        date = match.group("date")
        brand = re.sub(r'<.*?>', '', match.group("brand_html")).strip()
        data = by_key.get((date, brand))
        if not data:
            return row
        metrics = (
            f'<div class="activity-metric metric-trio num"><span class="metric-cell"><b>{data["viewers"]:,}</b></span>'
            f'<span class="metric-cell"><b>{fmt_won(data["gmv_1d"])}</b></span>'
            f'<span class="metric-cell"><b>{fmt_won(data["gmv_1h"]) if data["gmv_1h"] else "—"}</b></span></div>'
        )
        return re.sub(r'<div class="activity-metric metric-trio num">.*?</div>', metrics, row, count=1, flags=re.S)

    pattern = re.compile(
        r'<div class="activity-row">\s*<time class="activity-date" datetime="(?P<date>2026-08-\d{2})">.*?</time>.*?'
        r'<a class="content-link" data-content-link="live"[^>]*>(?P<brand_html>.*?)<span aria-hidden="true">↗</span></a>.*?'
        r'<div class="activity-metric metric-trio num">.*?</div></div>',
        re.S,
    )
    return pattern.sub(repl, html)


def update_chips_footer_and_live_row(html: str, now: dt.datetime, summary: dict, ingest: str | None) -> str:
    latest = summary["latest_positive_date"]
    first = dt.date(now.year, now.month, 1)
    if latest is None:
        raw_label = f"LIVE RAW {now.month}/1~확인중"
        range_label = f"{now.month}/1~확인중"
    else:
        raw_label = f"LIVE RAW {first.month}/1~{fmt_m_d(latest)}"
        range_label = f"{first.month}/1~{fmt_m_d(latest)}"
    html = re.sub(
        r'<span class="chip">RAW [^<]+</span><span class="chip vi">FORECAST 2026-08</span>',
        f'<span class="chip">{raw_label}</span><span class="chip vi">FORECAST 2026-08</span>',
        html,
        count=1,
    )

    # Update the Live team's current RAW row inside the team card. Keep ad_gen/ad_int
    # rows untouched because their SSOT actual source is a separate snapshot.
    live_total = None
    db = duckdb.connect(str(DEFAULT_DUCKDB), read_only=True)
    try:
        row = db.execute("select AF매출_유상 from live.kpi_monthly_total where 월='2026-08'").fetchone()
        live_total = clean_int(row[0]) if row else None
    finally:
        db.close()
    if live_total:
        live_progress = live_total / 212_000_000 * 100
        live_value = fmt_won(live_total)
        live_row = f'<div class="r"><span>LIVE RAW · {range_label}</span><b>{live_value} <span class="mutpct">진척 {fmt_pct(live_progress)}</span></b></div>'
        html = re.sub(
            r'(<span class="nm">라이브</span>.*?<div class="rows num">.*?<div class="r"><span>월 목표</span><b>2\.12억</b></div><div class="r"><span>GAP</span><b style="color:var\(--red\)">\+600만</b></div>)<div class="r"><span>RAW 누적 · 8/1~8/9</span><b>5,500만 <span class="mutpct">진척 25\.9%</span></b></div>',
            r'\1' + live_row,
            html,
            count=1,
            flags=re.S,
        )

    built_short = now.strftime("%m-%d %H:%M")
    ingest_note = ingest or now.isoformat(timespec="seconds")
    footer_head = (
        f'LIVE 빌드 {built_short} · 라이브 1D/RAW = DuckDB live.raw_slots {range_label} '
        f'· ingest {ingest_note} · 매출/OKR/온드/유튜브 = 기존 공개 스냅샷 '
    )
    html = re.sub(
        r'<div class="foot">LIVE 빌드 .*?\(<a ',
        '<div class="foot">' + footer_head + '(<a ',
        html,
        count=1,
        flags=re.S,
    )
    html = re.sub(
        r'<br>audit: .*?</div>',
        f'<br>audit: 라이브 1D 품질 {summary["overall"]["n"]}건 readback green · 정적 Pages daily refresh는 Hermes cron에서 수행</div>',
        html,
        count=1,
        flags=re.S,
    )
    return html


def refresh(html_path: Path, db_path: Path, quiet: bool = False) -> dict:
    now = dt.datetime.now(KST)
    year, month = now.year, now.month
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    rows, ingest = fetch_live_rows(db_path, year, month)
    prev_rows, _ = fetch_live_rows(db_path, prev_year, prev_month)
    summary = summarize(rows, prev_rows)
    if not summary["overall"]["n"]:
        raise RuntimeError("no positive current-month Live 1D rows found")

    html = html_path.read_text(encoding="utf-8")
    html = update_live_quality(html, summary)
    html = update_live_activity_rows(html, rows)
    html = update_chips_footer_and_live_row(html, now, summary, ingest)
    payload = {
        "script": "scripts/refresh_live_daily_from_duckdb.py",
        "generated_at_kst": now.isoformat(timespec="seconds"),
        "source": str(db_path),
        "month": f"{year:04d}-{month:02d}",
        "latest_positive_date": str(summary["latest_positive_date"]),
        "live_1d": summary,
    }
    html = update_manifest(html, now.isoformat(timespec="seconds"), payload)
    before = html_path.read_text(encoding="utf-8")
    changed = before != html
    if changed:
        html_path.write_text(html, encoding="utf-8")
    result = {
        "changed": changed,
        "latest_positive_date": str(summary["latest_positive_date"]),
        "overall_n": summary["overall"]["n"],
        "overall_sum_won": summary["overall"]["sum"],
        "overall_avg_won": summary["overall"]["avg"],
        "overall_avg_display": fmt_won(summary["overall"]["avg"] or 0),
    }
    if not quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", default=str(DEFAULT_HTML))
    parser.add_argument("--duckdb", default=str(DEFAULT_DUCKDB))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    refresh(Path(args.html), Path(args.duckdb), args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
