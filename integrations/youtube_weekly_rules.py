"""Read-only Weekly rule lane; no Slack transport, DB or credential writes."""
from __future__ import annotations
import datetime as dt
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import time

RULE = 'youtube-weekly-lf-sf-v1'
KST = dt.timezone(dt.timedelta(hours=9))
ROOT = Path('/Users/cnc-media/automations/youtube-view-snapshot')
PYTHON = '/Users/cnc-media/automations/.venvs/mbd/bin/python3'
CLIENT = Path('/Users/cnc-media/services/mbd-dashboard-metrics/dashboard_metrics_client.py')
CHANNEL = 'UCBKtitA1RwY7F32rCniV1dA'
_CACHE = {}
FAIL = '🙏 *확인 필요*\n• 위클리 기준 지표를 확인 못 했습니다. 이전 집계나 추정값으로 대체하지 않습니다.'


def weekly_request(question, today=None):
    """Route all weekly surfaces before the legacy dashboard/LLM paths."""
    today = today or dt.datetime.now(KST).date()
    q = re.sub(r'\s+', ' ', question.lower()).strip()
    if re.search(r'd\+7|p75', q):
        return None
    spec = importlib.util.spec_from_file_location('weekly_period_parser', CLIENT)
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    weekly = bool(re.search(r'지난\s*주|저번\s*주|이번\s*주|금주|주간|위클리|\bd[57]\b', q))
    # A bare weekly request means the most recent elapsed calendar week.
    if weekly and not re.search(r'\d+[월/.-]|지난\s*주|저번\s*주|이번\s*주|금주', q):
        q += ' 지난주'
    try:
        start, end, kind = parser.period(q, today)
    except ValueError:
        if weekly:
            raise
        return None
    if not weekly and not (start.weekday() == 0 and (end-start).days == 6):
        return None
    if start.weekday() != 0 or (end-start).days != 6:
        raise ValueError('주간은 월요일~일요일의 날짜 범위를 지정해주세요.')
    if re.search(r'쇼핑|shopping|yts', q):
        raise ValueError('유튜브 쇼핑은 집계 대상에서 제외되어 있습니다.')
    cleaned = re.sub(r'live|위클리|신규|기발행|기존|기여|분해|구성|잔여|미분류|조정|일반\s*영상|쇼츠|비교|증감률|전주\s*대비|\bd[57]\b', '', q)
    parser.check_scope(cleaned, 'youtube')
    return start, end


def answer_weekly(question, today=None):
    try:
        request = weekly_request(question, today)
        if request is None:
            return None
        start, end = request
        key = (str(start), str(end))
        cached = _CACHE.get(key)
        if cached and time.monotonic()-cached[0] < 300:
            packet = cached[1]
        else:
            result = subprocess.run([PYTHON, '-B', str(Path(__file__).resolve()), str(start), str(end)],
                                    capture_output=True, text=True, check=True, timeout=90)
            packet = json.loads(result.stdout)
            if packet['rule'] != RULE or packet['week'] != list(key):
                raise ValueError('주간 근거가 요청과 일치하지 않습니다.')
            _CACHE.clear()
            _CACHE[key] = (time.monotonic(), packet)
        return render(packet, question)
    except ValueError as exc:
        return '🙏 *확인 필요*\n• ' + str(exc)
    except Exception:
        return FAIL


def cohorts(videos, start, end):
    result = {k: [] for k in ('new_LF', 'new_SF', 'prior_LF', 'prior_SF', 'LIVE')}
    seen = set()
    for vid, pub, form in videos:
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid) or vid in seen:
            raise ValueError('영상 ID 중복 또는 형식 오류')
        seen.add(vid)
        if isinstance(pub, dt.datetime):
            pub = pub.date()
        if not pub or pub > end:
            continue
        if form == 'LIVE':
            result['LIVE'].append(vid)
        elif form in ('LF', 'SF'):
            result[('new_' if pub >= start else 'prior_') + form].append(vid)
    return {k: sorted(v) for k, v in result.items()}


def schedule_status(state, groups, start, end, now):
    """A row count alone cannot establish schedule completeness."""
    stamp = dt.datetime.fromisoformat(state['generated_at'])
    stamp = stamp.replace(tzinfo=KST) if stamp.tzinfo is None else stamp
    parity = state['parity']
    if not -300 <= (now-stamp).total_seconds() <= 36*3600:
        raise ValueError('편성 검증 갱신 지연')
    if state.get('fallback_used') or state.get('verification_source') != 'youtube_data_api':
        raise ValueError('편성 검증 근거 부족')
    if parity['period_start'] != str(start) or parity['period_end'] != str(end):
        return None
    expected = groups['new_LF'] + groups['new_SF']
    if (len(expected) != len(parity['expected_video_ids']) or
            set(expected) != set(parity['expected_video_ids']) or
            set(expected) != set(parity['duckdb_video_ids']) or
            parity.get('duplicate_resolution') or parity.get('missing_in_duckdb') or parity.get('extra_in_duckdb')):
        raise ValueError('편성과 LF/SF 영상 ID 불일치')
    unresolved = parity['unresolved_rows']
    if (state.get('capture_status') not in ('ok', 'verified_only_schedule_pending') or
            parity['expected_count'] != len(expected)+len(unresolved) or
            parity['resolved_count'] != len(expected)):
        raise ValueError('편성 검증 상태 불일치')
    return len(unresolved)


