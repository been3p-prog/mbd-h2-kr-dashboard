"""Reconcile the current month's complete public Live schedule from raw_slots.

The performance KPI population is deliberately unchanged. This ledger includes
non-cancelled free and future slots, but exposes only approved public fields.
"""
import calendar
import datetime as dt
import html
import math
import re

import duckdb


def _metric(value):
    if value is None or str(value).strip() in ('', '-', '—', 'None', 'nan'):
        return None
    try:
        number = float(str(value).replace(',', '').replace('원', '').strip())
        return int(number) if math.isfinite(number) and number >= 0 else None
    except (ValueError, OverflowError):
        return None


def fetch_schedule(db_path, year, month):
    start = dt.date(year, month, 1)
    end = dt.date(year, month, calendar.monthrange(year, month)[1])
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(r'''
            select try_cast("온에어 일자" as date), "브랜드명", "패키지", "PGM",
                   "라이브 시청자 (비로그인 포함)",
                   "일 전체 GMV (라이브 브랜드 전체)", "라이브 1H GMV",
                   regexp_matches(lower(concat_ws(' ', "패키지", "PGM", "비고 (프로모션)")), '무상|무료|free')
            from live.raw_slots
            where try_cast("온에어 일자" as date) between ? and ?
              and not regexp_matches(lower(concat_ws(' ', "패키지", "PGM", "비고 (프로모션)")), '취소|cancel')
            order by 1, 2, 3, 4
        ''', [start, end]).fetchall()
    finally:
        con.close()
    # Do not deduplicate by date/brand: separate broadcasts can share both.
    return [dict(date=d, brand=brand or '브랜드 확인중', package=package or '',
                 pgm=pgm or '', viewers=_metric(viewers), gmv_1d=_metric(day_gmv),
                 gmv_1h=_metric(hour_gmv), free=bool(free))
            for d, brand, package, pgm, viewers, day_gmv, hour_gmv, free in rows]


def update_schedule(document, rows, *, as_of, source_as_of=None):
    from refresh_live_daily_from_duckdb import _month_bounds, fmt_m_d, fmt_won

    year, month = as_of.year, as_of.month
    source_as_of = source_as_of or as_of
    month_end = calendar.monthrange(year, month)[1]
    for row in rows:
        if (row['date'].year, row['date'].month) != (year, month):
            raise RuntimeError('Live schedule contains a row outside the current month')
    rows = sorted(rows, key=lambda row: (row['date'], row['brand'], row['package'], row['pgm']))
    measured = sum(row['date'] <= as_of and (row['gmv_1d'] or 0) > 0 for row in rows)
    future = sum(row['date'] > as_of for row in rows)
    pending = len(rows) - measured - future
    latest = max((row['date'] for row in rows if row['date'] <= as_of and (row['gmv_1d'] or 0) > 0), default=None)
    note = (f'<div class="plan-note" data-live-main-source-count="{len(rows)}" '
            f'data-live-main-source-as-of="{as_of.isoformat()}" '
            f'data-live-main-source-snapshot-date="{source_as_of.isoformat()}" '
            f'data-live-main-source-measured="{measured}" '
            f'data-live-main-source-pending="{pending}" '
            f'data-live-main-source-future="{future}" '
            f'data-live-main-source-latest-result="{latest.isoformat() if latest else "none"}">'
            f'<b>{month}월 전체 편성 {len(rows)}건</b> · 실적 반영 {measured}건 · 집계 대기 {pending}건 · 예정 {future}건'
            f' · 편성 원천 {fmt_m_d(source_as_of)} 기준 / 실적 최신 {fmt_m_d(latest) if latest else "없음"}'
            ' · 취소 제외, 무료 편성 포함(품질 평균 제외)</div>')
    groups = []
    for week in range(1, math.ceil(month_end / 7) + 1):
        first, last = (week - 1) * 7 + 1, min(week * 7, month_end)
        items = []
        for row in rows:
            if not first <= row['date'].day <= last:
                continue
            is_future = row['date'] > as_of
            has_result = not is_future and (row['gmv_1d'] or 0) > 0
            state = '예정' if is_future else ('실적 반영' if has_result else '실적 집계 대기')
            meta = ' · '.join(filter(None, [row['package'], row['pgm'], state,
                                           '무료 · 품질 평균 제외' if row['free'] else '']))
            metrics = []
            for key in ('viewers', 'gmv_1d', 'gmv_1h'):
                value = row[key]
                # Future metrics must never leak into actuals; empty/placeholder
                # zero metrics on unmeasured slots remain explicitly unavailable.
                missing = is_future or value is None or (not has_result and value == 0)
                label = '—' if missing else (f'{value:,}' if key == 'viewers' else fmt_won(value))
                metrics.append(f'<span class="metric-cell"><b>{label}</b></span>')
            items.append('<div class="activity-row">'
                         f'<time class="activity-date" datetime="{row["date"].isoformat()}">{fmt_m_d(row["date"])}</time>'
                         '<div class="activity-main activity-main-inline"><span class="activity-title-line">'
                         f'<b class="content-title">{html.escape(row["brand"])}</b>'
                         f'<small class="activity-inline-meta">{html.escape(meta)}</small></span></div>'
                         '<div class="activity-metric metric-trio num">' + ''.join(metrics) + '</div></div>')
        body = ''.join(items) or '<div class="activity-empty">원천에 등록된 편성 없음</div>'
        groups.append(f'<details class="week-group" data-week-group="{month}-{week}" open>'
                      f'<summary data-week-toggle="{month}-{week}"><span class="week-label">{month}월 {week}주차 '
                      f'<small>{month}/{first}–{month}/{last}</small></span><span class="week-chevron" aria-hidden="true"></span></summary>'
                      '<div class="week-items"><div class="activity-column-head" aria-label="지표 칼럼">'
                      '<div class="activity-metric-head metric-trio"><span>시청자수</span><span>1D 거래액</span><span>1H 거래액</span></div></div>'
                      + body + '</div></details>')
    start, end = _month_bounds(document, 'mvr', month)
    block = document[start:end]
    marker = '<div class="content-ledger" data-content-ledger="live">'
    ledger_start = block.index(marker) + len(marker)
    ledger_end = block.index('<div class="card quality-card yt-quality">', ledger_start)
    if not block[:ledger_end].rstrip().endswith('</div></div>'):
        raise RuntimeError('Live ledger boundary changed; retain last-good dashboard')
    block = block[:ledger_start] + note + ''.join(groups) + '</div></div>' + block[ledger_end:]
    return document[:start] + block + document[end:]
