"""Read-only shared answer engine. Deployed privately beside data/metrics.json.

Question enters via stdin, never a shell argument. No Slack/network writes, no LLM.
Unanswerable metric questions fail closed instead of reaching legacy totals.
"""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
import re
import sys
import unicodedata
import urllib.request
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))
URL = 'https://been3p-prog.github.io/mbd-h2-kr-dashboard/'
SCHEMA = 'mbd-bot-metrics-v1'
MAX_BYTES = 8_000_000


def safe(text):
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', ' ')[:220]


def won(value):
    if value is None:
        return '확인 못 함'
    return f'{value / 100_000_000:.2f}억 ({value:,.0f}원)'


def number(value):
    return '—' if value is None else f'{value:,.0f}'


def compact_won(value):
    if value is None:
        return '확인 못 함'
    return f'{value / 100_000_000:.2f}억'


def bullet_summary(text):
    return f'• 요약: {text}'


def candidate(question, domain):
    q = unicodedata.normalize('NFKC', question).lower()
    if len(q) > 1600:
        return False
    if re.search(r'취소\s*해|수정\s*해|이동\s*해|등록\s*해|삭제\s*해|만들어|기획해|방법|매뉴얼|정책|세금|정산', q):
        return False
    if domain == 'ads' and ('부킹 기준' in q or '부킹률' in q):
        return False  # explicit admin basis retains existing authorization/mutation lanes
    return bool(re.search(r'성과|실적|매출|gmv|거래액|예측|부킹|편성|지표|조회수|구독자|잘된|영상별|콘텐츠별|상세', q))


def month_end(day):
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def period(question, today):
    """Calendar day, Monday-Sunday week, explicit month-week, or calendar month."""
    q = re.sub(r'\s+', '', unicodedata.normalize('NFKC', question).lower())
    if re.search(r'작년|재작년|내년|후년|전전|지지난|저저번', q):
        raise ValueError('연도와 시작·종료일을 명시해주세요.')
    if re.search(r'월(?:초|중순|중|말)|상반|하반|분기|최근\d|(?<!\d)\d{2}년', q):
        raise ValueError('축약된 기간 대신 시작일과 종료일을 명시해주세요.')
    relative = [x for x in ('이번주','지난주','저번주','금주','이번달','지난달','전월','다음달','차월','오늘','어제') if x in q]
    if len(relative)>1:
        raise ValueError('기간을 하나씩 조회해주세요.')
    dates = re.findall(r'(?:(\d{4})년)?(\d{1,2})월(\d{1,2})일', q)
    iso = re.findall(r'(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)', q)
    slash = re.findall(r'(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])', q)
    parsed = [dt.date(int(y or today.year), int(m), int(d)) for y,m,d in dates + iso]
    if not parsed:
        parsed = [dt.date(today.year, int(m), int(d)) for m,d in slash]
    if parsed:
        if relative:
            raise ValueError('상대 기간과 날짜를 동시에 지정하지 마세요.')
        if len(parsed) > 2 or len(set(parsed)) != len(parsed):
            raise ValueError('기간을 하나만 지정해주세요.')
        start, end = parsed[0], parsed[-1]
        kind = 'day' if start == end else 'range'
    elif '오늘' in q or '어제' in q:
        if '오늘' in q and '어제' in q:
            raise ValueError('하루씩 조회해주세요.')
        start = end = today - dt.timedelta(days=int('어제' in q))
        kind = 'day'
    else:
        months = re.findall(r'(?:(\d{4})년)?(\d{1,2})월', q)
        week = re.search(r'(\d{1,2})월([1-5])주(?:차)?', q)
        if len(months) > 1:
            raise ValueError('한 달 또는 정확한 날짜 범위를 지정해주세요.')
        if week:
            m,w = map(int, week.groups())
            start = dt.date(int(months[0][0] or today.year), m, (w-1)*7+1)
            end = min(month_end(start), start + dt.timedelta(days=6))
            kind = 'month_week'
        elif '지난주' in q or '저번주' in q or '이번주' in q or '금주' in q:
            if months:
                raise ValueError('주와 월을 동시에 지정하지 말고 날짜 범위를 지정해주세요.')
            start = today - dt.timedelta(days=today.weekday())
            if '지난주' in q or '저번주' in q:
                start -= dt.timedelta(days=7)
            end = start + dt.timedelta(days=6)
            kind = 'week'
        else:
            if months:
                y,m = months[0]
                start = dt.date(int(y or today.year), int(m), 1)
            elif '지난달' in q or '전월' in q or '저번달' in q:
                start = (today.replace(day=1)-dt.timedelta(days=1)).replace(day=1)
            elif '차월' in q or '다음달' in q:
                start = month_end(today)+dt.timedelta(days=1)
            else:
                start = today.replace(day=1)
            end = month_end(start)
            kind = 'month'
    if end < start or (end-start).days > 366:
        raise ValueError('유효한 1년 이내 기간을 지정해주세요.')
    return start, end, kind


