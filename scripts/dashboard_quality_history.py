"""Refresh late performance facts without reopening historical revenue closes."""
from __future__ import annotations

import calendar
import datetime as dt
import html as html_lib
import re
from pathlib import Path


def render_live_quality_trend(series: dict, month: int) -> str:
    from refresh_live_daily_from_duckdb import PACKAGE_LABELS, TARGET_WON, fmt_won

    keys = ("signature", "smart", "essential")
    colors = ("#2f64e9", "#1fb7a6", "#9b7af4")
    maximum = max([TARGET_WON] + [s[k]["avg"] or 0 for s in series.values() for k in ("overall", *keys)]) * 1.08
    y = lambda value: 174 - (value or 0) / maximum * 92
    current = series[month]
    parts = [f'<div class="quality-trend" data-quality-trend="live-{month}" data-quality-trend-kind="live-package-average" data-live-trend-source-average="{current["overall"]["avg"] or 0}">',
             '<div class="qt-head"><b>월별 평균 추이 · 패키지별 평균</b><span>평균치는 누적하지 않음 · square bar=패키지 평균 · 검은선=전체 평균</span></div>',
             '<svg viewBox="0 0 1060 210" role="img" aria-label="월별 평균 추이 · 패키지별 평균">']
    for grid_y in (174, 128, 82):
        parts.append(f'<line class="qt-grid" x1="37" y1="{grid_y}" x2="1036" y2="{grid_y}"/>')
    points = []
    for m in range(1, 13):
        x = 93 + (m - 1) * 82
        rec = series.get(m)
        avg = rec["overall"]["avg"] if rec else None
        previous = series.get(m - 1, {}).get("overall", {}).get("avg")
        if m == month:
            parts.append(f'<rect class="qt-focus" x="{x - 33}" y="27" width="66" height="158" rx="10"/>')
        suffix = " on" if m == month else ""
        parts.append(f'<text class="qt-top{suffix}" x="{x}" y="24" text-anchor="middle">{fmt_won(avg) if avg else "—"}</text>')
        delta = avg - previous if avg is not None and previous else None
        cls = "pos" if delta and delta > 0 else "neg" if delta and delta < 0 else "flat"
        delta_text = ("+" if delta > 0 else "−" if delta < 0 else "") + fmt_won(abs(delta)) if delta is not None else "—" if rec else "예정"
        parts.append(f'<text class="qt-delta {cls}" x="{x}" y="40" text-anchor="middle">{delta_text}</text>')
        if m >= month:
            parts.append(f'<line class="qt-target" x1="{x - 27}" y1="{y(TARGET_WON):.1f}" x2="{x + 27}" y2="{y(TARGET_WON):.1f}"/>')
        if rec:
            for i, key in enumerate(keys):
                value = rec[key]["avg"]
                if value:
                    top = y(value)
                    parts.append(f'<rect class="qt-bar" x="{x - 25 + i * 17}" y="{top:.1f}" width="15" height="{max(1, 174 - top):.1f}" fill="{colors[i]}"/>')
            if avg:
                points.append((x, y(avg)))
        parts.append(f'<text class="qt-month{suffix}" x="{x}" y="196" text-anchor="middle">{m}월</text>')
    if points:
        path = " ".join(("M" if i == 0 else "L") + f"{x} {point_y:.1f}" for i, (x, point_y) in enumerate(points))
        parts.append(f'<path class="qt-avg-line" d="{path}"/>')
        parts.extend(f'<circle class="qt-avg-dot" cx="{x}" cy="{point_y:.1f}" r="4"/>' for x, point_y in points)
    parts.append('</svg><div class="qt-legend">')
    parts.extend(f'<span><i style="background:{color}"></i>{PACKAGE_LABELS[key]} 평균</span>' for key, color in zip(keys, colors))
    parts.append('<span><i class="line"></i>전체 평균</span><span><i class="line" style="border-top-color:#64748B"></i>월 목표</span></div>')
    counts = " · ".join(f'{PACKAGE_LABELS[k]} n={current[k]["n"]}' for k in keys)
    parts.append(f'<div class="qt-note">{month}월 {counts} · 현재월/미도래월은 해석 보수</div></div><!-- /quality-trend -->')
    return "".join(parts)