def load(start, end):
    import duckdb
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    now = dt.datetime.now(KST)
    if start.weekday() != 0 or (end-start).days != 6 or start > now.date():
        raise ValueError('유효하지 않은 주간')
    creds = Credentials.from_authorized_user_file('/Users/cnc-media/automations/mbd/secrets/youtube_analytics_token.json')
    analytics = build('youtubeAnalytics', 'v2', credentials=creds, cache_discovery=False)
    data = build('youtube', 'v3', credentials=creds, cache_discovery=False)
    identity = data.channels().list(part='id', mine=True).execute()
    if [r['id'] for r in identity.get('items', [])] != [CHANNEL]:
        raise ValueError('채널 신원 불일치')
    def query(a, b, **kwargs):
        return analytics.reports().query(ids='channel==MINE', startDate=str(a), endDate=str(b), metrics='views', **kwargs).execute(num_retries=1)
    def rows(response, columns):
        if [x['name'] for x in response.get('columnHeaders', [])] != columns:
            raise ValueError('API 응답 형식 검증 실패')
        result = response.get('rows', [])
        if not isinstance(result, list):
            raise ValueError('API 응답 누락')
        for row in result:
            if len(row) != len(columns) or type(row[-1]) is not int or row[-1] < 0:
                raise ValueError('API 숫자 검증 실패')
        return result
    latest_rows = rows(query(now.date()-dt.timedelta(days=21), now.date()-dt.timedelta(days=1), dimensions='day', sort='day'), ['day','views'])
    cutoff = max(dt.date.fromisoformat(r[0]) for r in latest_rows)
    if (now.date()-cutoff).days > 7:
        raise ValueError('Analytics 수집일 지연')
    con = duckdb.connect(str(ROOT/'youtube_views.duckdb'), read_only=True)
    try:
        videos = con.execute("SELECT video_id,publish_date,form FROM dim_video WHERE COALESCE(is_active,TRUE)").fetchall()
    finally:
        con.close()
    # Use the same recent-publication privacy screen as Weekly, but fail on API errors.
    recent = [v[0] for v in videos if v[1] and v[1] >= cutoff-dt.timedelta(days=21)]
    public = set()
    for i in range(0, len(recent), 50):
        response = data.videos().list(part='status,snippet', id=','.join(recent[i:i+50])).execute(num_retries=1)
        for row in response.get('items', []):
            if row['snippet']['channelId'] != CHANNEL:
                raise ValueError('영상 채널 불일치')
            if row['status']['privacyStatus'] == 'public':
                public.add(row['id'])
    videos = [v for v in videos if v[0] not in recent or v[0] in public]
    groups = cohorts(videos, start, end)
    # Schedule completeness is independent of authenticated channel-view totals.
    # Keep its unknown status explicit instead of dropping the entire answer.
    try:
        state = json.loads((ROOT/'state/latest_schedule_completeness.json').read_text())
        pending = schedule_status(state, groups, start, end, now)
    except (OSError, ValueError, KeyError, TypeError):
        pending = None
    cache = {}
    def window(stop):
        if stop < start:
            return None
        if stop in cache:
            return cache[stop]
        daily = rows(query(start, stop, dimensions='day', sort='day'), ['day','views'])
        expected = {str(start+dt.timedelta(days=i)) for i in range((stop-start).days+1)}
        if len(daily) != len(expected) or {r[0] for r in daily} != expected:
            raise ValueError('API 기간 중 누락 일자')
        headline = rows(query(start, stop), ['views'])
        if len(headline) != 1:
            raise ValueError('공식 채널 총조회수 누락')
        totals = {}
        for label, ids in groups.items():
            total = 0
            for i in range(0, len(ids), 100):
                batch = ids[i:i+100]
                result = rows(query(start, stop, dimensions='video', filters='video=='+','.join(batch), maxResults=200), ['video','views'])
                if len({r[0] for r in result}) != len(result) or not {r[0] for r in result} <= set(batch):
                    raise ValueError('API 영상 ID 불일치')
                total += sum(r[1] for r in result)
            totals[label] = total
        totals['channel'] = headline[0][0]
        totals['residual'] = totals['channel']-sum(totals[k] for k in groups)
        cache[stop] = totals
        return totals
    windows = {}
    for name, days in [('D5',4),('D7',6)]:
        target = start+dt.timedelta(days=days)
        stop = min(target, cutoff)
        totals = window(stop)
        previous = None
        if totals and stop == target:
            a, b = start-dt.timedelta(days=7), target-dt.timedelta(days=7)
            daily = rows(query(a, b, dimensions='day', sort='day'), ['day','views'])
            expected = {str(a+dt.timedelta(days=i)) for i in range(days+1)}
            if len(daily) == len(expected) and {r[0] for r in daily} == expected:
                values = rows(query(a,b), ['views'])
                if len(values) == 1:
                    previous = values[0][0]
        windows[name] = dict(target=str(target), actual_end=str(stop), complete=stop==target,
                             values=totals, previous_channel=previous)
    return dict(rule=RULE, week=[str(start),str(end)], captured_at=now.isoformat(),
                groups=groups, pending=pending, windows=windows)


