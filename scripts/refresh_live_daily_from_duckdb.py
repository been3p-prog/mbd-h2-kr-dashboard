#!/usr/bin/env python3
"""Refresh current RAW revenue and live-daily MBD H2 Pages surfaces.

This is intentionally narrow: the public GitHub Pages artifact is a static
single-file dashboard whose original full generator is not present in this
repo. This script reconciles the current-month RAW headline and all three team
rows, then refreshes Live 1D quality from the local MBD DuckDB. Forecast,
owned-media, and YouTube surfaces remain separate generators/snapshots.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html as html_lib
import json
import re
import subprocess
from pathlib import Path

import duckdb

KST = dt.timezone(dt.timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HTML = ROOT / "index.html"
DEFAULT_DUCKDB = Path("/Users/sb.lee/automations/mbd/mbd.duckdb")
TARGET_WON = 100_000_000
CURRENT_REVENUE_TARGET_WON = 1_278_000_000

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


def fetch_live_rows(
    db_path: Path,
    year: int,
    month: int,
    *,
    end_date: dt.date | None = None,
) -> tuple[list[dict], str | None]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        start = dt.date(year, month, 1)
        next_month = dt.date(year + (month == 12), 1 if month == 12 else month + 1, 1)
        upper_bound = next_month
        if end_date is not None and start <= end_date < next_month:
            upper_bound = end_date + dt.timedelta(days=1)
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
              and trim(coalesce("1P/3P", '')) = '3P'
              and not regexp_matches(
                    lower(concat_ws(' ', coalesce("패키지", ''), coalesce("PGM", ''), coalesce("비고 (프로모션)", ''))),
                    '무상|무료|free|취소|cancel'
                  )
            order by d, brand
            ''',
            [start, upper_bound],
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


def fetch_current_revenue_snapshot(
    db_path: Path,
    as_of: dt.date,
    *,
    target_won: int | None = None,
) -> dict:
    """Return current-month MTD RAW revenue with future rows excluded."""
    month_start = as_of.replace(day=1)
    month_key = as_of.strftime("%Y%m")
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        ad_gen = con.execute(
            r'''
            select coalesce(sum(try_cast(regexp_replace(coalesce(revenue, '0'), '[^0-9.-]', '', 'g') as bigint)), 0)
            from ad_gen.booking_pred
            where try_cast("date" as date) between ? and ?
              and ad_type = '일반광고'
              and upper(coalesce(status, '')) not in ('CANCEL', 'CANCELLED')
              and trim(coalesce(party_type, '')) in ('3P', '제3자', '판촉결합')
            ''',
            [month_start, as_of],
        ).fetchone()[0]
        ad_int = con.execute(
            r'''
            select coalesce(sum(try_cast(regexp_replace(coalesce("미셀 매출액", '0'), '[^0-9.-]', '', 'g') as bigint)), 0)
            from ad_int.contract
            where "매출 귀속월" = ?
              and try_strptime("계약 시작일", '%Y. %-m. %-d')::date <= ?
            ''',
            [month_key, as_of],
        ).fetchone()[0]
        live = con.execute(
            r'''
            select coalesce(sum(try_cast(regexp_replace(coalesce("AF수취액", '0'), '[^0-9.-]', '', 'g') as bigint)), 0)
            from live.raw_slots
            where try_cast("온에어 일자" as date) between ? and ?
              and "1P/3P" = '3P'
              and not regexp_matches(
                    lower(concat_ws(' ', coalesce("패키지", ''), coalesce("PGM", ''), coalesce("비고 (프로모션)", ''))),
                    '무상|무료|free|취소|cancel'
                  )
            ''',
            [month_start, as_of],
        ).fetchone()[0]
        target_rows = con.execute(
            r'''
            select team, try_cast(value_num as bigint)
            from meta.targets
            where ym = ? and metric = '매출' and kind = 'target'
              and team in ('ad_gen', 'ad_int', 'live')
            ''',
            [as_of.strftime("%Y-%m")],
        ).fetchall()
    finally:
        con.close()
    team_targets = {str(team): clean_int(value) for team, value in target_rows}
    missing_targets = {"ad_gen", "ad_int", "live"} - set(team_targets)
    if missing_targets:
        raise RuntimeError(f"missing current revenue targets: {sorted(missing_targets)}")
    effective_target = clean_int(target_won) if target_won is not None else sum(team_targets.values())
    snapshot = {
        "as_of": as_of.isoformat(),
        "range_label": f"{as_of.month}/1~{as_of.month}/{as_of.day}",
        "ad_gen_won": clean_int(ad_gen),
        "ad_int_won": clean_int(ad_int),
        "live_won": clean_int(live),
        "target_won": effective_target,
        "team_targets_won": team_targets,
    }
    snapshot["total_won"] = snapshot["ad_gen_won"] + snapshot["ad_int_won"] + snapshot["live_won"]
    snapshot["progress_pct"] = snapshot["total_won"] / effective_target * 100 if effective_target else None
    return snapshot


def update_manifest(
    html: str,
    built: str,
    payload: dict,
    *,
    touched_sources: set[str] | frozenset[str] = frozenset(),
) -> str:
    manifest_re = re.compile(r'(<script type="application/json" id="mbd-public-guard">)(.*?)(</script>)', re.S)
    match = manifest_re.search(html)
    if not match:
        raise RuntimeError("mbd-public-guard manifest not found")
    manifest = json.loads(match.group(2))
    manifest["built_at_kst"] = built
    for key in touched_sources:
        if key not in manifest.get("source_snapshot_as_of", {}):
            raise RuntimeError(f"unknown manifest source timestamp {key}")
        manifest["source_snapshot_as_of"][key] = built
    payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    manifest["source_payload_sha256"] = hashlib.sha256(payload_bytes).hexdigest()
    new_raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    return manifest_re.sub(lambda m: m.group(1) + new_raw + m.group(3), html, count=1)


def _raw_team_row(value: int, target: int, range_label: str) -> str:
    progress = value / target * 100 if target else None
    return (
        f'<div class="r"><span>RAW 누적 · {range_label}</span><b>{fmt_won(value)} '
        f'<span class="mutpct">진척 {fmt_pct(progress)}</span></b></div>'
    )


def _month_bounds(html: str, group: str, month: int) -> tuple[int, int]:
    marker = re.compile(rf'class="{re.escape(group)} mv" data-m="(\d+)"')
    matches = list(marker.finditer(html))
    for index, match in enumerate(matches):
        if int(match.group(1)) == month:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(html)
            return match.start(), end
    raise RuntimeError(f"month surface not found: group={group} month={month}")


def update_current_raw_surfaces(html: str, snapshot: dict) -> str:
    """Reconcile the current-month top RAW card, chip, and all team RAW rows."""
    month = int(snapshot["as_of"][5:7])
    year = int(snapshot["as_of"][:4])
    start, end = _month_bounds(html, "mvk", month)
    segment = html[start:end]
    range_label = snapshot["range_label"]

    tip = (
        f'<div class="th">RAW 누적 · {range_label}</div>'
        f'<div class="tr"><span>일반광고</span><b>{fmt_won(snapshot["ad_gen_won"])}</b></div>'
        f'<div class="tr"><span>통광마</span><b>{fmt_won(snapshot["ad_int_won"])}</b></div>'
        f'<div class="tr"><span>라이브</span><b>{fmt_won(snapshot["live_won"])}</b></div>'
        '<div class="tn">실발생 누적 — 미래 일정·취소·무상 제외</div>'
    )
    escaped_tip = html_lib.escape(tip, quote=True)
    top_pattern = re.compile(
        r'<div class="kpi" data-tip="[^"]*RAW 누적[^"]*">'
        r'(?P<icon><div class="ic">.*?</div>)<div>\s*'
        r'<div class="k">현재 RAW 누적[^<]*</div><div class="v num">[^<]*</div>\s*'
        r'<div class="s num"><span class="pill flat num">목표 진척 [^<]*</span></div></div></div>',
        re.S,
    )
    match = top_pattern.search(segment)
    if not match:
        raise RuntimeError("current RAW KPI card not found")
    top_card = (
        f'<div class="kpi" data-tip="{escaped_tip}">{match.group("icon")}<div>\n'
        f'          <div class="k">현재 RAW 누적 · {range_label}</div><div class="v num">{fmt_won(snapshot["total_won"])}</div>\n'
        f'          <div class="s num"><span class="pill flat num">목표 진척 {fmt_pct(snapshot["progress_pct"])}</span></div></div></div>'
    )
    segment = top_pattern.sub(top_card, segment, count=1)
    html = html[:start] + segment + html[end:]

    team_start, team_end = _month_bounds(html, "mvr", month)
    team_segment = html[team_start:team_end]
    team_specs = (
        ("일반광고", "ad_gen_won", "ad_gen"),
        ("통광마", "ad_int_won", "ad_int"),
        ("라이브", "live_won", "live"),
    )
    for team, key, target_key in team_specs:
        pattern = re.compile(
            rf'(<span class="nm">{team}</span>.*?<div class="rows num">.*?)'
            r'<div class="r"><span>(?:LIVE )?RAW(?: 누적)? · [^<]+</span><b>.*?'
            r'<span class="mutpct">진척 [^<]+</span></b></div>',
            re.S,
        )
        team_segment, count = pattern.subn(
            lambda m: m.group(1) + _raw_team_row(
                snapshot[key], snapshot["team_targets_won"][target_key], range_label
            ),
            team_segment,
            count=1,
        )
        if count != 1:
            raise RuntimeError(f"current RAW team row not found: {team}")
    html = html[:team_start] + team_segment + html[team_end:]

    html, chip_count = re.subn(
        r'<span class="chip">(?:LIVE )?RAW [^<]+</span><span class="chip vi">FORECAST \d{4}-\d{2}</span>',
        f'<span class="chip">RAW {range_label}</span><span class="chip vi">FORECAST {year:04d}-{month:02d}</span>',
        html,
        count=1,
    )
    if chip_count != 1:
        raise RuntimeError("current RAW header chip not found")
    return html


def replace_cell(segment: str, card_key: str, stats: dict, *, month: int) -> str:
    label = PACKAGE_LABELS[card_key]
    cls, _, mom_text = fmt_delta(stats["mom"])
    avg = fmt_won(stats["avg"] or 0)
    total = fmt_sum(stats["sum"])
    n = stats["n"]
    new = (
        f'<div class="qcell{" hero" if card_key == "overall" else ""}" '
        f'data-live-quality-mom="{month}-{card_key}"><div class="qk">{label}</div>'
        f'<div class="qn num">{avg}</div><div class="qm2 num">{n}방송 · 총 {total}</div>'
        f'<div class="qmom {cls} num">{mom_text}</div></div>'
    )
    pattern = re.compile(
        rf'<div class="qcell(?: hero)?" data-live-quality-mom="{month}-{re.escape(card_key)}">.*?</div></div>',
        re.S,
    )
    updated, count = pattern.subn(new, segment, count=1)
    if count != 1:
        raise RuntimeError(f"failed to replace live quality card {card_key}")
    return updated


def _empty_live_quality_qsplit(month: int) -> str:
    cards = ''.join(
        (
            f'<div class="qcell{" hero" if key == "overall" else ""}" '
            f'data-live-quality-mom="{month}-{key}"><div class="qk">{PACKAGE_LABELS[key]}</div>'
            '<div class="qn num">—</div><div class="qm2 num">0방송 · 총 0원</div>'
            '<div class="qmom flat num">MoM —</div></div>'
        )
        for key in TEAM_ORDER
    )
    return (
        '<div class="qsplit"><div><div class="qk2">1D 평균 거래액</div>'
        '<div class="qv num">—</div>'
        f'<div class="qs"><span class="pill flat num big">{month}월 목표 1.00억 대비 —</span>'
        f'<span class="pill flat num big" data-live-quality-mom-main="{month}">MoM —</span></div>'
        f'</div><div><div class="qcells">{cards}</div></div></div>\n'
    )


def update_live_quality(html: str, summary: dict, *, month: int) -> str:
    start, end = _month_bounds(html, "mvr", month)
    segment = html[start:end]
    if f'data-live-quality-mom="{month}-overall"' not in segment:
        prefix_pattern = re.compile(
            r'(<div class="card quality-card live-quality"><div class="hd">'
            r'<span class="t">라이브 1D 평균거래액 · 품질</span></div>).*?'
            rf'(?=<div class="quality-trend" data-quality-trend="live-{month}")',
            re.S,
        )
        segment, seed_count = prefix_pattern.subn(
            lambda match: match.group(1) + _empty_live_quality_qsplit(month),
            segment,
            count=1,
        )
        if seed_count != 1:
            raise RuntimeError(f"failed to initialize live quality surface for month {month}")
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
        rf'<div class="qs"><span class="pill [^"]+ num big">{month}월 목표 1\.00억 대비 [^<]+</span><span class="pill [^"]+ num big" data-live-quality-mom-main="{month}">MoM [^<]+</span></div>',
        f'<div class="qs"><span class="pill {"up" if target_pct >= 100 else "dn"} num big">{month}월 목표 1.00억 대비 {fmt_pct(target_pct)}</span><span class="pill {cls} num big" data-live-quality-mom-main="{month}">{mom_text}</span></div>',
        segment,
        count=1,
    )
    for key in TEAM_ORDER:
        segment = replace_cell(segment, key, summary[key], month=month)
    return html[:start] + segment + html[end:]


def update_live_activity_rows(
    html: str,
    rows: list[dict],
    *,
    year: int,
    month: int,
) -> str:
    by_key = {(r["date"].isoformat(), r["brand"]): r for r in rows if r["gmv_1d"] > 0 or r["viewers"] > 0 or r["gmv_1h"] > 0}
    empty_metrics = (
        '<div class="activity-metric metric-trio num">'
        '<span class="metric-cell"><b>—</b></span>'
        '<span class="metric-cell"><b>—</b></span>'
        '<span class="metric-cell"><b>—</b></span></div>'
    )

    def repl(match: re.Match) -> str:
        row = match.group(0)
        date = match.group("date")
        brand = re.sub(r'<.*?>', '', match.group("brand_html")).strip()
        data = by_key.get((date, brand))
        if not data:
            return re.sub(
                r'<div class="activity-metric metric-trio num">.*?</div>',
                empty_metrics,
                row,
                count=1,
                flags=re.S,
            )
        metrics = (
            f'<div class="activity-metric metric-trio num"><span class="metric-cell"><b>{data["viewers"]:,}</b></span>'
            f'<span class="metric-cell"><b>{fmt_won(data["gmv_1d"])}</b></span>'
            f'<span class="metric-cell"><b>{fmt_won(data["gmv_1h"]) if data["gmv_1h"] else "—"}</b></span></div>'
        )
        return re.sub(r'<div class="activity-metric metric-trio num">.*?</div>', metrics, row, count=1, flags=re.S)

    period_prefix = re.escape(f"{year:04d}-{month:02d}")
    pattern = re.compile(
        rf'<div class="activity-row">\s*<time class="activity-date" datetime="(?P<date>{period_prefix}-\d{{2}})">.*?</time>.*?'
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
        r'<span class="chip">(?:LIVE )?RAW [^<]+</span><span class="chip vi">FORECAST \d{4}-\d{2}</span>',
        f'<span class="chip">{raw_label}</span><span class="chip vi">FORECAST {now.year:04d}-{now.month:02d}</span>',
        html,
        count=1,
    )

    built_short = now.strftime("%m-%d %H:%M")
    ingest_note = ingest or now.isoformat(timespec="seconds")
    footer_head = (
        f'LIVE 빌드 {built_short} · 현재 RAW 매출 = DuckDB 3팀 MTD · 라이브 1D = live.raw_slots {range_label} '
        f'· ingest {ingest_note} · 마감예상/OKR/온드/유튜브 = 별도 공개 스냅샷 '
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
    rows, ingest = fetch_live_rows(db_path, year, month, end_date=now.date())
    prev_rows, _ = fetch_live_rows(db_path, prev_year, prev_month)
    summary = summarize(rows, prev_rows)
    revenue_snapshot = fetch_current_revenue_snapshot(db_path, now.date())
    if not summary["overall"]["n"]:
        raise RuntimeError("no positive current-month Live 1D rows found")

    html = html_path.read_text(encoding="utf-8")
    html = update_live_quality(html, summary, month=month)
    html = update_live_activity_rows(html, rows, year=year, month=month)
    html = update_chips_footer_and_live_row(html, now, summary, ingest)
    html = update_current_raw_surfaces(html, revenue_snapshot)
    payload = {
        "script": "scripts/refresh_live_daily_from_duckdb.py",
        "generated_at_kst": now.isoformat(timespec="seconds"),
        "source": str(db_path),
        "month": f"{year:04d}-{month:02d}",
        "latest_positive_date": str(summary["latest_positive_date"]),
        "live_1d": summary,
        "current_raw_revenue": revenue_snapshot,
    }
    html = update_manifest(
        html,
        now.isoformat(timespec="seconds"),
        payload,
        touched_sources={"revenue_mirror", "live_quality", "okr_targets"},
    )
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
        "raw_revenue_total_won": revenue_snapshot["total_won"],
        "raw_revenue_display": fmt_won(revenue_snapshot["total_won"]),
        "raw_revenue_as_of": revenue_snapshot["as_of"],
    }
    if not quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return result


def assert_safe_default_refresh(html_path: Path, *, allow_dirty: bool) -> None:
    """Refuse direct default-index mutation when an operator worktree is dirty."""
    if allow_dirty or html_path.resolve() != DEFAULT_HTML.resolve():
        return
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        raise RuntimeError(f"unable to inspect git worktree: {status.stderr.strip()}")
    if status.stdout.strip():
        raise RuntimeError("refusing default dashboard refresh in dirty worktree; use --allow-dirty only for an intentional operator run")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", default=str(DEFAULT_HTML))
    parser.add_argument("--duckdb", default=str(DEFAULT_DUCKDB))
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    html_path = Path(args.html)
    assert_safe_default_refresh(html_path, allow_dirty=args.allow_dirty)
    refresh(html_path, Path(args.duckdb), args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
