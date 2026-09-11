"""Private, derived bot cache from the exact dashboard snapshots. Never publish JSON.

No source writes. This file emits only allowlisted metrics, not raw sheet records,
costs, margins, people or notes. index.html's byte hash binds the cache to a release.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path

import duckdb
from dashboard_forecast_state import fetch_forecast
from refresh_live_daily_from_duckdb import fetch_current_revenue_snapshot
from refresh_owned_youtube_window_from_duckdb import fetch_main_content

SCHEMA = "mbd-bot-metrics-v1"
KST = dt.timezone(dt.timedelta(hours=9))


def query(con, sql, args=()):
    cur = con.execute(sql, args)
    return [dict(zip([x[0] for x in cur.description], row)) for row in cur.fetchall()]


def export(mbd_path, yt_path, html_path, as_of):
    html = Path(html_path).read_bytes()
    manifest = json.loads(re.search(rb'<script type="application/json" id="mbd-public-guard">(.*?)</script>', html, re.S)[1])
    if int(manifest['default_month']) != as_of.month:
        raise ValueError('dashboard month differs from cache cutoff')
    start = dt.date(as_of.year, 1, 1)
    c = duckdb.connect(str(mbd_path), read_only=True)
    y = duckdb.connect(str(yt_path), read_only=True)
    try:
        live = query(c, r'''select try_cast("온에어 일자" as date) as date,
          coalesce("브랜드명", '') as brand, coalesce("패키지", '') as package,
          try_cast(regexp_replace(coalesce("방송별 데이터 GMV", ''), '[^0-9.-]', '', 'g') as bigint) as gmv,
          try_cast(regexp_replace(coalesce("일 전체 GMV (라이브 브랜드 전체)", ''), '[^0-9.-]', '', 'g') as bigint) as gmv_1d,
          try_cast(regexp_replace(coalesce("라이브 1H GMV", ''), '[^0-9.-]', '', 'g') as bigint) as gmv_1h,
          try_cast(regexp_replace(coalesce("라이브 시청자 (비로그인 포함)", ''), '[^0-9.-]', '', 'g') as bigint) as viewers,
          regexp_matches(lower(concat_ws(' ', "패키지", "PGM", "비고 (프로모션)")), '무상|무료|free') as free,
          coalesce("1P/3P", '') = '3P' as attributed,
          trim(coalesce("1P/3P", '')) = '' as unknown_party,
          try_cast(regexp_replace(coalesce("AF수취액", ''), '[^0-9.-]', '', 'g') as bigint) as af
          from live.raw_slots
          where try_cast("온에어 일자" as date) between ? and ?
          and not regexp_matches(lower(concat_ws(' ', "패키지", "PGM", "비고 (프로모션)")), '취소|cancel')
          order by 1,2,3''', [start, dt.date(as_of.year, 12, 31)])
        for row in live:
            if row['date'] > as_of:
                for key in ('gmv', 'gmv_1d', 'gmv_1h', 'viewers', 'af'):
                    row[key] = None
        months = {}
        for month in range(1, as_of.month + 1):
            cutoff = min(as_of, dt.date(as_of.year, month, calendar.monthrange(as_of.year, month)[1]))
            ym = cutoff.strftime('%Y-%m')
            raw = fetch_current_revenue_snapshot(mbd_path, cutoff, include_targets=False)
            actual = query(c, '''select revenue_team, sum(team_attributed_revenue) as won
              from revenue.integrated_ssot where revenue_month=? and include_in_mbd_revenue
              and revenue_team in ('일반광고','통합광고','라이브커머스') group by 1''', [ym])
            mapping = {'일반광고': 'ad_gen', '통합광고': 'ad_int', '라이브커머스': 'live'}
            actual_values = {mapping[r['revenue_team']]: round(r['won']) for r in actual}
            closed = bool(re.search(fr'class="mvk mv" data-m="{month}"[^>]*data-phase="closed"'.encode(), html))
            months[ym] = {'raw': raw, 'actual': actual_values if closed and len(actual_values) == 3 else None,
                          'closed': closed, 'forecast': None}
        for month in (as_of.month, as_of.month + 1):
            if month > 12:
                continue
            cutoff = dt.date(as_of.year, month, 1)
            f = fetch_forecast(mbd_path, cutoff)
            months.setdefault(cutoff.strftime('%Y-%m'), {})['forecast'] = f
        # Difference of the existing same-month MTD function, not a new revenue rule.
        daily = []
        previous = {k: 0 for k in ('ad_gen_won', 'ad_int_won', 'live_won')}
        for offset in range((as_of - start).days + 1):
            day = start + dt.timedelta(days=offset)
            if day.day == 1:
                previous = dict.fromkeys(previous, 0)
            raw = fetch_current_revenue_snapshot(mbd_path, day, include_targets=False)
            daily.append({'date': day, **{k: raw[k] - previous[k] for k in previous}})
            previous = {k: raw[k] for k in previous}
        periods = []
        for view in ('v_youtube_monthly_analytics', 'v_youtube_weekly_analytics'):
            periods += query(y, f'''select period_type, period_start, period_end, metric_start_date,
              metric_end_date, period_complete, channel_view_count as views,
              new_published_view_count as new_views, prior_published_view_count as prior_views,
              unknown_publish_view_count as residual, fetched_at, raw_status, error, source
              from {view} where period_start >= ? and period_start <= ? order by period_start''', [start, as_of])
        for row in periods:
            row['available'] = (row['raw_status'] == 'ok' and not row['error'] and
                'channel_views=standalone' in (row['source'] or '') and
                row['metric_start_date'] == row['period_start'] and
                row['metric_end_date'] is not None and row['metric_end_date'] <= min(row['period_end'], as_of) and
                (not row['period_complete'] or row['metric_end_date'] == row['period_end']) and
                all(type(row[k]) is int for k in ('views','new_views','prior_views','residual')) and
                row['views'] == row['new_views'] + row['prior_views'] + row['residual'])
            if not row['available']:
                for key in ('views', 'new_views', 'prior_views', 'residual'):
                    row[key] = None
            row.pop('error'); row.pop('source')
        content, snapshot = fetch_main_content(y, start, as_of)
        subscribers = query(y, '''select snapshot_date as date, subscriber_count as count
          from v_channel_daily_subscribers where snapshot_date between ? and ?
          qualify row_number() over(partition by snapshot_date order by captured_at desc)=1
          order by snapshot_date''', [start, as_of])
        # Copy only the already validated schedule cache, never raw H:O metrics.
        from youtube_schedule import load_snapshot, reconcile
        schedule_cache = load_snapshot(y, now=dt.datetime.now(KST))
        schedules = {}
        for month in range(1, as_of.month + 2):
            if month > 12:
                continue
            month_rows = [r for r in content if r['publish_date'].month == month]
            reconciled = reconcile(schedule_cache, month_rows, year=as_of.year, month=month, as_of=as_of,
                                   known_videos={r['video_id']: r for r in content})
            schedules[f'{as_of.year}-{month:02d}'] = reconciled
        result = {'schema': SCHEMA, 'as_of': as_of, 'built_at': dt.datetime.now(KST),
                  'coverage_start': (as_of.replace(day=1)-dt.timedelta(days=1)).replace(day=1),
                  'dashboard_sha256': hashlib.sha256(html).hexdigest(),
                  'source_as_of': manifest.get('source_snapshot_as_of', {}),
                  'revenue': {'months': months, 'days': daily}, 'live': live,
                  'youtube': {'periods': periods, 'content': content, 'snapshot_date': snapshot,
                              'subscribers': subscribers, 'schedules': schedules}}
        # Bind metric VALUES as well as the bytes: a fresh HTML hash alone is not parity.
        current = months[as_of.strftime('%Y-%m')]
        for name, value in [('current-total-won',current['raw']['total_won']),
                            ('forecast-total-won',current['forecast']['total_won'])]:
            if f'data-{name}="{value}"'.encode() not in html:
                raise ValueError('export differs from dashboard '+name)
        quality = [r for r in live if r['date'].month==as_of.month and r['date']<=as_of and not r['free'] and (r['gmv_1d'] or 0)>0]
        live_contract = json.loads((Path(html_path).resolve().parent/'data/live_window_contract.json').read_text())
        if live_contract['quality_summary']['count'] != len(quality) or live_contract['quality_summary']['gmv_1d'] != sum(r['gmv_1d'] for r in quality):
            raise ValueError('Live quality differs from dashboard contract')
        yt_contract = json.loads((Path(html_path).resolve().parent/'data/owned_youtube_window_contract.json').read_text())
        if yt_contract['schedule_coverage']['source_count'] != schedules[as_of.strftime('%Y-%m')]['coverage']['source_count']:
            raise ValueError('YouTube schedule differs from dashboard contract')
        if yt_contract['schedule_coverage'] != schedules[as_of.strftime('%Y-%m')]['coverage']:
            raise ValueError('YouTube schedule identities or capture differ')
        from refresh_live_daily_from_duckdb import fetch_snapshot_clock
        from refresh_owned_youtube_window_from_duckdb import (fetch_source_as_of,fetch_month,fetch_weeks,
            fetch_publish_counts,fetch_top_content,fetch_quality_series,render_section,replace_section,
            update_main_youtube_surfaces)
        clock=fetch_snapshot_clock(mbd_path,dt.datetime.now(KST))
        if clock['source_as_of'] != manifest['source_snapshot_as_of']['revenue_mirror'] or clock['as_of'] != as_of:
            raise ValueError('MBD source clock differs from dashboard')
        m=fetch_month(y,as_of.year,as_of.month)
        clocks=fetch_source_as_of(y,m['fetched_at'])
        if any(clocks[k] != manifest['source_snapshot_as_of'][k] for k in clocks):
            raise ValueError('YouTube source clock differs from dashboard')
        source_text=html.decode()
        from refresh_live_window_from_duckdb import fetch_rows,render_section as live_section,replace_live_section
        live_rows,ingest=fetch_rows(mbd_path,as_of.year,as_of.month)
        live_rendered,_=live_section(live_rows,dt.datetime.now(KST),ingest,mbd_path)
        if replace_live_section(source_text,live_rendered) != source_text:
            raise ValueError('Live detailed metrics differ from dashboard')
        for key in ('ad_gen','ad_int','live'):
            if f'data-current-raw-team="{key}" data-current-raw-won="{current["raw"][key+"_won"]}"' not in source_text:
                raise ValueError('RAW team differs from dashboard')
            if f'data-forecast-{key.replace("_","-")}-won="{current["forecast"][key]}"' not in source_text:
                raise ValueError('forecast team differs from dashboard')
        section,_=render_section(m,fetch_weeks(y,m['period_start'],m['period_end']),
            fetch_publish_counts(y,as_of.replace(day=1),as_of),
            fetch_top_content(y,m['metric_start_date'],m['metric_end_date']),dt.datetime.now(KST),db_path=yt_path)
        if replace_section(source_text,section) != source_text:
            raise ValueError('YouTube period metrics differ from dashboard')
        quality_series=fetch_quality_series(y,as_of.year,as_of.month,as_of=as_of)
        for month in range(max(1,as_of.month-1),as_of.month+1):
            cutoff=min(as_of,dt.date(as_of.year,month,calendar.monthrange(as_of.year,month)[1]))
            rows=[r for r in content if r['publish_date'].month==month]
            checked=update_main_youtube_surfaces(source_text,year=as_of.year,month=month,as_of=cutoff,
                snapshot_date=snapshot,rows=rows,quality_series=quality_series,
                schedule=schedules[f'{as_of.year}-{month:02d}'])
            if checked != source_text:
                raise ValueError('YouTube content/quality metrics differ from dashboard')
        # The read-back contracts must themselves belong to the HTML stage manifest.
        for stage,contract in [('owned_youtube',yt_contract),('live_window',live_contract)]:
            digest=hashlib.sha256(json.dumps(contract,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()
            if manifest['stage_payload_sha256'].get(stage)!=digest:
                raise ValueError('HTML/contract stage hash differs')
        return json.loads(json.dumps(result, ensure_ascii=False, default=str, allow_nan=False))
    finally:
        c.close(); y.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--mbd', required=True); p.add_argument('--youtube', required=True)
    p.add_argument('--html', default='index.html'); p.add_argument('--as-of', required=True)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    output = Path(args.output).resolve()
    root = Path(__file__).resolve().parents[1]
    if output == root or root in output.parents:
        raise ValueError('private metrics must be outside the public repository')
    payload = export(args.mbd, args.youtube, args.html, dt.date.fromisoformat(args.as_of))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.candidate.json')
    with open(temporary, 'w', encoding='utf-8', opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
        json.dump(payload, f, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    os.replace(temporary, output)
    print(json.dumps({'schema': SCHEMA, 'as_of': payload['as_of'], 'dashboard_sha256': payload['dashboard_sha256']}))


if __name__ == '__main__':
    main()