def validate(packet, now, public_bytes=None):
    if packet.get('schema') != SCHEMA or not re.fullmatch('[0-9a-f]{64}', packet.get('dashboard_sha256','')):
        raise ValueError('공통 지표 형식 검증 실패')
    built = dt.datetime.fromisoformat(packet['built_at'])
    if built.tzinfo is None or not -300 <= (now-built).total_seconds() <= 48*3600:
        raise ValueError('공통 지표 갱신 지연(48시간 초과)')
    cutoff = dt.date.fromisoformat(packet['as_of'])
    if cutoff > now.date() or (now.date()-cutoff).days > 2:
        raise ValueError('원천 기준일 갱신 지연')
    if public_bytes is not None and hashlib.sha256(public_bytes).hexdigest() != packet['dashboard_sha256']:
        raise ValueError('대시보드 배포본과 지표 버전 불일치')
    if not all(k in packet for k in ('revenue','live','youtube','source_as_of')):
        raise ValueError('공통 지표 필수 필드 누락')
    for key in ('revenue_mirror','live_quality','yt_quality','owned_media'):
        stamp = packet['source_as_of'].get(key)
        if not stamp:
            raise ValueError('원천 수집시각 누락')
        source_time = dt.datetime.fromisoformat(stamp)
        if source_time.tzinfo is None or not -300 <= (now-source_time).total_seconds() <= 48*3600:
            raise ValueError('원천 수집 갱신 지연')
    api = packet['youtube'].get('verified_api')
    if api:
        stamp=dt.datetime.fromisoformat(api['captured_at'])
        if api.get('schema')!='youtube-verified-api-v1' or api.get('channel_id')!='UCBKtitA1RwY7F32rCniV1dA':
            raise ValueError('공식 API 채널 검증 실패')
        if stamp.tzinfo is None or not -300 <= (now-stamp).total_seconds() <= 48*3600:
            raise ValueError('공식 API 수집 지연')
        end=dt.date.fromisoformat(api['actual_end'])
        if not now.date()-dt.timedelta(days=7) <= end <= now.date():
            raise ValueError('공식 API 집계일 지연')