def update_live_quality_summary(html: str, summary: dict, month: int, *, series: dict | None = None) -> str:
    from refresh_live_daily_from_duckdb import _month_bounds, update_live_quality

    html = update_live_quality(html, summary, month=month)
    start, end = _month_bounds(html, "mvr", month)
    block = html[start:end]
    card = block.index('<div class="card quality-card live-quality">')
    qsplit = block.index('<div class="qsplit"', card)
    tag_end = block.index('>', qsplit)
    tag = re.sub(r' data-live-quality-source-[a-z-]+="[^"]*"', '', block[qsplit:tag_end])
    overall = summary["overall"]
    tag += f' data-live-quality-source-count="{overall["n"]}" data-live-quality-source-total="{overall["sum"]}" data-live-quality-source-average="{overall["avg"] or 0}"'
    block = block[:qsplit] + tag + block[tag_end:]
    if series is not None:
        pattern = re.compile(rf'<div class="quality-trend" data-quality-trend="live-{month}".*?<!-- /quality-trend -->', re.S)
        block, count = pattern.subn(lambda _: render_live_quality_trend(series, month), block, count=1)
        if count != 1:
            raise RuntimeError(f"Live quality trend missing for month {month}")
    return html[:start] + block + html[end:]


def update_live_quality_history(html: str, db_path: Path, year: int, month: int, as_of: dt.date) -> str:
    from refresh_live_daily_from_duckdb import fetch_live_rows, summarize, update_live_activity_rows

    if (as_of.year, as_of.month) != (year, month):
        raise ValueError("quality period must match as_of")
    rows_by_month = {}
    prior = dt.date(year, 1, 1) - dt.timedelta(days=1)
    rows_by_month[0], _ = fetch_live_rows(db_path, prior.year, prior.month, end_date=prior)
    series = {}
    for m in range(1, month + 1):
        end = as_of if m == month else dt.date(year, m, calendar.monthrange(year, m)[1])
        rows_by_month[m], _ = fetch_live_rows(db_path, year, m, end_date=end)
        series[m] = summarize(rows_by_month[m], rows_by_month[m - 1])
    for m in range(max(1, month - 1), month + 1):
        html = update_live_quality_summary(html, series[m], m, series=series)
        html = update_live_activity_rows(html, rows_by_month[m], year=year, month=m)
        html = clear_ineligible_live_metrics(html, rows_by_month[m], year, m, as_of)
    return html


def clear_ineligible_live_metrics(html: str, rows: list[dict], year: int, month: int, as_of: dt.date) -> str:
    """Keep scheduled rows but never retain facts excluded by the quality query."""
    from refresh_live_daily_from_duckdb import _month_bounds

    start, end = _month_bounds(html, "mvr", month)
    block = html[start:end]
    live_start = block.index('<div class="card quality-card live-quality">')
    live_end = block.find('<div class="card quality-card yt-quality">', live_start)
    if live_end < 0:
        raise RuntimeError("Live quality ledger boundary missing")
    eligible = {(row["date"].isoformat(), row["brand"]) for row in rows if row["gmv_1d"] > 0}
    edits = []
    for match in re.finditer(r'<div class="activity-row">', block[live_start:live_end]):
        row_start = live_start + match.start()
        depth = 0
        row_end = None
        for token in re.finditer(r'<div\b[^>]*>|</div>', block[row_start:live_end]):
            depth += -1 if token.group() == '</div>' else 1
            if depth == 0:
                row_end = row_start + token.end()
                break
        if row_end is None:
            raise RuntimeError("Unterminated Live activity row")
        row = block[row_start:row_end]
        date = re.search(r'<time\b[^>]*datetime="(\d{4}-\d{2}-\d{2})"', row)
        if not date:
            raise RuntimeError("Live activity date missing")
        day = dt.date.fromisoformat(date[1])
        if (day.year, day.month) != (year, month) or day > as_of:
            continue
        brand = re.search(r'<a\b[^>]*data-content-link="live"[^>]*>(.*?)</a>|<b class="content-title">(.*?)</b>', row, re.S)
        name = html_lib.unescape(re.sub(r'<[^>]*>', '', (brand[1] or brand[2]) if brand else '')).replace('↗', '').strip()
        if (date[1], name) in eligible:
            continue
        empty = '<div class="activity-metric metric-trio num">' + '<span class="metric-cell"><b>—</b></span>' * 3 + '</div>'
        cleared, count = re.subn(r'<div class="activity-metric metric-trio num">.*?</div>', lambda _: empty, row, count=1, flags=re.S)
        if count != 1:
            raise RuntimeError("Live activity metric trio missing")
        edits.append((row_start, row_end, cleared))
    for row_start, row_end, cleared in reversed(edits):
        block = block[:row_start] + cleared + block[row_end:]
    return html[:start] + block + html[end:]
