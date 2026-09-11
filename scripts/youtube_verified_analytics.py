"""Verified API overlay in a disposable dashboard snapshot, never canonical DB.

The remote collector is stdout-only: existing host-local credentials are refreshed
in memory, not saved. Period headlines remain standalone queries, not daily sums.
Discovery never guesses a business format or matches an unassigned schedule title.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import subprocess
from pathlib import Path

CHANNEL = 'UCBKtitA1RwY7F32rCniV1dA'
KST = dt.timezone(dt.timedelta(hours=9))
SCHEMA = 'youtube-verified-api-v1'
FORMS = {'shorts': 'Shorts', 'videoOnDemand': '일반 동영상', 'liveStream': '라이브', 'posts': '게시물'}


def dates(start, end):
    return [(start + dt.timedelta(days=i)).isoformat() for i in range((end-start).days+1)]


def collect(yt, analytics, con, now):
    channels = yt.channels().list(part='id,contentDetails', mine=True).execute().get('items', [])
    if len(channels) != 1 or channels[0]['id'] != CHANNEL:
        raise ValueError('YouTube target channel mismatch')
    today = now.date()
    month_start = today.replace(day=1)
    previous = (month_start-dt.timedelta(days=1)).replace(day=1)
    start = previous-dt.timedelta(days=previous.weekday())
    requested_end = today-dt.timedelta(days=1)

    def query(a, b, metrics='views', **extra):
        return analytics.reports().query(ids='channel=='+CHANNEL, startDate=str(a),
            endDate=str(b), metrics=metrics, **extra).execute().get('rows', [])

    daily = query(start, requested_end, dimensions='day', sort='day')
    if not daily:
        raise ValueError('official daily Analytics unavailable')
    cutoff = dt.date.fromisoformat(daily[-1][0])
    if [r[0] for r in daily] != dates(start, cutoff):
        raise ValueError('official channel daily coverage has gaps')
    if not today-dt.timedelta(days=7) <= cutoff <= requested_end:
        raise ValueError('official daily cutoff outside freshness window')
    split = query(start, cutoff, dimensions='day,creatorContentType', sort='day')
    engagement_days = query(start, cutoff, 'likes,comments,shares', dimensions='day', sort='day')
    if not engagement_days:
        raise ValueError('official engagement availability unknown')
    quality_cutoff = min(cutoff, dt.date.fromisoformat(engagement_days[-1][0]))
    known = {r[0]: r[1] for r in con.execute(
        'select video_id,publish_date from dim_video where is_active and publish_date is not null').fetchall()}
    periods = []

    def views_for_ids(ids, a, b):
        result = 0
        for offset in range(0, len(ids), 100):
            rows = query(a, b, filters='video=='+','.join(ids[offset:offset+100]))
            result += sum(r[0] for r in rows)
        return result

    for kind, a, b in con.execute('select period_type,period_start,period_end from fact_period_analytics where period_start>=? and period_start<=? order by period_start', [start,today]).fetchall():
        end = min(b, quality_cutoff)
        if end < a:
            continue
        headline = query(a, end)
        if len(headline) != 1:
            raise ValueError('official period headline unavailable')
        engagement = query(a, end, 'likes,comments,shares')
        if len(engagement) != 1:
            raise ValueError('official period engagement unavailable')
        formats = query(a, end, dimensions='creatorContentType')
        new = views_for_ids([v for v,day in known.items() if a <= day <= end], a, end)
        prior = views_for_ids([v for v,day in known.items() if day < a], a, end)
        periods.append(dict(period_type=kind, period_start=str(a), period_end=str(b),
            metric_end_date=str(end), views=headline[0][0], new_views=new, prior_views=prior,
            residual=headline[0][0]-new-prior, likes=engagement[0][0], comments=engagement[0][1],
            shares=engagement[0][2], formats=dict(formats),
            daily_residual=headline[0][0]-sum(r[1] for r in daily if str(a)<=r[0]<=str(end))))

    d7 = []
    for vid, published in sorted(known.items()):
        if not previous <= published <= today:
            continue
        end = published+dt.timedelta(days=6)
        # Incomplete windows remain unavailable. No optimistic source flag survives.
        record = dict(video_id=vid, start=str(published), end=str(end), complete=False,
                      actual_end=None, views=None, likes=None, comments=None, shares=None)
        if end <= quality_cutoff:
            observed = query(published, end, dimensions='day', sort='day', filters='video=='+vid)
            observed_engagement = query(published, end, 'likes,comments,shares',
                dimensions='day', sort='day', filters='video=='+vid)
            record['actual_end'] = observed[-1][0] if observed else None
            # Missing/zero-suppressed days are conservative pending, never fabricated zeroes.
            if [r[0] for r in observed] == dates(published, end) and [r[0] for r in observed_engagement] == dates(published, end):
                total = query(published, end, filters='video=='+vid)
                engagement = query(published, end, 'likes,comments,shares', filters='video=='+vid)
                if len(total) == len(engagement) == 1:
                    record.update(complete=True, views=total[0][0], likes=engagement[0][0],
                                  comments=engagement[0][1], shares=engagement[0][2])
        d7.append(record)

    playlist = channels[0]['contentDetails']['relatedPlaylists']['uploads']
    candidates, page, reached_boundary = set(), None, False
    for _ in range(30):
        kwargs = dict(part='contentDetails', playlistId=playlist, maxResults=50)
        if page:
            kwargs['pageToken'] = page
        result = yt.playlistItems().list(**kwargs).execute()
        for row in result.get('items', []):
            detail = row['contentDetails']
            stamp = detail.get('videoPublishedAt')
            if stamp and dt.datetime.fromisoformat(stamp.replace('Z','+00:00')).astimezone(KST).date() < previous:
                reached_boundary = True
            else:
                candidates.add(detail['videoId'])
        page = result.get('nextPageToken')
        if reached_boundary or not page:
            break
    else:
        raise ValueError('uploads pagination incomplete')
    discovered = []
    all_known = {r[0] for r in con.execute('select video_id from dim_video').fetchall()}
    ids = sorted(candidates-all_known)
    for offset in range(0,len(ids),50):
        result = yt.videos().list(part='id,snippet,status,statistics', id=','.join(ids[offset:offset+50])).execute()
        for row in result.get('items', []):
            snippet = row['snippet']
            if snippet['channelId'] != CHANNEL:
                raise ValueError('discovery channel mismatch')
            if row['status'].get('privacyStatus') != 'public':
                continue
            stamp = dt.datetime.fromisoformat(snippet['publishedAt'].replace('Z','+00:00'))
            day = stamp.astimezone(KST).date()
            if previous <= day <= today and stamp <= now:
                discovered.append(dict(video_id=row['id'], published_date=str(day),
                    published_at=stamp.isoformat(), title=snippet['title'],
                    views=int(row['statistics']['viewCount']) if 'viewCount' in row.get('statistics',{}) else None,
                    channel_id=CHANNEL, privacy='public'))
    return dict(schema=SCHEMA,channel_id=CHANNEL,captured_at=now.isoformat(),
        coverage_start=str(start),requested_end=str(requested_end),actual_end=str(cutoff),
        quality_cutoff=str(quality_cutoff), daily=[dict(date=d,views=v) for d,v in daily],
        daily_formats=[dict(date=d,form=f,views=v) for d,f,v in split], periods=periods,
        d7=d7,discovered=sorted(discovered,key=lambda x:(x['published_date'],x['video_id'])))


def validate(payload, now=None):
    now = now or dt.datetime.now(KST)
    if payload.get('schema') != SCHEMA or payload.get('channel_id') != CHANNEL:
        raise ValueError('verified YouTube source identity invalid')
    captured = dt.datetime.fromisoformat(payload['captured_at'])
    if captured.tzinfo is None or not -300 <= (now-captured).total_seconds() <= 48*3600:
        raise ValueError('verified YouTube source stale')
    start,end = map(dt.date.fromisoformat,(payload['coverage_start'],payload['actual_end']))
    requested=dt.date.fromisoformat(payload['requested_end'])
    quality=dt.date.fromisoformat(payload['quality_cutoff'])
    if not start<=quality<=end<=requested<=now.date():
        raise ValueError('API cutoff order invalid')
    if end > now.date() or end < now.date()-dt.timedelta(days=7) or start > end:
        raise ValueError('invalid Analytics date coverage')
    if [r['date'] for r in payload['daily']] != dates(start,end):
        raise ValueError('duplicate or missing daily rows')
    def integer(value):
        if type(value) is not int or value<0:
            raise ValueError('invalid API count')
    for r in payload['daily']:
        integer(r['views'])
    seen = set()
    for r in payload['daily_formats']:
        key = (r['date'],r['form'])
        if key in seen or r['form'] not in FORMS or not str(start)<=r['date']<=str(end):
            raise ValueError('invalid or duplicate daily format')
        seen.add(key); integer(r['views'])
    ids=set()
    for r in payload['discovered']:
        if r['privacy']!='public' or r['channel_id']!=CHANNEL or not re.fullmatch('[A-Za-z0-9_-]{11}',r['video_id']) or r['video_id'] in ids:
            raise ValueError('untrusted discovery')
        ids.add(r['video_id'])
        if r['views'] is not None:integer(r['views'])
        stamp=dt.datetime.fromisoformat(r['published_at'])
        if stamp.tzinfo is None or stamp>now or str(stamp.astimezone(KST).date())!=r['published_date']:
            raise ValueError('invalid publication clock')
    seen_periods=set()
    for r in payload['periods']:
        key=(r['period_type'],r['period_start'])
        if key in seen_periods or r['period_type'] not in ('month','week'):
            raise ValueError('duplicate or invalid period')
        seen_periods.add(key)
        if not r['period_start']<=r['metric_end_date']<=min(r['period_end'],payload['quality_cutoff']):
            raise ValueError('invalid period cutoff')
        for key in ('views','new_views','prior_views','likes','comments','shares'):
            integer(r[key])
        if r['views']!=r['new_views']+r['prior_views']+r['residual']:
            raise ValueError('period reconciliation failed')
        if any(f not in FORMS for f in r['formats']):
            raise ValueError('unknown creator content type')
        for v in r['formats'].values():integer(v)
    seen_d7=set()
    for r in payload['d7']:
        if r['video_id'] in seen_d7 or not re.fullmatch('[A-Za-z0-9_-]{11}',r['video_id']) or type(r['complete']) is not bool:
            raise ValueError('D7 identity invalid')
        seen_d7.add(r['video_id'])
        if r['complete']:
            if r['end']!=r['actual_end'] or r['end']>payload['quality_cutoff'] or (dt.date.fromisoformat(r['end'])-dt.date.fromisoformat(r['start'])).days!=6:
                raise ValueError('D7 window not verified')
            for key in ('views','likes','comments','shares'):integer(r[key])
    return payload


def apply_overlay(path, payload):
    import duckdb
    validate(payload)
    # Explicit capability guard: accept only disposable snapshots, never a source DB.
    path=Path(path).resolve()
    if not (str(path).startswith('/private/tmp/') or str(path).startswith('/tmp/')):
        raise ValueError('API overlay requires a disposable snapshot path')
    con=duckdb.connect(str(path))
    try:
        con.execute('begin')
        for r in payload['periods']:
            if con.execute('select count(*) from fact_period_analytics where period_type=? and period_start=? and period_end=?',
                           [r['period_type'],r['period_start'],r['period_end']]).fetchone()[0]!=1:
                raise ValueError('snapshot period differs from API collection')
            con.execute('''update fact_period_analytics set metric_end_date=?,period_complete=?,
                channel_view_count=?,channel_like_count=?,channel_comment_count=?,channel_share_count=?,channel_engagement_count=?,
                new_published_view_count=?,prior_published_view_count=?,unknown_publish_view_count=?,
                fetched_at=?,raw_status='ok',error='',source=? where period_type=? and period_start=?''',
                [r['metric_end_date'],r['metric_end_date']==r['period_end'],r['views'],r['likes'],r['comments'],r['shares'],
                 r['likes']+r['comments']+r['shares'],r['new_views'],r['prior_views'],r['residual'],
                 dt.datetime.fromisoformat(payload['captured_at']).astimezone(KST).replace(tzinfo=None),
                 'YouTube Analytics verified returned-day cutoff; channel_views=standalone; canonical publication-ID cohorts; signed residual',r['period_type'],r['period_start']])
        # Remove all competing old completion flags within the refreshed cohort.
        for r in payload['d7']:
            if con.execute('select count(*) from dim_video where is_active and video_id=? and publish_date=?',[r['video_id'],r['start']]).fetchone()[0]!=1:
                raise ValueError('snapshot publication identity differs from API collection')
            con.execute('update fact_analytics_d7 set d7_complete=false where video_id=?',[r['video_id']])
            con.execute('''insert into fact_analytics_d7(video_id,metric_start_date,metric_end_date,requested_end_date,
                fetched_at,view_count,like_count,comment_count,share_count,api_rows,d7_complete,source,raw_status,error)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?) on conflict(video_id,metric_start_date) do update set
                metric_end_date=excluded.metric_end_date,requested_end_date=excluded.requested_end_date,
                fetched_at=excluded.fetched_at,view_count=excluded.view_count,like_count=excluded.like_count,
                comment_count=excluded.comment_count,share_count=excluded.share_count,api_rows=excluded.api_rows,
                d7_complete=excluded.d7_complete,source=excluded.source,raw_status=excluded.raw_status,error=excluded.error''',
                [r['video_id'],r['start'],r['actual_end'],r['end'],
                 dt.datetime.fromisoformat(payload['captured_at']).astimezone(KST).replace(tzinfo=None),
                 r['views'],r['likes'],r['comments'],r['shares'],7 if r['complete'] else 0,r['complete'],
                 'YouTube Analytics verified day coverage; views standalone', 'ok' if r['complete'] else 'not_due',''])
        con.execute('create table if not exists dashboard_verified_youtube(payload varchar)')
        con.execute('delete from dashboard_verified_youtube')
        con.execute('insert into dashboard_verified_youtube values (?)',[json.dumps(payload,ensure_ascii=False)])
        con.execute('commit')
    except BaseException:
        con.execute('rollback');raise
    finally:con.close()


def load_overlay(con, required=False):
    if not con.execute("select count(*) from information_schema.tables where table_name='dashboard_verified_youtube'").fetchone()[0]:
        if required:raise ValueError('verified YouTube API overlay missing')
        return None
    rows=con.execute('select payload from dashboard_verified_youtube').fetchall()
    if len(rows)!=1:raise ValueError('verified YouTube API overlay ambiguous')
    return validate(json.loads(rows[0][0]))


def fetch_overlay(ssh, remote_python):
    script=Path(__file__).read_text()+'''\n
if __name__ == '__main__':
    import duckdb,sys
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    config=json.loads(Path('/Users/cnc-media/automations/youtube-view-snapshot/config.json').read_text())
    credentials=Credentials.from_authorized_user_file(config.get('youtube_analytics_token_json') or '/Users/cnc-media/automations/mbd/secrets/youtube_analytics_token.json')
    if credentials.expired and credentials.refresh_token:credentials.refresh(Request())
    yt=build('youtube','v3',credentials=credentials,cache_discovery=False)
    analytics=build('youtubeAnalytics','v2',credentials=credentials,cache_discovery=False)
    con=duckdb.connect('/Users/cnc-media/automations/youtube-view-snapshot/youtube_views.duckdb',read_only=True)
    try:print(json.dumps(collect(yt,analytics,con,dt.datetime.now(KST)),ensure_ascii=False))
    finally:con.close()
'''
    result=subprocess.run(ssh+[remote_python,'-B','-'],input=script,text=True,capture_output=True,timeout=900)
    if result.returncode:
        # Never relay provider errors that might include credential material.
        raise RuntimeError('verified YouTube API collection failed; previous snapshot retained')
    if len(result.stdout)>8_000_000:raise ValueError('API overlay too large')
    return validate(json.loads(result.stdout))


def render_overlay(payload, month, *, daily=False):
    if not payload:return ''
    esc=lambda s:html.escape(str(s),quote=True)
    ym=f'{dt.date.fromisoformat(payload["requested_end"]).year}-{month:02d}'
    found=[r for r in payload['discovered'] if r['published_date'].startswith(ym)]
    body=''
    if daily:
        periods=[r for r in payload['periods'] if r['period_type']=='month' and r['period_start'].startswith(ym)]
        if periods:
            p=periods[0]
            values=' · '.join(f'{FORMS[k]} {v:,}회' for k,v in p['formats'].items())
            body+=f'<p>{esc(values)}</p><p>공식 총조회수 {p["views"]:,}회 · 포맷 합계 차이 {p["views"]-sum(p["formats"].values()):+,}회 · 일별 합계 차이 {p["daily_residual"]:+,}회</p>'
        body+='<details open><summary>공식 일별 조회수 · 실제 집계 '+esc(payload['actual_end'])+'까지</summary>'
        body+=''.join(f'<div class="yt-bench-row" data-yt-api-day="{r["date"]}" data-views="{r["views"]}"><b>{r["date"]}</b><small>{r["views"]:,}회</small></div>' for r in payload['daily'] if r['date'].startswith(ym))+'</details>'
    body+=f'<p>공개 업로드 추가 확인 {len(found)}건 · 기존 편성/영상 DB에 미연결. 제목 매칭·포맷 추정 없이 별도 표시하며 기존 발행·D7 분모에 합산하지 않습니다.</p>'
    for r in found:
        views=f'{r["views"]:,}회' if r['views'] is not None else '—'
        body+=f'<div class="yt-bench-row" data-yt-discovered-id="{esc(r["video_id"])}"><b>{esc(r["published_date"])}</b><small><a href="https://www.youtube.com/watch?v={esc(r["video_id"])}" target="_blank" rel="noopener">{esc(r["title"])}</a> · 공개 누적 {views} · D7 —</small></div>'
    return '<div class="yt-panel" data-yt-verified-api="'+str(month)+'"><h3>공식 API 연결 · 추가 공개 영상</h3>'+body+'<small>수집 '+esc(payload['captured_at'])+' · YouTube 플랫폼 포맷은 수기 LF/SF와 별도 기준</small></div>'