def verified_youtube_answer(api, start, end, form_filter, verbose=False):
    """Standalone period totals take precedence; arbitrary dates use daily basis."""
    kind={'LF':'videoOnDemand','SF':'shorts'}.get(form_filter)
    label={'LF':'일반 동영상',
           'SF':'Shorts'}.get(form_filter,'채널')
    period=next((r for r in api['periods'] if r['period_start']==str(start) and r['period_end']==str(end)),None)
    if period:
        value=period['formats'].get(kind,0) if kind else period['views']
        lines=[f'• {label} 조회수: {number(value)}회 ({period["metric_end_date"]}까지)',
               f'• 데이터 기준: {start}~{period["metric_end_date"]} · '+('완료' if period['metric_end_date']==str(end) else '진행 중')]
        if verbose and not kind:
            names={'shorts':'Shorts','videoOnDemand':'일반 동영상','liveStream':'라이브','posts':'게시물'}
            lines+=['• 콘텐츠 타입: '+' / '.join(f'{names[k]} {number(v)}회' for k,v in period['formats'].items())]
        previous_start=(start-dt.timedelta(days=1)).replace(day=1) if period['period_type']=='month' else start-dt.timedelta(days=7)
        previous=next((r for r in api['periods'] if r['period_type']==period['period_type'] and r['period_start']==str(previous_start)),None)
        if period['metric_end_date']==period['period_end'] and previous and previous['metric_end_date']==previous['period_end']:
            baseline=previous['formats'].get(kind,0) if kind else previous['views']
            if baseline:
                lines += [f'• {"MoM" if period["period_type"]=="month" else "WoW"} {(value/baseline-1)*100:+.1f}% · 완료 기간끼리 비교']
        return lines
    if str(start)<api['coverage_start']:
        return ['• 조회수: 확인 못 함(연결 기간 밖)']
    stop=min(str(end),api['actual_end'])
    if str(start)>stop:
        return [f'• {label} 조회수: 집계 대기 · 실제 Analytics 최신일 {api["actual_end"]}. 미집계를 0으로 표시하지 않습니다.']
    coverage=[r['date'] for r in api['daily'] if str(start)<=r['date']<=stop]
    expected=(dt.date.fromisoformat(stop)-start).days+1
    if len(set(coverage))!=expected or len(coverage)!=expected:
        raise ValueError('공식 일별 데이터에 누락·중복이 있습니다.')
    rows=api['daily_formats'] if kind else api['daily']
    total=sum(r['views'] for r in rows if str(start)<=r['date']<=stop and (not kind or r['form']==kind))
    return [f'• {label} 조회수: {number(total)}회',
            f'• 데이터 기준: {start}~{stop}'+(' · 이후 날짜는 집계 대기' if stop<str(end) else '')]


def discovered_lines(api, start, end):
    found=[r for r in api.get('discovered',[]) if str(start)<=r['published_date']<=str(end)]
    if not found:return []
    lines=[f'• 공개 업로드 추가 확인 {len(found)}건 · 기존 영상/편성 DB 미연결. 위 발행·D7 분모와 별도입니다.']
    for r in found[:20]:
        if r.get('privacy')!='public' or r.get('channel_id')!='UCBKtitA1RwY7F32rCniV1dA' or not re.fullmatch('[A-Za-z0-9_-]{11}',r['video_id']):
            raise ValueError('추가 공개 영상 신원 확인 실패')
        lines += [f'• {r["published_date"]} {safe(r["title"])} · 공개 누적 {number(r["views"])} · <https://www.youtube.com/watch?v={r["video_id"]}|영상> · D7 —']
    if len(found)>20:lines += ['• 추가 공개 영상 전체는 대시보드에서 확인하거나 일별 조회해주세요.']
    return lines


def live_scope(rows, q):
    """Never silently drop a named brand or unsupported scope."""
    brands = sorted({r['brand'] for r in rows if r['brand']}, key=len, reverse=True)
    compact = re.sub(r'\s+', '', q).lower()
    matches = [b for b in brands if re.sub(r'\s+', '', b).lower() in compact]
    # longest exact source name wins for overlapping brand names
    matches = [b for b in matches if not any(b != a and b in a for a in matches)]
    if len(matches) > 1:
        raise ValueError('브랜드를 하나씩 조회해주세요.')
    clean = q
    if matches:
        clean = re.sub(re.escape(matches[0]), '', clean, flags=re.I)
    clean = re.sub(r'\d{4}[-년.]|\d{1,2}(?:주차|[월일주/.-])|이번\s*달|이번\s*주|지난\s*주|지난\s*달|오늘|어제|금월|금주|전월|차월|다음\s*달|저번\s*주|저번\s*달', '', clean)
    clean = re.sub(r'라이브커머스|라이브|방송|성과|실적|매출|거래액|gmv|GMV|1D|1H|누적|현황|상세|지표|편성|전체|월간|주간|일간|브랜드별|패키지별|브랜드|기준|합계|총|건수|시청자|수|알려줘|알려주세요|보여줘|보여주세요|어때|얼마|있어|알려|는|은|을|를|의|과|와|랑|좀|해줘', '', clean)
    if re.sub(r'[\s?.,!~·()0-9]+', '', clean):
        raise ValueError('브랜드/조건을 확정하지 못했습니다. 원천 브랜드명과 기간을 명시해주세요.')
    return matches[0] if matches else None