def render(p, question=''):
    q = question.lower()
    if re.search(r'쇼핑|shopping|yts', q):
        return '유튜브 쇼핑은 집계 대상에서 제외되어 있습니다.'
    lf, sf = bool(re.search(r'롱폼|일반\s*영상|\blf\b', q)), bool(re.search(r'숏폼|쇼츠|\bsf\b', q))
    forms = ['LF'] if lf and not sf else ['SF'] if sf and not lf else ['LF','SF']
    if not re.search(r'상세|분해|기여|구성|잔여|미분류|영상별|콘텐츠별|상위|순위|잘된|top|구독자', q):
        count = sum(len(p['groups']['new_'+f]) for f in forms)
        status = '편성 확인 중' if p['pending'] is None else f'미연결 편성 {p["pending"]}건' if p['pending'] else '편성 확인 완료'
        lines = [f'*유튜브 주간 · {p["week"][0]}~{p["week"][1]}*',
                 f'• 연결 {"/".join(forms)} 발행 {count}개 · {status}']
        requested = [x for x in ('D5','D7') if re.search(r'\b'+x.lower()+r'\b',q)] or ['D5','D7']
        for name in requested:
            w=p['windows'][name]; v=w['values']
            if v is None:
                lines += [f'• {name}: 집계 대기']
                continue
            state = '완료' if w['complete'] else '집계 중'
            comparison = ''
            if w['complete'] and w['previous_channel']:
                comparison = f' · 전주 대비 {(v["channel"]/w["previous_channel"]-1)*100:+.1f}%'
            lines += [f'• {name} 채널 조회수 {v["channel"]:,}회 · {w["actual_end"]}까지({state}){comparison}']
            if len(forms)==1:
                f=forms[0]
                lines += [f'  {f} 신규 {v["new_"+f]:,}회 · 기발행 {v["prior_"+f]:,}회']
        lines += ['• 발행일 KST · 조회수 집계일 PT']
        return '\n'.join(lines)
    lines = [f'*유튜브 위클리 · {p["week"][0]}~{p["week"][1]}*',
             '• 발행 주차: KST 월~일 전체 · 지표 날짜: YouTube Analytics PT']
    count = sum(len(p['groups']['new_'+f]) for f in forms)
    status = f'미연결 편성 {p["pending"]}건' if p['pending'] is not None else '전체 편성 검증 확인 못 함'
    lines += [f'• 연결 {"/".join(forms)} 발행 {count}개 · '+status+(' · 잠정' if p['pending'] != 0 else '')]
    requested = [x for x in ('D5','D7') if re.search(r'\b'+x.lower()+r'\b',q)] or ['D5','D7']
    for name in requested:
        w = p['windows'][name]
        v = w['values']
        if v is None:
            lines += [f'• {name}: 집계 대기 · 비교 보류']
            continue
        lines += [f'*{name} · {p["week"][0]}~{w["actual_end"]} PT · '+('완료' if w['complete'] else '잠정')+'*',
                  f'• 공식 채널 전체 조회수: {v["channel"]:,}회',
                  f'• 신규 {"/".join(forms)}: {sum(v["new_"+f] for f in forms):,}회',
                  f'• 기발행 {"/".join(forms)}: {sum(v["prior_"+f] for f in forms):,}회',
                  f'• 별도 LIVE: {v["LIVE"]:,}회 · 미분류/조정 잔여: {v["residual"]:,}회']
        if len(forms) == 1:
            lines += ['• 채널 전체에는 다른 폼도 포함됩니다. 잔여는 전체 LF/SF·LIVE 차감 기준입니다.']
        prev = w['previous_channel']
        if w['complete'] and prev is not None and prev > 0:
            lines += [f'• 채널 전체 전주 {name} 대비: {(v["channel"]/prev-1)*100:+.1f}%']
        else:
            lines += [f'• 전주 {name} 비교·증감률 보류: 미완료 또는 비교 근거 부족']
    if re.search(r'상위|순위|영상별|콘텐츠별|잘된|top|상세',q):
        lines += ['• 영상별 순위는 이 주간 집계 경로에서 확인 못 함. 전체 순위로 대체하지 않습니다.']
    if '구독자' in q:
        lines += ['• 구독자 지표는 이 주간 조회수 집계에서 확인 못 함.']
    lines += [f'• 공식 Analytics 재조회: {p["captured_at"]} · 최대 5분 재사용']
    return '\n'.join(lines)


if __name__ == '__main__':
    try:
        print(json.dumps(load(dt.date.fromisoformat(sys.argv[1]), dt.date.fromisoformat(sys.argv[2])), ensure_ascii=False))
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
