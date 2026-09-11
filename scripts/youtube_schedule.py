"""Read-only schedule SoT capture, local snapshot and independent coverage checks.

No Sheet writes or mutations of the canonical YouTube database. Scheduled rows
remain separate from published-video facts and their performance denominators.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import duckdb

ROOT = Path(__file__).resolve().parents[1]
KST = dt.timezone(dt.timedelta(hours=9))
DEFAULT_CREDENTIAL = Path('/Users/sb.lee/automations/slack-digest/credentials/ohouse-drive-cnc-a87f58a854c8.json')


def source_contract():
    document = json.loads((ROOT / 'data/source_contract.json').read_text())
    if document.get('schema') != 'mbd-dashboard-sot-v1':
        raise RuntimeError('unsupported dashboard source contract')
    source = document['sources']['youtube_schedule']
    return document['year'], source


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def parse_date(value, year):
    text = str(value or '').strip()
    if not text or '주차' in text:
        return None
    match = re.match(r'^(?:(\d{4})\s*[-./년]\s*)?(\d{1,2})\s*[-./월]\s*(\d{1,2})(?:\s*[일.]|\s|$)', text)
    if not match:
        return None
    return dt.date(int(match[1] or year), int(match[2]), int(match[3]))


def identity(raw_id, raw_url, form):
    """Accept only exact IDs and allowlisted URLs, never arbitrary source links."""
    raw_id, raw_url = str(raw_id or '').strip(), str(raw_url or '').strip()
    community = form == '커뮤니티'
    pattern = r'Ug[A-Za-z0-9_-]+' if community else r'[A-Za-z0-9_-]{11}'
    url_id = ''
    if raw_url:
        parsed = urlparse(raw_url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port or parsed.hostname not in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'}:
            raise RuntimeError('schedule contains a non-allowlisted URL')
        if community:
            match = re.fullmatch(r'/post/(Ug[A-Za-z0-9_-]+)/?', parsed.path)
            url_id = match[1] if match else ''
        elif parsed.hostname == 'youtu.be':
            url_id = parsed.path.strip('/')
        elif parsed.path == '/watch':
            values = parse_qs(parsed.query).get('v', [])
            url_id = values[0] if len(values) == 1 else ''
        else:
            match = re.fullmatch(r'/(?:shorts|live)/([A-Za-z0-9_-]{11})/?', parsed.path)
            url_id = match[1] if match else ''
        if not re.fullmatch(pattern, url_id):
            raise RuntimeError('schedule contains an unresolvable YouTube URL')
    if raw_id and not re.fullmatch(pattern, raw_id):
        raise RuntimeError('schedule contains an invalid source ID')
    if raw_id and url_id and raw_id != url_id:
        raise RuntimeError('schedule ID and URL conflict')
    return raw_id or url_id


def parse_sheet(values, *, captured_at, year, source):
    header_index = source['header_row'] - 1
    if len(values) <= header_index or values[header_index][:7] != source['headers']:
        raise RuntimeError('schedule header missing or changed; absence is not an empty schedule')
    rows, empty_slots, seen_ids = [], [], set()
    for number, raw in enumerate(values[source['data_start_row'] - 1:], source['data_start_row']):
        cells = (raw + [''] * 7)[:7]
        has_content = any(str(cells[i] or '').strip() for i in (0, 3, 4, 5, 6))
        try:
            day = parse_date(cells[1], year)
        except ValueError:
            raise RuntimeError(f'invalid schedule date at row {number}') from None
        if not has_content:
            if day and day.year == year:
                empty_slots.append(day.isoformat())
            continue
        if day is None:
            raise RuntimeError(f'schedule content has no attributable date at row {number}')
        if day.year != year:
            raise RuntimeError(f'schedule year differs from source contract at row {number}')
        form = str(cells[3] or '').strip() or '미정'
        item_id = identity(cells[0], cells[6], form)
        if item_id and item_id in seen_ids:
            raise RuntimeError('duplicate schedule ID; cannot safely merge separate rows')
        if item_id:
            seen_ids.add(item_id)
        rows.append(dict(key=digest([source['spreadsheet_id'], source['sheet_id'], number])[:24],
                         sheet_row=number, date=day.isoformat(), time=str(cells[2] or '').strip(),
                         form=form, ip=str(cells[4] or '').strip(), title=str(cells[5] or '').strip(),
                         item_id=item_id, community=form == '커뮤니티'))
    payload = dict(schema='youtube-schedule-snapshot-v1', year=year,
                   spreadsheet_id=source['spreadsheet_id'], sheet_id=source['sheet_id'],
                   sheet_title=source['sheet_title'], captured_at=captured_at,
                   source_rows=len(rows), empty_slots=empty_slots, rows=rows)
    payload['sha256'] = digest(payload)
    return payload


def fetch_sheet(*, credential=DEFAULT_CREDENTIAL, session=None, now=None):
    # Lazy Google imports keep offline CI/tests independent of authentication.
    year, source = source_contract()
    now = now or dt.datetime.now(KST)
    if now.year != year:
        raise RuntimeError('schedule source contract requires year rollover review')
    if session is None:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        creds = service_account.Credentials.from_service_account_file(
            str(credential), scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
        session = AuthorizedSession(creds)
    endpoint = 'https://sheets.googleapis.com/v4/spreadsheets/' + source['spreadsheet_id']
    response = session.get(endpoint, params={'fields': 'sheets(properties(sheetId,title,gridProperties(rowCount)))'}, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(f'schedule metadata read failed HTTP {response.status_code}')
    matches = [s['properties'] for s in response.json().get('sheets', []) if s['properties']['sheetId'] == source['sheet_id']]
    if len(matches) != 1 or matches[0]['title'] != source['sheet_title']:
        raise RuntimeError('schedule tab identity changed or missing')
    last_row = matches[0]['gridProperties']['rowCount']
    if not isinstance(last_row, int) or not source['header_row'] <= last_row <= 100000:
        raise RuntimeError('schedule source row boundary invalid')
    range_name = "'" + source['sheet_title'].replace("'", "''") + "'!A1:G" + str(last_row)
    response = session.get(endpoint + '/values/' + quote(range_name, safe=''),
                           params={'valueRenderOption': 'FORMATTED_VALUE'}, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(f'schedule values read failed HTTP {response.status_code}')
    return parse_sheet(response.json().get('values', []), captured_at=now.isoformat(timespec='seconds'), year=year, source=source)


def store_snapshot(path, payload):
    con = duckdb.connect(str(path))
    try:
        con.execute('begin transaction')
        con.execute('create table if not exists dashboard_youtube_schedule (payload varchar)')
        con.execute('delete from dashboard_youtube_schedule')
        con.execute('insert into dashboard_youtube_schedule values (?)', [json.dumps(payload, ensure_ascii=False)])
        con.execute('commit')
    finally:
        con.close()


def load_snapshot(con, *, now):
    try:
        records = con.execute('select payload from dashboard_youtube_schedule').fetchall()
    except duckdb.CatalogException:
        raise RuntimeError('YouTube schedule source not connected; cannot claim full coverage') from None
    if len(records) != 1:
        raise RuntimeError('schedule snapshot must contain one verified capture')
    payload = json.loads(records[0][0])
    checksum = payload.pop('sha256', None)
    year, source = source_contract()
    if checksum != digest(payload) or payload.get('source_rows') != len(payload.get('rows', [])):
        raise RuntimeError('schedule snapshot integrity mismatch')
    if payload.get('schema') != 'youtube-schedule-snapshot-v1' or payload.get('year') != year or any(payload.get(k) != source[k] for k in ('spreadsheet_id', 'sheet_id', 'sheet_title')):
        raise RuntimeError('schedule snapshot does not match source contract')
    captured = dt.datetime.fromisoformat(payload['captured_at'])
    if captured.tzinfo is None or not -300 <= (now - captured).total_seconds() <= source['freshness_hours'] * 3600:
        raise RuntimeError('schedule capture is stale or has invalid clock')
    payload['sha256'] = checksum
    return payload


def reconcile(payload, published, *, year, month, as_of, known_videos=None):
    """Account for every source row, without manufacturing publication facts."""
    source_rows = [r for r in payload['rows'] if dt.date.fromisoformat(r['date']).month == month]
    published_ids = {str(row['video_id']) for row in published}
    if len(published_ids) != len(published):
        raise RuntimeError('duplicate published video IDs')
    matched = set()
    extras = []
    by_video = {}
    known_videos = known_videos or {}
    for row in source_rows:
        item_id = row['item_id']
        if not row['community'] and item_id in published_ids:
            matched.add(item_id)
            by_video[item_id] = row
            continue
        date = dt.date.fromisoformat(row['date'])
        is_slot = not (row['title'] or row['ip'] or item_id)
        other = known_videos.get(item_id) if not row['community'] else None
        state = ('other_period' if other and other['publish_date'] <= as_of else
                 'slot' if is_slot else 'planned' if date > as_of else
                 'community' if row['community'] else 'unmatched')
        extras.append(dict(row, state=state, actual_date=other['publish_date'].isoformat() if state == 'other_period' else None))
    counts = {state: sum(r['state'] == state for r in extras) for state in ('planned', 'unmatched', 'community', 'slot', 'other_period')}
    coverage = dict(source_count=len(source_rows), matched_count=len(matched), extra_count=len(extras),
                    published_unmatched_count=len(published_ids - matched), published_count=len(published),
                    empty_slot_count=sum(dt.date.fromisoformat(day).month == month for day in payload['empty_slots']),
                    rendered_count=len(published) + len(extras), captured_at=payload['captured_at'],
                    source_sha256=payload['sha256'], source_keys=sorted(r['key'] for r in source_rows), **counts)
    if coverage['source_count'] != coverage['matched_count'] + coverage['extra_count']:
        raise RuntimeError('schedule coverage mismatch')
    return dict(coverage=coverage, extras=extras, by_video=by_video)


def extra_activity(row):
    labels = {'planned': '편성 예정', 'unmatched': '발행 확인 대기', 'community': '커뮤니티 · 게시 확인 필요',
              'slot': '콘텐츠 미정 구좌', 'other_period': '다른 기간 발행 확인 · ' + str(row.get('actual_date') or '')}
    title = row['title'] or row['ip'] or '콘텐츠 미정'
    meta = ' · '.join(filter(None, [row['form'], row['ip'], row['time'], labels[row['state']]]))
    day = dt.date.fromisoformat(row['date'])
    return (f'<div class="activity-row" data-yt-schedule-key="{row["key"]}" data-yt-schedule-state="{row["state"]}">'
            f'<time class="activity-date" datetime="{row["date"]}" data-yt-scheduled="true">{day.month}/{day.day}</time>'
            '<div class="activity-main activity-main-inline"><span class="activity-title-line">'
            f'<b class="content-title">{html.escape(title)}</b><small class="activity-inline-meta">{html.escape(meta)}</small></span></div>'
            '<div class="activity-metric metric-trio num">' + '<span class="metric-cell"><b>—</b></span>' * 3 + '</div></div>')


def coverage_note(result):
    c = result['coverage']
    attrs = ' '.join(f'data-yt-schedule-{key.replace("_", "-")}="{html.escape(str(value), quote=True)}"'
                     for key, value in c.items() if key != 'source_keys')
    return (f'<div class="plan-note" {attrs}><b>편성 원천 {c["source_count"]}건</b> · 영상 연결 {c["matched_count"]}건'
            f' · 편성 예정 {c["planned"]}건 · 발행 확인 대기 {c["unmatched"]}건 · 커뮤니티 확인 {c["community"]}건'
            f' · 콘텐츠 미정 {c["slot"]}건 · 다른 기간 발행 {c["other_period"]}건'
            f' · 시트 당월 편성과 미매칭인 발행 {c["published_unmatched_count"]}건'
            f' · 날짜만 입력된 빈 구좌 {c["empty_slot_count"]}건(편성 건수 제외)'
            f' · 시트 조회 {html.escape(c["captured_at"])} · 취소 상태 열 없음(자동 판정 안 함)</div>')


def verify_coverage(block, expected=None, *, now=None, require_fresh=False):
    """Pure DOM/source equality check used before writes and by release guards."""
    marker = re.search(r'<div class="plan-note" (?=[^>]*data-yt-schedule-source-count=)([^>]+)>', block)
    if not marker:
        raise RuntimeError('YouTube schedule coverage metadata missing')
    values = dict(re.findall(r'data-yt-schedule-([a-z0-9-]+)="([^"]*)"', marker[1]))
    count_keys = ('source_count', 'matched_count', 'extra_count', 'published_unmatched_count', 'published_count',
                  'empty_slot_count', 'rendered_count', 'planned', 'unmatched', 'community', 'slot', 'other_period')
    counts = {key: int(values[key.replace('_', '-')]) for key in count_keys}
    captured = dt.datetime.fromisoformat(values['captured-at'])
    if captured.tzinfo is None or not re.fullmatch(r'[0-9a-f]{64}', values.get('source-sha256', '')):
        raise RuntimeError('invalid schedule source identity or clock')
    if now is not None:
        _, source = source_contract()
        age = (now - captured).total_seconds()
        if age < -300 or (require_fresh and age > source['freshness_hours'] * 3600):
            raise RuntimeError('schedule source capture is stale or future')
    if min(counts.values()) < 0:
        raise RuntimeError('negative schedule count')
    keys = re.findall(r'data-yt-schedule-key="([^"]+)"', block)
    states = re.findall(r'data-yt-schedule-state="([^"]+)"', block)
    extra_rows = re.findall(r'<div class="activity-row" [^>]*data-yt-schedule-state="[^"]+"[^>]*>.*?(?=<div class="activity-row"|</details>)', block, re.S)
    if len(extra_rows) != len(states) or any(
            re.findall(r'<span class="metric-cell"><b>(.*?)</b></span>', row, re.S) != ['—'] * 3
            or 'href=' in row for row in extra_rows):
        raise RuntimeError('schedule-only rows must have three unavailable metrics and no publication links')
    published = block.count('data-content-link="youtube"')
    if (any(not re.fullmatch(r'[0-9a-f]{24}', key) for key in keys) or
            any(state not in {'planned', 'unmatched', 'community', 'slot', 'other_period'} for state in states) or
            len(keys) != len(set(keys)) or len(keys) != counts['source_count'] or published != counts['published_count'] or
            counts['source_count'] != counts['matched_count'] + counts['extra_count'] or
            published != counts['matched_count'] + counts['published_unmatched_count'] or
            len(states) != counts['extra_count'] or published + len(states) != counts['rendered_count'] or
            any(states.count(key) != counts[key] for key in ('planned', 'unmatched', 'community', 'slot', 'other_period'))):
        raise RuntimeError('YouTube schedule source-to-DOM count mismatch')
    if expected and (counts != {key: expected[key] for key in count_keys} or sorted(keys) != expected['source_keys'] or values.get('source-sha256') != expected['source_sha256'] or values['captured-at'] != expected['captured_at']):
        raise RuntimeError('YouTube schedule source-to-DOM identity mismatch')