def live_answer(p, q, start, end, kind):
    brand = live_scope(p['live'], q)
    rows = [r for r in p['live'] if start.isoformat() <= r['date'] <= end.isoformat() and (not brand or r['brand'] == brand)]
    cutoff = p['as_of']
    elapsed = [r for r in rows if r['date'] <= cutoff]
    quality = [r for r in elapsed if not r['free'] and (r['gmv_1d'] or 0) > 0]
    unknown = [r for r in elapsed if not r['free'] and r['unknown_party']]
    attributed = [r for r in elapsed if not r['free'] and r['attributed']]
    def total_metric(items,key):
        known = [r[key] for r in items if r[key] is not None]
        if items and not known:
            return '확인 못 함(전부 미기입)'
        suffix = f' · 미기입 {len(items)-len(known)}건 제외' if len(known)<len(items) else ''
        return won(sum(known))+suffix
    label = safe(brand) if brand else '전체'
    lines = [f'*라이브 {label} · {start}~{end}*',
             f'• 전체 편성 {len(rows)}건 · 성과 확인 {len(quality)}건 · 미래 편성 {sum(r["date"] > cutoff for r in rows)}건',
             f'• 1D 브랜드 거래액: {won(sum(r["gmv_1d"] for r in quality)) if quality else "확인된 실적 없음"}',
             f'• 방송별 GMV: {total_metric(quality,"gmv") if quality else "확인된 실적 없음"}',
             f'• RAW 귀속 매출(3P AF): {total_metric(attributed,"af") if elapsed else "해당 기간 실적 행 없음"}']
    if unknown:
        lines += [f'⚠️ 1P/3P 미기재 {len(unknown)}건 · AF {total_metric(unknown,"af")} 귀속 확인 필요. 위 RAW 합계에서 제외되며 매출 없음이 아닙니다.']
    if kind in ('week','month_week'):
        lines += ['• 주 기준: ' + ('월~일' if kind == 'week' else '대시보드 월내 1~7일/8~14일 구간')]
    if quality:
        lines += [f'• 방당 1D 평균: {won(sum(r["gmv_1d"] for r in quality)/len(quality))}']
    if brand or kind == 'day' or re.search(r'상세|브랜드별|패키지별|편성', q):
        for r in rows[:35]:
            measured = r['date'] <= cutoff and (r['gmv_1d'] or 0)>0
            lines += [f'• {r["date"]} {safe(r["brand"])} · {safe(r["package"])}: '
                      + (f'1D {won(r["gmv_1d"])} / 방송 GMV {won(r["gmv"])} / 시청자 {number(r["viewers"])}' if measured else '지표 — · 예정/집계 대기')
                      + (' · 무료(품질 합계 제외)' if r['free'] else '')]
        if len(rows)>35:
            lines += [f'• 전체 {len(rows)}건 중 35건 표시. 일별로 조회하면 누락 없이 확인할 수 있습니다.']
    lines += ['• 기준: 취소 제외 · 품질은 무료 제외·양수 1D · 방송별 GMV와 1D는 별개']
    raw_known = [r["af"] for r in attributed if r["af"] is not None]
    raw_summary = '해당 기간 실적 행 없음' if not elapsed else compact_won(sum(raw_known)) if raw_known or not attributed else '확인 못 함'
    summary = (
        f'성과 확인 {len(quality)}/{len(rows)}건 · 1D {compact_won(sum(r["gmv_1d"] for r in quality)) if quality else "확인된 실적 없음"}'
        f' · RAW {raw_summary}'
    )
    if sum(r["date"] > cutoff for r in rows):
        summary += f' · 미래 편성 {sum(r["date"] > cutoff for r in rows)}건은 지표 대기'
    if unknown:
        summary += f' · 귀속 미확인 {len(unknown)}건 별도'
    lines.insert(1, bullet_summary(summary))
    return lines


