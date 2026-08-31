#!/usr/bin/env python3
"""Refresh the Owned Media / YouTube detail window from local YouTube DuckDB.

Default view is current-month cumulative YouTube Analytics. Weekly rows are
shown separately so the overlay is not frozen to one historical week.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import hashlib
import html as html_lib
import json
import math
import re
from pathlib import Path

import duckdb

KST = dt.timezone(dt.timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HTML = ROOT / "index.html"
DEFAULT_CONTRACT = ROOT / "data" / "owned_youtube_window_contract.json"
DEFAULT_YT_DB = Path("/Users/sb.lee/automations/youtube-view-snapshot/youtube_views.duckdb")


def esc(value) -> str:
    return html_lib.escape(str(value or ""), quote=True)


def ensure_date(value) -> dt.date:
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def fmt_m_d(day: dt.date | None) -> str:
    if not day:
        return "확인중"
    return f"{day.month}/{day.day}"


def fmt_range(start: dt.date | None, end: dt.date | None) -> str:
    if not start or not end:
        return "확인중"
    return f"{fmt_m_d(start)}–{fmt_m_d(end)}"


def fmt_count(value: float | int | None) -> str:
    value = float(value or 0)
    if abs(value) >= 10_000_000:
        text = f"{value / 10_000:.0f}만"
        return text
    if abs(value) >= 100_000:
        return f"{round(value / 10_000):,}만"
    if abs(value) >= 10_000:
        text = f"{value / 10_000:.1f}".rstrip("0").rstrip(".")
        return f"{text}만"
    return f"{round(value):,}"


def fmt_num(value: float | int | None) -> str:
    return f"{int(value or 0):,}"


def metric(label: str, value: str, em: str) -> str:
    return f'<div class="yt-kpi"><small>{esc(label)}</small><b>{esc(value)}</b><em>{esc(em)}</em></div>'


def fetch_month(con: duckdb.DuckDBPyConnection, year: int, month: int) -> dict:
    row = con.execute(
        """
        select period_start, period_end, metric_start_date, metric_end_date,
               period_complete, channel_view_count, channel_like_count,
               channel_comment_count, channel_share_count, channel_engagement_count,
               new_published_view_count, prior_published_view_count,
               unknown_publish_view_count, fetched_at, raw_status
        from v_youtube_monthly_analytics
        where period_start = ?
        order by fetched_at desc
        limit 1
        """,
        [dt.date(year, month, 1)],
    ).fetchone()
    if not row:
        raise RuntimeError(f"monthly YouTube analytics row not found for {year}-{month:02d}")
    keys = [
        "period_start", "period_end", "metric_start_date", "metric_end_date",
        "period_complete", "views", "likes", "comments", "shares", "engagement",
        "new_views", "prior_views", "unknown_views", "fetched_at", "raw_status",
    ]
    out = dict(zip(keys, row))
    for key in ("period_start", "period_end", "metric_start_date", "metric_end_date"):
        out[key] = ensure_date(out[key])
    return out


def fetch_weeks(con: duckdb.DuckDBPyConnection, month_start: dt.date, month_end: dt.date) -> list[dict]:
    rows = con.execute(
        """
        select period_start, period_end, metric_start_date, metric_end_date,
               period_complete, channel_view_count, channel_like_count,
               channel_comment_count, channel_share_count, channel_engagement_count,
               new_published_view_count, prior_published_view_count,
               unknown_publish_view_count, raw_status
        from v_youtube_weekly_analytics
        where period_start <= ? and period_end >= ?
        order by period_start
        """,
        [month_end, month_start],
    ).fetchall()
    keys = [
        "period_start", "period_end", "metric_start_date", "metric_end_date",
        "period_complete", "views", "likes", "comments", "shares", "engagement",
        "new_views", "prior_views", "unknown_views", "raw_status",
    ]
    out = []
    for row in rows:
        rec = dict(zip(keys, row))
        for key in ("period_start", "period_end", "metric_start_date", "metric_end_date"):
            rec[key] = ensure_date(rec[key])
        out.append(rec)
    return out


def fetch_publish_counts(con: duckdb.DuckDBPyConnection, start: dt.date, end: dt.date) -> dict:
    rows = con.execute(
        """
        select coalesce(form, '확인중') as form, count(*)
        from dim_video
        where publish_date >= ? and publish_date <= ? and is_active
        group by 1
        """,
        [start, end],
    ).fetchall()
    counts = {str(form): int(count) for form, count in rows}
    counts["total"] = sum(counts.values())
    return counts


def fetch_top_content(con: duckdb.DuckDBPyConnection, start: dt.date, end: dt.date, limit: int = 6) -> list[dict]:
    rows = con.execute(
        """
        with ranked as (
          select *, row_number() over(partition by video_id order by d_plus_n desc) as rn
          from v_public_dplusn_video
          where publish_date >= ? and publish_date <= ? and complete
        )
        select d_plus_n, video_id, publish_date, form, ip, title, url, views
        from ranked
        where rn = 1
        order by views desc
        limit ?
        """,
        [start, end, limit],
    ).fetchall()
    keys = ["d_plus_n", "video_id", "publish_date", "form", "ip", "title", "url", "views"]
    out = []
    for row in rows:
        rec = dict(zip(keys, row))
        rec["publish_date"] = ensure_date(rec["publish_date"])
        out.append(rec)
    return out


def fetch_main_content(con: duckdb.DuckDBPyConnection, start: dt.date, end: dt.date) -> tuple[list[dict], dt.date | None]:
    rows = con.execute(
        """
        with latest_d7 as (
          select *, row_number() over(
            partition by video_id
            order by d7_complete desc, fetched_at desc, metric_end_date desc
          ) as rn
          from fact_analytics_d7
        )
        select d.publish_date, d.video_id, coalesce(d.form, '확인중') as form,
               d.title, d.url, s.cumulative_view_count, s.snapshot_date,
               case when a.d7_complete then a.view_count end as d7_views,
               case when a.d7_complete then
                 coalesce(a.like_count, 0) + coalesce(a.comment_count, 0) + coalesce(a.share_count, 0)
               end as pis,
               coalesce(a.d7_complete, false) as d7_complete
        from dim_video d
        left join v_latest_snapshot s using(video_id)
        left join latest_d7 a on a.video_id = d.video_id and a.rn = 1
        where d.is_active and d.publish_date between ? and ?
        order by d.publish_date, d.video_id
        """,
        [start, end],
    ).fetchall()
    keys = [
        "publish_date", "video_id", "form", "title", "url", "views_total",
        "snapshot_date", "d7_views", "pis", "d7_complete",
    ]
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        rec = dict(zip(keys, row))
        video_id = str(rec["video_id"])
        if video_id in seen:
            raise RuntimeError(f"duplicate current-month YouTube video {video_id}")
        seen.add(video_id)
        rec["publish_date"] = ensure_date(rec["publish_date"])
        if rec["snapshot_date"]:
            rec["snapshot_date"] = ensure_date(rec["snapshot_date"])
        out.append(rec)
    global_snapshot = con.execute("select max(snapshot_date) from fact_snapshot").fetchone()[0]
    return out, ensure_date(global_snapshot) if global_snapshot else None


def fetch_quality_series(
    con: duckdb.DuckDBPyConnection,
    year: int,
    through_month: int,
    *,
    as_of: dt.date | None = None,
) -> dict[int, dict]:
    range_start = dt.date(year - 1, 12, 1) if through_month == 1 else dt.date(year, 1, 1)
    range_end = _month_end(year, through_month)
    if as_of is not None:
        range_end = min(range_end, as_of)
    publish_rows = con.execute(
        """
        select year(publish_date) as year, month(publish_date) as month,
               coalesce(form, '확인중') as form, count(*)
        from dim_video
        where is_active and publish_date between ? and ?
        group by 1, 2, 3
        """,
        [range_start, range_end],
    ).fetchall()
    d7_rows = con.execute(
        """
        with latest_d7 as (
          select *, row_number() over(
            partition by video_id
            order by d7_complete desc, fetched_at desc, metric_end_date desc
          ) as rn
          from fact_analytics_d7
        )
        select year(d.publish_date) as year, month(d.publish_date) as month,
               coalesce(d.form, '확인중') as form,
               count(*) as completed, sum(a.view_count) as views
        from dim_video d
        join latest_d7 a on a.video_id = d.video_id and a.rn = 1
        where d.is_active and d.publish_date between ? and ?
          and a.d7_complete
        group by 1, 2, 3
        """,
        [range_start, range_end],
    ).fetchall()
    subscriber_rows = con.execute(
        """
        select year(snapshot_date) as year, month(snapshot_date) as month,
               subscriber_count, snapshot_date
        from v_channel_daily_subscribers
        where snapshot_date between ? and ?
        qualify row_number() over(
          partition by year(snapshot_date), month(snapshot_date)
          order by snapshot_date desc, captured_at desc
        ) = 1
        """,
        [range_start, range_end],
    ).fetchall()

    def empty_record() -> dict:
        return {
            "published": 0,
            "LF_count": 0,
            "SF_count": 0,
            "completed": 0,
            "LF_completed": 0,
            "SF_completed": 0,
            "view_sum": 0,
            "LF_sum": 0,
            "SF_sum": 0,
            "overall": 0,
            "LF": 0,
            "SF": 0,
            "subscriber": None,
            "subscriber_date": None,
        }

    series: dict[int, dict] = {month: empty_record() for month in range(1, through_month + 1)}
    if through_month == 1:
        series[0] = empty_record()

    def series_key(row_year: int, row_month: int) -> int:
        if through_month == 1 and int(row_year) == year - 1 and int(row_month) == 12:
            return 0
        return int(row_month)

    for row_year, month, form, count in publish_rows:
        rec = series[series_key(row_year, month)]
        rec["published"] += int(count)
        if form in ("LF", "SF"):
            rec[f"{form}_count"] += int(count)
    for row_year, month, form, completed, views in d7_rows:
        rec = series[series_key(row_year, month)]
        rec["completed"] += int(completed)
        rec["view_sum"] += int(views or 0)
        if form in ("LF", "SF"):
            rec[f"{form}_completed"] += int(completed)
            rec[f"{form}_sum"] += int(views or 0)
    for row_year, month, subscriber, snapshot_date in subscriber_rows:
        rec = series[series_key(row_year, month)]
        rec["subscriber"] = int(subscriber) if subscriber is not None else None
        rec["subscriber_date"] = ensure_date(snapshot_date) if snapshot_date else None
    for rec in series.values():
        rec["overall"] = round(rec["view_sum"] / rec["completed"]) if rec["completed"] else 0
        for form in ("LF", "SF"):
            completed = rec[f"{form}_completed"]
            rec[form] = round(rec[f"{form}_sum"] / completed) if completed else 0
    return series


def month_block_bounds(html: str, month: int) -> tuple[int, int]:
    marker = f'<div class="mvr mv" data-m="{month}"'
    start = html.index(marker)
    if month < 12:
        end = html.index(f'<div class="mvr mv" data-m="{month + 1}"', start)
    else:
        end = html.index('<section id="youtubeWindow"', start)
    return start, end


def _month_end(year: int, month: int) -> dt.date:
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def _activity_row(item: dict) -> str:
    day = ensure_date(item["publish_date"])
    title = item.get("title") or item.get("video_id") or "제목 확인중"
    form = item.get("form") or "확인중"
    d7_complete = bool(item.get("d7_complete"))
    d7 = fmt_num(item.get("d7_views")) if d7_complete else "—"
    pis = fmt_num(item.get("pis")) if d7_complete else "—"
    state = "" if d7_complete else " · D+7 수집중"
    return f'''<div class="activity-row">
              <time class="activity-date" datetime="{day.isoformat()}">{day.month}/{day.day}</time>
              <div class="activity-main activity-main-inline"><span class="activity-title-line"><a class="content-link" data-content-link="youtube" href="{esc(item.get('url'))}" target="_blank" rel="noopener">{esc(title)}<span aria-hidden="true">↗</span></a><small class="activity-inline-meta">{esc(form + state)}</small></span></div>
              <div class="activity-metric metric-trio num"><span class="metric-cell"><b>{esc(fmt_num(item.get('views_total')) if item.get('views_total') is not None else '—')}</b></span><span class="metric-cell"><b>{esc(d7)}</b></span><span class="metric-cell"><b>{esc(pis)}</b></span></div></div>'''


def render_main_ledger(*, year: int, month: int, as_of: dt.date, rows: list[dict], snapshot_date: dt.date | None) -> str:
    month_end = _month_end(year, month)
    period_end = min(month_end, as_of) if as_of >= dt.date(year, month, 1) else month_end
    elapsed_weeks = max(1, math.ceil(period_end.day / 7))
    latest_publish = max((ensure_date(row["publish_date"]) for row in rows), default=None)
    completed = sum(1 for row in rows if row.get("d7_complete"))
    snapshot_label = fmt_m_d(snapshot_date) if snapshot_date else "확인중"
    latest_nonempty_week = max(((ensure_date(row["publish_date"]).day - 1) // 7 + 1 for row in rows), default=1)
    groups: list[str] = []
    for index in range(1, elapsed_weeks + 1):
        start_day = (index - 1) * 7 + 1
        end_day = min(index * 7, month_end.day)
        week_start = dt.date(year, month, start_day)
        week_end = dt.date(year, month, end_day)
        items = [row for row in rows if week_start <= ensure_date(row["publish_date"]) <= week_end]
        item_html = "".join(_activity_row(item) for item in items)
        if not item_html:
            item_html = '<div class="activity-empty">이 주차 발행 없음</div>'
        open_attr = " open" if index == latest_nonempty_week else ""
        groups.append(f'''<details class="week-group" data-week-group="{month}-{index}"{open_attr}>
          <summary data-week-toggle="{month}-{index}"><span class="week-label">{month}월 {index}주차 <small>{fmt_range(week_start, week_end)}</small></span>
          <span class="week-chevron" aria-hidden="true"></span></summary>
          <div class="week-items"><div class="activity-column-head" aria-label="지표 칼럼"><div class="activity-metric-head metric-trio"><span>누적조회수</span><span>D7 조회수</span><span>PIS</span></div></div>{item_html}</div></details>''')
    return (
        f'<div class="plan-note" data-yt-main-source-publish-count="{len(rows)}" '
        f'data-yt-main-source-latest-publish-date="{latest_publish.isoformat() if latest_publish else "none"}" '
        f'data-yt-main-source-snapshot-date="{snapshot_date.isoformat() if snapshot_date else "none"}" '
        f'data-yt-main-source-elapsed-weeks="{elapsed_weeks}">'
        f'<b>{month}월 발행 {len(rows)}건</b> · D+7 완료 {completed}건 · {esc(snapshot_label)} public 스냅샷</div>'
        + "".join(groups)
    )


def _mom_parts(current: float | int | None, previous: float | int | None) -> tuple[str, str]:
    if current is None or previous in (None, 0):
        return "flat", "MoM —"
    change = (float(current) / float(previous) - 1) * 100
    if abs(change) < 0.05:
        return "flat", "MoM 0.0%"
    if change > 0:
        return "up", f"MoM ▲ {abs(change):.1f}%"
    return "dn", f"MoM ▼ {abs(change):.1f}%"


def _qmom(current: float | int | None, previous: float | int | None) -> str:
    css, text = _mom_parts(current, previous)
    return f'<div class="qmom {css} num">{text}</div>'


def _target_from_block(block: str, month: int) -> int | None:
    match = re.search(rf"{month}월 목표 ([0-9,]+) 대비", block)
    return int(match.group(1).replace(",", "")) if match else None


def render_main_quality(block: str, month: int, series: dict[int, dict]) -> str:
    current = series.get(month, {})
    previous = series.get(month - 1, {})
    published = int(current.get("published", 0))
    completed = int(current.get("completed", 0))
    overall = int(current.get("overall", 0))
    target = _target_from_block(block, month)
    pills: list[str] = []
    if target and overall:
        css = "up" if overall >= target else "dn"
        pills.append(f'<span class="pill {css} num big">{month}월 목표 {target:,} 대비 {overall / target * 100:.0f}%</span>')
    mom_css, mom_text = _mom_parts(overall, previous.get("overall"))
    pills.append(f'<span class="pill {mom_css} num big" data-yt-quality-mom-main="{month}">{mom_text}</span>')
    subscriber = current.get("subscriber")
    subscriber_detail = (
        f'{fmt_m_d(current.get("subscriber_date"))} public 스냅샷'
        if current.get("subscriber_date") else "public 스냅샷 미적재"
    )

    def average_card(form: str) -> str:
        value = int(current.get(form, 0))
        done = int(current.get(f"{form}_completed", 0))
        count = int(current.get(f"{form}_count", 0))
        shown = fmt_num(value) if value else "—"
        detail = f"D+7 완료 {done}/{count}건"
        return (
            f'<div class="qcell" data-yt-{form.lower()}-average-card="{month}"><div class="qk">{form} 평균</div>'
            f'<div class="qn num">{shown}</div><div class="qm2 num">{detail}</div>'
            f'{_qmom(value or None, previous.get(form))}</div>'
        )

    subscriber_source = str(subscriber) if subscriber is not None else "none"
    return f'''<div class="qsplit" data-yt-main-quality-basis="analytics-d7" data-yt-main-source-average-views="{overall}" data-yt-main-source-lf-average-views="{int(current.get('LF', 0))}" data-yt-main-source-sf-average-views="{int(current.get('SF', 0))}" data-yt-main-source-subscriber-count="{subscriber_source}" data-yt-main-source-d7-completed="{completed}"><div data-yt-main-average="{month}"><div class="qk2">전체 평균 조회수</div>
      <div class="qv num">{fmt_num(overall) if overall else '—'}</div><div class="qs">{''.join(pills)}</div>
      <div class="qmeta"><span>D+7 완료 {completed}/{published}건 · YouTube Analytics</span> · <a href="https://docs.google.com/spreadsheets/d/1lXIjLja-DEdBmDWDTM9LqNOG9UhVPCLS2B09InHQD90/edit?gid=673164445#gid=673164445" target="_blank" rel="noopener">MBD YT SSOT ↗</a></div></div><div><div class="qcells yt-qcells"><div class="qcell hero" data-yt-subscriber-card="{month}"><div class="qk">구독자</div><div class="qn num">{fmt_num(subscriber) if subscriber is not None else '—'}</div><div class="qm2 num">{subscriber_detail}</div>{_qmom(subscriber, previous.get('subscriber'))}</div><div class="qcell" data-yt-publish-card="{month}"><div class="qk">{month}월 발행</div><div class="qn num">{published}건</div><div class="qm2 num">SF {int(current.get('SF_count', 0))}건 · LF {int(current.get('LF_count', 0))}건</div>{_qmom(published, previous.get('published'))}</div><div class="qcell" data-yt-watch-duration-card="{month}"><div class="qk">평균 시청지속시간</div><div class="qn num">—</div><div class="qm2 num">Analytics 지속시간 미적재</div><div class="qmom flat num">MoM —</div></div>{average_card('LF')}{average_card('SF')}</div></div></div>'''


def render_quality_trend(series: dict[int, dict], focus_month: int, target: int | None) -> str:
    values = [
        int(series.get(month, {}).get(key, 0))
        for month in range(1, 13)
        for key in ("overall", "LF", "SF")
        if series.get(month, {}).get(key)
    ]
    if target:
        values.append(target)
    scale_max = max(values or [1]) * 1.08
    bottom = 174.0
    plot_height = 92.0

    def y(value: int | float) -> float:
        return bottom - float(value or 0) / scale_max * plot_height

    parts = [
        f'<div class="quality-trend" data-quality-trend="youtube-{focus_month}" data-quality-trend-kind="yt-format-average">',
        '<div class="qt-head"><b>월별 평균 추이 · LF/SF별 평균</b><span>D+7 Analytics · 평균치는 누적하지 않음 · square bar=LF/SF 평균 · 검은선=전체 평균</span></div>',
        '<svg viewBox="0 0 1060 210" role="img" aria-label="월별 평균 추이 · LF/SF별 평균">',
    ]
    for grid_y in (174.0, 128.0, 82.0):
        parts.append(f'<line class="qt-grid" x1="37" y1="{grid_y:.1f}" x2="1036" y2="{grid_y:.1f}"/>')
    points: list[tuple[float, float]] = []
    for month in range(1, 13):
        x = 93.0 + (month - 1) * 82.0
        rec = series.get(month)
        if month == focus_month:
            parts.append(f'<rect class="qt-focus" x="{x - 33:.1f}" y="27" width="66" height="158" rx="10"/>')
        overall = int(rec.get("overall", 0)) if rec else 0
        previous = int(series.get(month - 1, {}).get("overall", 0))
        top_class = "qt-top on" if month == focus_month else "qt-top"
        month_class = "qt-month on" if month == focus_month else "qt-month"
        top_text = fmt_count(overall) if overall else "—"
        if overall and previous:
            delta = overall - previous
            delta_class = "pos" if delta > 0 else "neg" if delta < 0 else "flat"
            delta_text = f"{'+' if delta > 0 else '−' if delta < 0 else ''}{fmt_count(abs(delta))}"
        else:
            delta_class = "flat"
            delta_text = "—" if rec else "예정"
        parts.append(f'<text class="{top_class}" x="{x:.1f}" y="24" text-anchor="middle">{top_text}</text>')
        parts.append(f'<text class="qt-delta {delta_class}" x="{x:.1f}" y="40" text-anchor="middle">{delta_text}</text>')
        if target and month >= focus_month:
            ty = y(target)
            parts.append(f'<line class="qt-target" x1="{x - 27:.1f}" y1="{ty:.1f}" x2="{x + 27:.1f}" y2="{ty:.1f}"/>')
        if rec:
            for offset, form, color in ((-16.5, "LF", "#2f64e9"), (1.5, "SF", "#9b7af4")):
                value = int(rec.get(form, 0))
                if not value:
                    continue
                top_y = y(value)
                height = max(1.0, bottom - top_y)
                parts.append(f'<rect class="qt-bar" x="{x + offset:.1f}" y="{top_y:.1f}" width="15" height="{height:.1f}" fill="{color}"/>')
                if height >= 18:
                    parts.append(f'<text class="qt-tiny" x="{x + offset + 7.5:.1f}" y="{top_y - 5:.1f}" text-anchor="middle">{fmt_count(value)}</text>')
            if overall:
                points.append((x, y(overall)))
        parts.append(f'<text class="{month_class}" x="{x:.1f}" y="196" text-anchor="middle">{month}월</text>')
    if points:
        path = " ".join(("M" if i == 0 else "L") + f"{x:.1f} {point_y:.1f}" for i, (x, point_y) in enumerate(points))
        parts.append(f'<path class="qt-avg-line" d="{path}"/>')
        parts.extend(f'<circle class="qt-avg-dot" cx="{x:.1f}" cy="{point_y:.1f}" r="4"/>' for x, point_y in points)
    current = series.get(focus_month, {})
    parts.append('</svg><div class="qt-legend"><span><i style="background:#2f64e9"></i>LF 평균</span><span><i style="background:#9b7af4"></i>SF 평균</span><span><i class="line"></i>전체 평균</span><span><i class="line" style="border-top-color:#64748B"></i>월 목표</span></div>')
    parts.append(f'<div class="qt-note">{focus_month}월 D+7 완료 {int(current.get("completed", 0))}/{int(current.get("published", 0))}건 · 현재월/미도래월은 해석 보수</div></div><!-- /quality-trend -->')
    return "".join(parts)


def update_main_youtube_surfaces(
    html: str,
    *,
    year: int,
    month: int,
    as_of: dt.date,
    snapshot_date: dt.date | None,
    rows: list[dict],
    quality_series: dict[int, dict],
) -> str:
    start, end = month_block_bounds(html, month)
    block = html[start:end]
    quality_start = block.index('<div class="card quality-card yt-quality">')
    qsplit_start = block.index('<div class="qsplit"', quality_start)
    trend_start = block.index(f'<div class="quality-trend" data-quality-trend="youtube-{month}"', qsplit_start)
    target = _target_from_block(block, month)
    block = block[:qsplit_start] + render_main_quality(block, month, quality_series) + "\n      " + block[trend_start:]
    trend_start = block.index(f'<div class="quality-trend" data-quality-trend="youtube-{month}"', quality_start)
    trend_end = block.index('<!-- /quality-trend -->', trend_start) + len('<!-- /quality-trend -->')
    block = block[:trend_start] + render_quality_trend(quality_series, month, target) + block[trend_end:]
    ledger_open = '<div class="content-ledger" data-content-ledger="youtube">'
    ledger_start = block.index(ledger_open, quality_start) + len(ledger_open)
    ledger_end = block.rfind('</div></div></div>')
    if ledger_end <= ledger_start:
        raise RuntimeError("current-month YouTube main ledger boundary not found")
    ledger = render_main_ledger(
        year=year,
        month=month,
        as_of=as_of,
        rows=rows,
        snapshot_date=snapshot_date,
    )
    block = block[:ledger_start] + ledger + block[ledger_end:]
    return html[:start] + block + html[end:]


def assert_main_parity(
    html: str,
    *,
    month: int,
    expected_published: int,
    expected_latest_publish_date: dt.date | None,
    expected_snapshot_date: dt.date | None,
    expected_elapsed_weeks: int,
) -> None:
    start, end = month_block_bounds(html, month)
    block = html[start:end]
    quality_start = block.index('<div class="card quality-card yt-quality">')
    yt_block = block[quality_start:]
    expected = {
        "publish count": str(expected_published),
        "latest publish date": expected_latest_publish_date.isoformat() if expected_latest_publish_date else "none",
        "snapshot date": expected_snapshot_date.isoformat() if expected_snapshot_date else "none",
        "elapsed weeks": str(expected_elapsed_weeks),
    }
    attrs = {
        "publish count": "data-yt-main-source-publish-count",
        "latest publish date": "data-yt-main-source-latest-publish-date",
        "snapshot date": "data-yt-main-source-snapshot-date",
        "elapsed weeks": "data-yt-main-source-elapsed-weeks",
    }
    for label, expected_value in expected.items():
        match = re.search(rf'{attrs[label]}="([^"]+)"', yt_block)
        actual = match.group(1) if match else None
        if actual != expected_value:
            raise RuntimeError(f"YouTube main {label} mismatch: rendered={actual!r} source={expected_value!r}")
    rendered_links = yt_block.count('data-content-link="youtube"')
    if rendered_links != expected_published:
        raise RuntimeError(
            f"YouTube main publish count mismatch: rendered rows={rendered_links} source={expected_published}"
        )
    if 'data-yt-main-quality-basis="analytics-d7"' not in yt_block:
        raise RuntimeError("YouTube main quality basis marker missing")
    rendered_weeks = {
        int(value)
        for value in re.findall(rf'data-week-group="{month}-(\d+)"', yt_block)
    }
    expected_weeks = set(range(1, expected_elapsed_weeks + 1))
    if rendered_weeks != expected_weeks:
        raise RuntimeError(
            f"YouTube main elapsed weeks mismatch: rendered={sorted(rendered_weeks)} "
            f"source={sorted(expected_weeks)}"
        )


def kpis_for_period(row: dict, publish_counts: dict | None = None) -> list[dict]:
    kpis = [
        {"label": "조회수", "value": fmt_count(row["views"]), "em": f"{fmt_range(row['metric_start_date'], row['metric_end_date'])}"},
        {"label": "당월/당주 발행 기여", "value": fmt_count(row["new_views"]), "em": "기간 내 발행 cohort"},
        {"label": "기발행 기여", "value": fmt_count(row["prior_views"]), "em": "기존 library cohort"},
        {"label": "인게이지먼트", "value": fmt_num(row["engagement"]), "em": f"좋아요+댓글+공유"},
        {"label": "좋아요", "value": fmt_num(row["likes"]), "em": f"댓글 {fmt_num(row['comments'])}"},
        {"label": "공유", "value": fmt_num(row["shares"]), "em": f"미매칭 {fmt_count(row['unknown_views'])}"},
    ]
    if publish_counts is not None:
        kpis.insert(1, {"label": "발행", "value": f"{publish_counts.get('total', 0)}건", "em": f"LF {publish_counts.get('LF', 0)} · SF {publish_counts.get('SF', 0)}"})
    return kpis


def render_kpis(items: list[dict]) -> str:
    return "".join(metric(item["label"], item["value"], item["em"]) for item in items)


def render_week(row: dict, index: int) -> str:
    complete = "완료" if row["period_complete"] and row["views"] else "수집중"
    label = f"{fmt_range(row['period_start'], row['period_end'])} · {complete}"
    if row["views"]:
        summary = f"조회 {fmt_count(row['views'])} · 당주 {fmt_count(row['new_views'])} · 기발행 {fmt_count(row['prior_views'])}"
    else:
        summary = "Analytics weekly partial 미반영 · 월누적에는 포함 가능"
    return f'''
        <details class="week-group yt-week-group" data-yt-week-group="{index}"{'' if index != 1 else ' open'}>
          <summary data-week-toggle="yt-{index}"><span class="week-label">온드 {index}주차 <small>{esc(label)} · {esc(summary)}</small></span><span class="week-chevron" aria-hidden="true"></span></summary>
          <div class="week-items"><div class="yt-kpi-strip num yt-week-kpis">{render_kpis(kpis_for_period(row))}</div></div>
        </details>'''


def render_content_card(item: dict) -> str:
    tier = f"D+{item['d_plus_n']}"
    title = item["title"] or item["video_id"]
    meta = f"{item['form']} · {item['ip']} · {fmt_m_d(item['publish_date'])}"
    return f'''
        <article class="yt-weekly-card" data-yt-weekly-card="mtd-{esc(item['video_id'])}">
          <div class="yt-weekly-title"><div><b><a class="content-link" data-content-link="youtube" href="{esc(item['url'])}" target="_blank" rel="noopener">{esc(title)}<span aria-hidden="true">↗</span></a></b><small>{esc(meta)}</small></div><span class="yt-tier">{esc(tier)}</span></div>
          <div class="yt-weekly-metrics"><span><small>현재</small><b>{esc(fmt_num(item['views']))}</b></span><span><small>기준</small><b>public D+N</b></span><span><small>상태</small><b>complete</b></span></div>
          <div class="yt-card-note"><b>판정:</b> 월 누적 상세탭의 콘텐츠 참고 카드입니다. Highlight/Lowlight 판정은 동종 D+N 벤치마크가 있을 때만 별도 표시합니다.</div>
        </article>'''


def render_section(month: dict, weeks: list[dict], publish_counts: dict, top_content: list[dict], now: dt.datetime) -> tuple[str, dict]:
    m = month["period_start"].month
    metric_range = fmt_range(month["metric_start_date"], month["metric_end_date"])
    status = "확정" if month["period_complete"] else "진행 중"
    content_cards = "".join(render_content_card(item) for item in top_content)
    week_blocks = "".join(render_week(row, i + 1) for i, row in enumerate(weeks))
    section = f'''<section id="youtubeWindow" class="yt-window" data-yt-window="weekly-detail" data-yt-period="mtd" data-owned-media-window="youtube" data-yt-window-latest-date="{esc(str(month['metric_end_date']))}" role="dialog" aria-modal="false" aria-labelledby="youtubeWindowTitle" aria-hidden="true">
  <div class="yt-window-bar">
    <div class="yt-window-title"><b id="youtubeWindowTitle">온드미디어 상세탭</b><small>디폴트 금월 누적 · 주차별 보기 · YouTube Analytics 기준</small></div>
    <button class="yt-window-close" type="button" data-yt-close>대시보드로 돌아가기</button>
  </div>
  <div class="yt-window-body">
    <div class="yt-hero">
      <div class="yt-hero-main" aria-label="{m}월 온드미디어 금월 누적 요약">
        <span class="yt-eyebrow">{m}월 금월 누적 · {esc(metric_range)} · {esc(status)}</span>
        <h2>온드미디어도 금월 누적이 디폴트,<br>주차별로 쪼개서 확인</h2>
        <p><b>YouTube 조회수 {esc(fmt_count(month['views']))}</b>까지 반영했습니다. 상단은 월누적 Analytics, 아래는 주차별 Analytics row입니다. 콘텐츠 카드는 public D+N 참고이며, 월/주차 headline과 섞어 판정하지 않습니다.</p>
        <div class="yt-kpi-strip num" aria-label="온드미디어 금월 누적 핵심 지표">{render_kpis(kpis_for_period(month, publish_counts))}</div>
      </div>
      <aside class="yt-hero-side" aria-label="온드미디어 기준">
        <div class="yt-window-section-title">기준 분리</div>
        <div class="yt-bench-list">
          <div class="yt-bench-row"><b>디폴트</b><small>금월 누적: YouTube Analytics API period row, metric_end={esc(str(month['metric_end_date']))}.</small></div>
          <div class="yt-bench-row"><b>주차별 보기</b><small>weekly Analytics row. 현재 주차가 0/partial이면 월누적과 별도로 수집중 표시.</small></div>
          <div class="yt-bench-row"><b>콘텐츠 카드</b><small>public D+N complete snapshot. Highlight/Lowlight는 벤치마크가 있을 때만 별도 판정.</small></div>
        </div>
      </aside>
    </div>
    <div class="yt-board">
      <div class="yt-panel" aria-label="온드 월누적 판단">
        <h3>이번 달 누적 판단</h3>
        <div class="yt-insights">
          <div class="yt-insight"><span><b>월 누적과 주차별을 분리.</b> 이전처럼 특정 1주차 D+N 창으로 고정하지 않고, 디폴트는 {esc(metric_range)} 월누적입니다.</span></div>
          <div class="yt-insight"><span><b>당월 발행 vs 기발행 분리.</b> 당월 발행 기여 {esc(fmt_count(month['new_views']))}, 기발행 기여 {esc(fmt_count(month['prior_views']))}로 cohort를 분리합니다.</span></div>
          <div class="yt-insight"><span><b>partial 표시.</b> {esc(status)} 월은 metric_end까지의 누적이며, 월말 확정 전에는 확정 수치로 말하지 않습니다.</span></div>
        </div>
      </div>
      <div class="yt-panel" aria-label="온드 주차 요약">
        <h3>주차별 요약</h3>
        <div class="live-next" data-yt-week-summary="{m}">{''.join(f'<div class="step"><b>{fmt_range(w["period_start"], w["period_end"])}</b><small>조회 {fmt_count(w["views"])} · 당주 {fmt_count(w["new_views"])} · 기발행 {fmt_count(w["prior_views"])} · {"완료" if w["period_complete"] and w["views"] else "수집중"}</small></div>' for w in weeks)}</div>
      </div>
    </div>
    <div class="yt-panel yt-weekly-panel" aria-label="온드 주차별 상세">
      <div class="yt-weekly-head"><h3>주차별 보기</h3><small>Weekly = 월~일 KST. headline은 Analytics, 콘텐츠 카드는 public D+N 참고.</small></div>
      <div class="yt-weekly-accordion" data-yt-weekly-view="{m}">{week_blocks}</div>
    </div>
    <div class="yt-panel yt-weekly-panel" aria-label="D+N 콘텐츠 참고">
      <div class="yt-weekly-head"><h3>콘텐츠 D+N 참고</h3><small>완료된 public D+N snapshot만 표시. 월누적 headline과 기준을 섞지 않음.</small></div>
      <div class="yt-card-grid num">{content_cards}</div>
    </div>
    <div class="yt-source"><b>데이터 기준:</b> YouTube Analytics API monthly/weekly period rows + public D+N snapshot · fetched_at {esc(str(month['fetched_at']))} · raw_status {esc(month['raw_status'])}</div>
  </div>
</section>'''
    contract = {
        "contract_id": f"owned-youtube-window-{month['period_start'].year}-{m:02d}-mtd-v1",
        "source": {
            "duckdb": str(DEFAULT_YT_DB),
            "monthly_period": f"{month['metric_start_date']}~{month['metric_end_date']}",
            "period_complete": bool(month["period_complete"]),
            "raw_status": month["raw_status"],
        },
        "required_copy": [
            "온드미디어 상세탭", "디폴트 금월 누적", "주차별 보기", "YouTube Analytics 기준",
            "data-yt-period=\"mtd\"", "data-owned-media-window=\"youtube\"",
            f"{m}월 금월 누적", "월 누적과 주차별을 분리", "당월 발행 vs 기발행 분리",
            "콘텐츠 D+N 참고", "public D+N snapshot",
        ],
        "forbidden_in_youtube_window": [
            "LF 비포애프터가 주간 성장 대부분", "public snapshot 기준 2026-08-11 23:57 KST",
            "data-yt-weekly-card=\"beforeafter-lf-ep91\"", "data-yt-weekly-card=\"nationhome-lf-ep9\"",
        ],
        "hero_kpis": kpis_for_period(month, publish_counts),
        "weekly_rows": [
            {"period": fmt_range(w["period_start"], w["period_end"]), "views": w["views"], "complete": bool(w["period_complete"])}
            for w in weeks
        ],
        "content_cards": [item["video_id"] for item in top_content],
    }
    return section, contract


def replace_section(html: str, section: str) -> str:
    start = html.index('<section id="youtubeWindow"')
    end = html.index('\n<section id="liveWindow"', start)
    return html[:start] + section + html[end:]


def update_manifest_sources(
    html: str,
    built: str,
    *,
    payload: dict,
    source_as_of: dict[str, str],
    default_month: int | None = None,
) -> str:
    manifest_re = re.compile(
        r'(<script type="application/json" id="mbd-public-guard">)(.*?)(</script>)',
        re.S,
    )
    match = manifest_re.search(html)
    if not match:
        raise RuntimeError("mbd-public-guard manifest not found")
    manifest = json.loads(match.group(2))
    manifest["built_at_kst"] = built
    if default_month is not None:
        if not 1 <= default_month <= 12:
            raise ValueError(f"default_month out of range: {default_month}")
        manifest["default_month"] = default_month
    stamps = manifest.get("source_snapshot_as_of", {})
    for key in ("yt_quality", "owned_media"):
        if key not in stamps:
            raise RuntimeError(f"unknown manifest source timestamp {key}")
        value = source_as_of.get(key)
        if not value:
            raise RuntimeError(f"source timestamp missing for {key}")
        stamps[key] = value
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    manifest["source_payload_sha256"] = hashlib.sha256(payload_bytes).hexdigest()
    raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    return manifest_re.sub(lambda m: m.group(1) + raw + m.group(3), html, count=1)


def _source_iso(value: dt.datetime | None) -> str:
    if value is None:
        raise RuntimeError("YouTube source timestamp is missing")
    if value.tzinfo is None:
        value = value.replace(tzinfo=KST)
    return value.astimezone(KST).isoformat(timespec="seconds")


def fetch_source_as_of(con: duckdb.DuckDBPyConnection, monthly_fetched_at: dt.datetime) -> dict[str, str]:
    d7_fetched_at = con.execute("select max(fetched_at) from fact_analytics_d7").fetchone()[0]
    subscriber_captured_at = con.execute("select max(captured_at) from fact_channel_snapshot").fetchone()[0]
    public_captured_at = con.execute("select max(captured_at) from fact_snapshot").fetchone()[0]
    return {
        "yt_quality": _source_iso(min(monthly_fetched_at, d7_fetched_at, subscriber_captured_at)),
        "owned_media": _source_iso(min(monthly_fetched_at, public_captured_at)),
    }


def refresh(html_path: Path, contract_path: Path, db_path: Path, quiet: bool = False) -> dict:
    now = dt.datetime.now(KST)
    current_start = dt.date(now.year, now.month, 1)
    current_end = min(now.date(), _month_end(now.year, now.month))
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        month = fetch_month(con, now.year, now.month)
        weeks = fetch_weeks(con, month["period_start"], month["period_end"])
        publish_counts = fetch_publish_counts(con, current_start, current_end)
        top_content = fetch_top_content(con, month["metric_start_date"], month["metric_end_date"])
        main_rows, snapshot_date = fetch_main_content(con, current_start, current_end)
        quality_series = fetch_quality_series(con, now.year, now.month, as_of=now.date())
        source_as_of = fetch_source_as_of(con, month["fetched_at"])
    finally:
        con.close()
    if publish_counts.get("total", 0) != len(main_rows):
        raise RuntimeError(
            f"YouTube current-month source mismatch: publish_counts={publish_counts.get('total', 0)} rows={len(main_rows)}"
        )
    section, contract = render_section(month, weeks, publish_counts, top_content, now)
    contract["source"]["source_as_of"] = source_as_of
    latest_publish = max((row["publish_date"] for row in main_rows), default=None)
    elapsed_weeks = max(1, math.ceil(now.day / 7))
    current_quality = quality_series.get(now.month, {})
    previous_quality = quality_series.get(now.month - 1, {})
    if int(current_quality.get("published", 0)) != len(main_rows):
        raise RuntimeError(
            "YouTube current-month quality publish count mismatch: "
            f"quality={current_quality.get('published', 0)} rows={len(main_rows)}"
        )
    if int(current_quality.get("LF_count", 0)) + int(current_quality.get("SF_count", 0)) != len(main_rows):
        raise RuntimeError("YouTube current-month LF/SF publish split does not reconcile")
    contract["main_surface"] = {
        "year": now.year,
        "month": now.month,
        "publish_count": len(main_rows),
        "lf_publish_count": int(current_quality.get("LF_count", 0)),
        "sf_publish_count": int(current_quality.get("SF_count", 0)),
        "latest_publish_date": str(latest_publish) if latest_publish else None,
        "snapshot_date": str(snapshot_date) if snapshot_date else None,
        "elapsed_weeks": elapsed_weeks,
        "quality_basis": "analytics-d7",
        "d7_completed": int(current_quality.get("completed", 0)),
        "average_views": int(current_quality.get("overall", 0)),
        "lf_average_views": int(current_quality.get("LF", 0)),
        "sf_average_views": int(current_quality.get("SF", 0)),
        "subscriber_count": current_quality.get("subscriber"),
        "previous_average_views": int(previous_quality.get("overall", 0)),
    }
    html = html_path.read_text(encoding="utf-8")
    updated = replace_section(html, section)
    updated = update_main_youtube_surfaces(
        updated,
        year=now.year,
        month=now.month,
        as_of=now.date(),
        snapshot_date=snapshot_date,
        rows=main_rows,
        quality_series=quality_series,
    )
    assert_main_parity(
        updated,
        month=now.month,
        expected_published=len(main_rows),
        expected_latest_publish_date=latest_publish,
        expected_snapshot_date=snapshot_date,
        expected_elapsed_weeks=elapsed_weeks,
    )
    updated = update_manifest_sources(
        updated,
        now.isoformat(timespec="seconds"),
        payload=contract,
        source_as_of=source_as_of,
        default_month=now.month,
    )
    html_changed = updated != html
    contract_text = json.dumps(contract, ensure_ascii=False, indent=2, default=str) + "\n"
    old_contract = contract_path.read_text(encoding="utf-8") if contract_path.exists() else ""
    contract_changed = old_contract != contract_text
    if html_changed:
        html_path.write_text(updated, encoding="utf-8")
    if contract_changed:
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(contract_text, encoding="utf-8")
    result = {
        "html_changed": html_changed,
        "contract_changed": contract_changed,
        "metric_end_date": str(month["metric_end_date"]),
        "views": month["views"],
        "published": publish_counts,
        "weeks": len(weeks),
        "content_cards": len(top_content),
        "main_publish_count": len(main_rows),
        "main_latest_publish_date": str(latest_publish) if latest_publish else None,
        "main_snapshot_date": str(snapshot_date) if snapshot_date else None,
        "main_elapsed_weeks": elapsed_weeks,
        "main_d7_completed": current_quality.get("completed", 0),
        "source_as_of": source_as_of,
    }
    if not quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", default=str(DEFAULT_HTML))
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument("--duckdb", default=str(DEFAULT_YT_DB))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    refresh(Path(args.html), Path(args.contract), Path(args.duckdb), quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