def revenue_answer(p, q, start, end, kind):
    month = p['revenue']['months'].get(start.strftime('%Y-%m'))
    if re.search(r'광고주|브랜드|지면|구좌', q):
        return ['*광고 상세 기준 확인*', '• 대시보드 기준 상세는 일반광고·통광마·라이브 팀 단위입니다.',
                '• 광고주·지면별 부킹 운영 값은 “부킹 기준 9월 광고주별 매출”처럼 조회해주세요. 마감 실적·예측과 다른 기준입니다.']
    teams = [('일반광고','ad_gen'),('통광마','ad_int'),('라이브','live')]
    requested = {'ad_gen': '일반광고' in q, 'ad_int': bool(re.search('통광마|통합광고',q)), 'live': '라이브' in q}
    selected = [(label,key) for label,key in teams if requested[key]] or teams
    lines = [f'*대시보드 매출(3팀 구분) · {start}~{end}*']
    if kind == 'month' and month:
        if month.get('closed') and month.get('actual'):
            values = month['actual']
            lines += [bullet_summary(f'마감확정 {compact_won(sum(values[k] for _,k in selected))} · {" + ".join(label for label,_ in selected)}')]
            lines += ['• 마감확정: ' + won(sum(values[k] for _,k in selected))]
            lines += [f'• {label} 확정: {won(values[key])}' for label,key in selected]
        else:
            f = month.get('forecast')
            raw = month.get('raw')
            if f or raw:
                parts = []
                if f:
                    parts.append(f'마감예측 {compact_won(sum(f[k] for _,k in selected))}')
                if raw:
                    parts.append(f'RAW {compact_won(sum(raw[k+"_won"] for _,k in selected))}({raw["as_of"]}까지)')
                lines += [bullet_summary(' · '.join(parts) + f' · {" + ".join(label for label,_ in selected)}')]
            if f:
                lines += ['• 마감예측(기존 모델): ' + won(sum(f[k] for _,k in selected))]
                lines += [f'• {label} 예측: {won(f[key])}' for label,key in selected]
                if len(selected)==3 and f.get('previous_total_won'):
                    lines += [f'• 예측 MoM {(f["total_won"]/f["previous_total_won"]-1)*100:+.1f}% · 전월 확정 대비']
            if raw:
                lines += [f'• RAW 누적({raw["as_of"]}까지): ' + won(sum(raw[k+'_won'] for _,k in selected))]
                lines += [f'• {label} RAW: {won(raw[key+"_won"])}' for label,key in selected]
                if f and raw['target_won'] and len(selected)==3:
                    lines += [f'• 월 목표 {won(raw["target_won"])} · 예상 달성률 {f["total_won"]/raw["target_won"]*100:.1f}%']
                unknown = [r for r in p['live'] if start.isoformat() <= r['date'] <= min(end.isoformat(),p['as_of']) and r['unknown_party'] and not r['free']]
                if unknown and any(k=='live' for _,k in selected):
                    lines += [f'⚠️ 라이브 귀속 미확인 {len(unknown)}건은 RAW 합계에서 제외. 매출 없음으로 해석하지 마세요.']
    else:
        rows = [r for r in p['revenue']['days'] if start.isoformat() <= r['date'] <= end.isoformat()]
        if not rows:
            return lines + ['• 해당 기간 수집된 RAW 없음 · 확인 못 함']
        values = {k:sum(r[k+'_won'] for r in rows) for _,k in teams}
        lines += [bullet_summary(f'기간 RAW {compact_won(sum(values[k] for _,k in selected))} · {" + ".join(label for label,_ in selected)} · 확정/월전체 예측 아님')]
        lines += [f'• 기간 RAW({rows[0]["date"]}~{rows[-1]["date"]}): {won(sum(values[k] for _,k in selected))}']
        lines += [f'• {label} RAW: {won(values[key])}' for label,key in selected]
        lines += ['• 일·주 RAW는 확정 매출이나 월전체 예측이 아닙니다.']
    lines += ['• 예측=revenue.v_revenue_forecast_monthly · 확정=integrated_ssot · RAW=대시보드 동일 귀속·취소 필터']
    return lines


def youtube_answer(p, q, start, end, kind):
    yt = p['youtube']
    rows = [r for r in yt['content'] if start.isoformat() <= r['publish_date'] <= end.isoformat()]
    lf = bool(re.search(r'롱폼|\bLF\b|\blf\b',q))
    sf = bool(re.search(r'숏폼|\bSF\b|\bsf\b',q))
    form_filter = 'LF' if lf and not sf else 'SF' if sf and not lf else None
    if form_filter:
        rows = [r for r in rows if r['form']==form_filter]
    complete = [r for r in rows if r['d7_complete'] and r['d7_views'] is not None]
    lines = [f'*유튜브 · {start}~{end}*']
    wants_detail = bool(re.search(r'상세|영상별|콘텐츠별|잘된|상위|top|D7|D\+7|d7|d\+7', q))
    wants_verbose_period = bool(re.search(r'포맷|구성|분해|breakdown', q))
    if '편성' in q:
        if form_filter:
            raise ValueError('편성 원천은 전체 월 기준으로 조회해주세요. 폼 조건을 전체 합계로 바꾸지 않습니다.')
        schedule = yt['schedules'].get(start.strftime('%Y-%m'))
        if kind != 'month' or not schedule:
            return lines + ['• 편성 원천은 월 단위로 조회해주세요. 예: 9월 유튜브 편성']
        c = schedule['coverage']
        lines += [bullet_summary(f'편성 {c["source_count"]}건 · 발행 연결 {c["matched_count"]}건 · 예정 {c["planned"]}건 · 미매칭 {c["unmatched"]}건')]
        lines += [f'• 편성 원천 {c["source_count"]}건 · 발행 연결 {c["matched_count"]}건 · 예정 {c["planned"]}건 · 미매칭 {c["unmatched"]}건',
                  f'• 커뮤니티 확인 {c["community"]}건 · 콘텐츠 미정 {c["slot"]}건 · 날짜만 있는 빈 구좌 {c["empty_slot_count"]}건(별도)',
                  f'• 시트 조회 {c["captured_at"]} · 주월간 대시보드 / 편성·개별 성과 아카이빙']
        return lines + discovered_lines(yt.get('verified_api',{}),start,end)
    official = next((r for r in yt['periods'] if r['period_start']==start.isoformat() and r['period_end']==end.isoformat()), None)
    if yt.get('verified_api'):
        lines += verified_youtube_answer(yt['verified_api'],start,end,form_filter,verbose=wants_verbose_period)
    elif official and official['available'] and not form_filter:
        lines += [f'• 채널 총조회수: {number(official["views"])}회 · '+('마감' if official['period_complete'] else '진행 중'),
                  f'• 실제 집계: {official["metric_start_date"]}~{official["metric_end_date"]}']
        if wants_verbose_period:
            lines += [f'• 기간 조회수: {number(official["views"])}회']
        prev_start = (start-dt.timedelta(days=1)).replace(day=1) if kind=='month' else start-dt.timedelta(days=7)
        previous = next((r for r in yt['periods'] if r['period_start']==prev_start.isoformat() and r['period_type']==official['period_type']),None)
        if official['period_complete'] and previous and previous['available'] and previous['period_complete'] and previous['views']:
            lines += [f'• {"MoM" if kind=="month" else "WoW"} {(official["views"]/previous["views"]-1)*100:+.1f}% · 완료 기간끼리 비교']
    else:
        lines += ['• '+(form_filter+' 조회수' if form_filter else '채널 조회수')+': 확인 못 함(해당 기간 Analytics 없음). 발행 영상 누적·D7로 대신하지 않습니다.']
    forms = {f:sum(r['form']==f for r in rows) for f in ('LF','SF')}
    lines += [f'• 발행 구성: 총 {len(rows)}건 · LF {forms["LF"]} / SF {forms["SF"]} / 기타 {len(rows)-sum(forms.values())}']
    lines += [f'• D+7 완료 {len(complete)}/{len(rows)}건 · 평균 {number(sum(r["d7_views"] for r in complete)/len(complete)) if complete else "—"}회']
    if not form_filter and (wants_detail or wants_verbose_period):
        lines += discovered_lines(yt.get('verified_api',{}),start,end)
    sub = [r for r in yt['subscribers'] if r['date'] <= min(end.isoformat(),p['as_of'])]
    if sub and wants_detail:
        lines += [f'• 구독자 {number(sub[-1]["count"])}명 · 수집 {sub[-1]["date"]}']
    if wants_detail or kind == 'day':
        ranked = sorted(rows, key=lambda r: (r['d7_views'] is not None, r['d7_views'] or 0), reverse=True)
        if re.search(r'잘된|상위|top', q):
            ranked = [next((r for r in ranked if r['form']==f), None) for f in ('LF','SF')] if ('롱폼' in q and '숏폼' in q) else ranked[:5]
            ranked = [r for r in ranked if r is not None]
        lines += ['• 상세: D+7 Analytics 기준. 최신 누적과 구분합니다.']
        for r in ranked[:20]:
            vid = r['video_id']
            link = f'<https://www.youtube.com/watch?v={vid}|영상>' if re.fullmatch('[A-Za-z0-9_-]{11}',vid) else '링크 확인 필요'
            lines += [f'• {r["publish_date"]} {safe(r["form"])} {safe(r["title"])} · D7 {number(r["d7_views"])} / 현재 누적 {number(r["views_total"])} · {link}']
        if len(ranked)>20:
            lines += [f'• 총 {len(ranked)}건 중 20건 표시. 주·일별로 나누어 조회하세요.']
    period_text = ''
    api = yt.get('verified_api')
    if api:
        api_period = next((r for r in api['periods'] if r['period_start']==str(start) and r['period_end']==str(end)),None)
        if api_period:
            kind_key = {'LF':'videoOnDemand','SF':'shorts'}.get(form_filter)
            value = api_period['formats'].get(kind_key,0) if kind_key else api_period['views']
            period_text = f'조회수 {number(value)}회({api_period["metric_end_date"]}까지)'
        else:
            kind_key = {'LF':'videoOnDemand','SF':'shorts'}.get(form_filter)
            if str(start) >= api['coverage_start']:
                stop = min(str(end), api['actual_end'])
                if str(start) > stop:
                    period_text = '조회수 집계 대기'
                else:
                    coverage=[r['date'] for r in api['daily'] if str(start)<=r['date']<=stop]
                    expected=(dt.date.fromisoformat(stop)-start).days+1
                    if len(set(coverage))==expected and len(coverage)==expected:
                        api_rows=api['daily_formats'] if kind_key else api['daily']
                        value=sum(r['views'] for r in api_rows if str(start)<=r['date']<=stop and (not kind_key or r['form']==kind_key))
                        period_text = f'조회수 {number(value)}회'+(f'({stop}까지)' if stop<str(end) else '')
    elif official and official['available'] and not form_filter:
        period_text = f'조회수 {number(official["views"])}회({official["metric_end_date"]}까지)'
    if not period_text:
        period_text = '조회수 확인 못 함'
    lines.insert(1, bullet_summary(f'{period_text} · 발행 {len(rows)}건 · D+7 완료 {len(complete)}/{len(rows)}건'))
    return lines


def answer(packet, question, domain, today=None):
    if domain not in ('live','youtube','ads') or not candidate(question,domain):
        return None
    today = today or dt.datetime.now(KST).date()
    try:
        start,end,kind = period(question,today)
        if start.year != int(packet['as_of'][:4]):
            raise ValueError('대시보드 연결 연도 밖입니다.')
        if start.isoformat() < packet.get('coverage_start',packet['as_of'][:4]+'-01-01'):
            raise ValueError('현재 대시보드와 대조 완료한 기간 밖입니다. 전월 이후로 조회해주세요.')
        if domain != 'live':
            check_scope(question,domain)
        renderer = {'live':live_answer,'youtube':youtube_answer,'ads':revenue_answer}[domain]
        lines = renderer(packet,question,start,end,kind)
        return '\n'.join(lines)
    except (ValueError,KeyError,TypeError) as exc:
        return '🙏 *확인 필요*\n• '+safe(str(exc))+'\n• 검증되지 않은 조건을 전체 합계로 바꾸지 않습니다.'


def check_scope(q, domain):
    clean = unicodedata.normalize('NFKC',q).lower()
    clean = re.sub(r'\d{4}[-년.]|\d{1,2}(?:주차|[월일주/.-])|이번\s*달|이번\s*주|지난\s*주|지난\s*달|오늘|어제|금월|금주|전월|차월|다음\s*달|저번\s*주|저번\s*달', '', clean)
    terms = ('유튜브|채널|광고주별|브랜드별|지면별|구좌별|일반광고|통합광고|통광마|라이브|광고|mbd|대시보드|'
             '성과|실적|매출|조회수|구독자|마감예측치|마감예측|마감|예측|확정|raw|누적|현황|상세|지표|편성|전체|월간|주간|일간|'
             '합계|총|건수|발행|영상별|콘텐츠별|가장|잘된|롱폼|숏폼|lf|sf|d\\+?7|상위|top|하나씩|하나|각각|랑|'
             '알려줘|알려주세요|보여줘|보여주세요|어때|얼마|알려|는|은|을|를|의|과|와|좀|해줘|있어|몇|개|명')
    clean = re.sub(terms,'',clean)
    if re.sub(r'[\s?.,!~·()+0-9]+','',clean):
        raise ValueError('요청한 브랜드·범위·조건을 확정하지 못했습니다. 지원 기준을 명시해주세요.')


def main():
    req = json.loads(sys.stdin.read(8000))
    domain,q = req['domain'],req['question']
    if not candidate(q,domain):
        print(json.dumps({'answer':None}));return
    try:
        raw = (Path(__file__).resolve().parent/'data/metrics.json').read_bytes()
        if len(raw)>MAX_BYTES:
            raise ValueError('공통 지표 크기 검증 실패')
        packet = json.loads(raw)
        request = urllib.request.Request(URL+'?metrics='+packet['dashboard_sha256'][:12],headers={'Cache-Control':'no-cache'})
        with urllib.request.urlopen(request,timeout=10) as response:
            public = response.read(MAX_BYTES+1)
        validate(packet,dt.datetime.now(KST),public)
        result = answer(packet,q,domain)
    except Exception:
        result = '🙏 *확인 필요*\n• 대시보드와 공통 지표의 버전·신선도를 확인하지 못했습니다.\n• 다른 집계나 추정 숫자로 대체하지 않습니다.'
    print(json.dumps({'answer':result},ensure_ascii=False))


if __name__ == '__main__':
    main()
