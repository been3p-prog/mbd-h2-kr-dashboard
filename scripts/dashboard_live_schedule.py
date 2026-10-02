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


def _review_text(value):
    lines = [line.rstrip() for line in str(value or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    while lines and not lines[-1]:
        lines.pop()
    return '\n'.join(lines).strip()


def _review_sent(value):
    return str(value or '').strip().upper() == 'TRUE'


def fetch_schedule(db_path, year, month):
    start = dt.date(year, month, 1)
    end = dt.date(year, month, calendar.monthrange(year, month)[1])
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        source_columns = {
            row[0] for row in con.execute(
                "select column_name from information_schema.columns "
                "where table_schema='live' and table_name='raw_slots'"
            ).fetchall()
        }
        review_expr = '"공식 회고"' if '공식 회고' in source_columns else 'NULL'
        sent_expr = '"회고 발송"' if '회고 발송' in source_columns else 'NULL'
        competitor_expr = '"타사 라이브 이력"' if '타사 라이브 이력' in source_columns else 'NULL'
        rows = con.execute(f'''
            select try_cast("온에어 일자" as date), "브랜드명", "패키지", "PGM",
                   "라이브 시청자 (비로그인 포함)",
                   "일 전체 GMV (라이브 브랜드 전체)", "라이브 1H GMV",
                   regexp_matches(lower(concat_ws(' ', "패키지", "PGM", "비고 (프로모션)")), '무상|무료|free'),
                   {review_expr}, {sent_expr}, {competitor_expr}
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
                 gmv_1h=_metric(hour_gmv), free=bool(free),
                 official_review=_review_text(official_review),
                 review_sent=_review_sent(review_sent),
                 competitor_live_history=_review_text(competitor_live_history))
            for (d, brand, package, pgm, viewers, day_gmv, hour_gmv, free,
                 official_review, review_sent, competitor_live_history) in rows]


RETRO_STYLE_MARKER = 'data-wk-retro-style="native-v1"'
# [2026-10-02] hover는 details 밖 형제 .wk-retro-pop만 띄우고(fixed·최상위), tap은 native
# <details>의 .wk-retro-body를 연다 — 조상의 overflow/stacking에 갇히지 않게 하려고 본문을
# 두 벌로 복제한다. 아이콘 하나만 보이도록 native disclosure marker는 숨긴다.
# [2026-10-02] .wk-retro-body의 hover 노출과 그것을 되돌리던 :not([open]) 보정 규칙을 전부
# 삭제 — body는 tap(open)으로만 열린다. desktop hover 셀렉터는 pop 한 줄만 남긴다.
# [2026-10-02] .wk-retro-pop의 pointer-events:none 제거 — 그 상태로는 계산된 display가
# block이어도 document.elementFromPoint가 pop을 절대 반환하지 않아 "실제로 맨 위에 떠서
# 포인터에 잡히는지"를 증명할 수 없었다(display만 녹색인 가짜 통과). pop은 activity-row의
# DOM 자손이라 포인터가 pop 위로 올라가도 .activity-row:hover가 유지돼 노출이 깜빡이지 않는다.
# [2026-10-02] `.week-group .wk-retro>summary` 리셋 추가 — 행 높이 회귀 수정.
# Chrome 1280 실측: 6303e38의 9/7 베베숲 행 height=42 / activity-title-line=16.875가
# 회고 아이콘 도입 후 51 / 42로 부풀었다. 원인은 index.html의 일반 규칙
# `.week-group summary{display:flex;gap:12px;min-height:42px;padding:8px 12px}`.
# 중첩된 `.wk-retro>summary`는 특이도가 (0,1,1)로 같고 min-height·padding·display를
# 되돌리지 않아, 순서와 무관하게 그 박스 모델을 그대로 물려받아 42px 박스가 되었다.
# 클래스를 하나 더 얹어 (0,2,1)로 올려 모바일 오버라이드(min-height:38px)까지 확정
# 재정의하고, `.week-group summary:hover{background:#F8FAFC}`(동일 (0,2,1))는 이 style
# 요소가 본 stylesheet 뒤에 오므로 순서로 이긴다. 셀렉터에 .wk-retro가 반드시 들어가므로
# 일반 주차 summary는 영향을 받지 않는다. details tap(open)과 pop hover는 그대로 둔다.
RETRO_STYLE = '''<style data-wk-retro-style="native-v1">
.wk-retro{display:inline-block;flex:0 0 auto;align-self:baseline;margin-left:.45rem;line-height:1;vertical-align:middle}
.wk-retro>summary{cursor:pointer;color:var(--sub);font-size:.75rem;line-height:1;list-style:none}
.week-group .wk-retro>summary{display:block;min-height:0;margin:0;padding:0;border:0;background:none;gap:0;font-size:.75rem;line-height:1;white-space:nowrap}
.wk-retro>summary::-webkit-details-marker{display:none}
.wk-retro-body{display:none;position:fixed;z-index:20;left:50%;top:50%;transform:translate(-50%,-50%);width:min(32rem,calc(100vw - 2rem));max-height:min(60vh,calc(100vh - 2rem));overflow:auto;box-sizing:border-box;padding:.75rem;margin:0;border:1px solid var(--line);border-radius:.65rem;background:var(--card);box-shadow:0 12px 30px rgba(15,23,42,.18);white-space:pre-wrap;text-align:left}
.wk-retro[open]>.wk-retro-body{display:block}
.wk-retro-pop{display:none;position:fixed;z-index:30;left:50%;top:50%;transform:translate(-50%,-50%);width:min(32rem,calc(100vw - 2rem));max-height:min(60vh,calc(100vh - 2rem));overflow:auto;box-sizing:border-box;padding:.75rem;margin:0;border:1px solid var(--line);border-radius:.65rem;background:var(--card);box-shadow:0 12px 30px rgba(15,23,42,.18);white-space:pre-wrap;text-align:left}
@media (hover:hover){.activity-row:hover .wk-retro-pop{display:block}}
</style>'''
RETRO_STYLE_PATTERN = re.compile(
    r'<style data-wk-retro-style="native-v1">.*?</style>', re.DOTALL
)
# [2026-10-02] 주차 경계 리터럴 충돌 방지용 구분자 — refresh_live_daily_from_duckdb의
# update_live_activity_rows 주차 삽입 정규식이 `<div class="week-items">` 이후 첫
# `</div></details>`를 주차 종료로 보기 때문에, 중첩된 wk-retro가 같은 시퀀스를
# 내보내면 주차 경계가 모호해진다. 무해한 HTML 주석을 body 닫기와 details 닫기
# 사이에 끼워 넣어 주차 경계가 계속 유일하게 식별되도록 한다.
RETRO_BODY_END_MARKER = '<!--wk-retro-body-->'
WEEK_BOUNDARY_CLOSE = '</div></details>'
# [2026-10-02] 16:05 Been 피드백 — 보이는 "회고 · 발송 완료"/대기 줄을 제거하고 메타데이터
# 라인 끝에 아이콘 하나만 남긴다. 상태 문구는 aria-label/title로만 노출해 행 높이를 유지한다.
# [2026-10-02] 트리거 아이콘은 이모지(🗒) 대신 ⓘ — 플랫폼별 이모지 렌더 폭/베이스라인 차이로
# 메타데이터 라인 높이가 흔들리던 문제를 피하고 텍스트 글리프로 폭을 고정한다.
RETRO_TRIGGER_ICON = 'ⓘ'
RETRO_TRIGGER_LABEL = '주간 회고 보기'


def _retro_html(row):
    """(details, pop) 쌍을 돌려준다 — 공식 회고가 비었으면 어떤 UI도 만들지 않는다."""
    # [2026-10-02] 발송 TRUE·타사 이력만으로는 회고 UI를 만들지 않는다 (공식 회고가 유일한 게이트).
    review = (row.get('official_review') or '').strip()
    if not review:
        return ('', '')
    competitor = (row.get('competitor_live_history') or '').strip()
    body = (f'<b>공식 회고</b>\n{html.escape(review, quote=True)}'
            f'\n\n<b>회고 발송</b>\n{"발송 완료" if row.get("review_sent") else "발송 대기"}')
    if competitor:
        body += f'\n\n<b>타사 라이브 이력</b>\n{html.escape(competitor, quote=True)}'
    # 필수 마크업(<details><summary><div class="wk-retro-body">)은 그대로 유지하고,
    # body 닫기와 details 닫기 사이에만 RETRO_BODY_END_MARKER를 삽입한다.
    details = (
        f'<details class="wk-retro">'
        f'<summary aria-label="{RETRO_TRIGGER_LABEL}" title="{RETRO_TRIGGER_LABEL}">'
        f'<span aria-hidden="true">{RETRO_TRIGGER_ICON}</span></summary>'
        f'<div class="wk-retro-body">{body}</div>'
        f'{RETRO_BODY_END_MARKER}</details>'
    )
    # hover용 복제본 — details 밖, activity-row의 직계 형제로 붙어 조상 overflow를 벗어난다.
    return details, f'<div class="wk-retro-pop" aria-hidden="true">{body}</div>'


def update_schedule(document, rows, *, as_of, source_as_of=None):
    from refresh_live_daily_from_duckdb import _month_bounds, fmt_m_d, fmt_won

    year, month = as_of.year, as_of.month
    source_as_of = source_as_of or as_of
    if RETRO_STYLE_MARKER in document:
        match = RETRO_STYLE_PATTERN.search(document)
        if not match:
            raise RuntimeError('Live retrospective style boundary changed; retain last-good dashboard')
        document = (document[:match.start()] + RETRO_STYLE
                    + RETRO_STYLE_PATTERN.sub('', document[match.end():]))
    else:
        document = document.replace('</head>', RETRO_STYLE + '</head>', 1)
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
            retro_details, retro_pop = _retro_html(row) if has_result else ('', '')
            # [2026-10-02] 행 여는 태그를 정확히 `<div class="activity-row">`로 유지 — 다른
            # 렌더러/가드(refresh_live_daily_from_duckdb의 주차 삽입 등)가 이 리터럴을 정확
            # 일치로 스캔하므로 class 변경이나 data-* 속성 추가는 매칭 행을 0건으로 만든다.
            # 행 식별은 내부 ID 대신 <time datetime>과 content-title로 한다.
            # [2026-10-02] 주간 회고 hover는 .activity-row:hover .wk-retro-pop 하나로만 스코프한다
            # — body는 tap(open) 전용이므로 hover가 body를 건드리면 안 된다.
            # [2026-10-02] 아이콘(details)은 메타데이터 small 바로 뒤 activity-title-line 안에
            # 인라인으로 붙이고, hover 복제본(pop)은 activity-row 직계 자식으로 details 밖에 둔다.
            items.append('<div class="activity-row">'
                         f'<time class="activity-date" datetime="{row["date"].isoformat()}">{fmt_m_d(row["date"])}</time>'
                         '<div class="activity-main activity-main-inline"><span class="activity-title-line">'
                         f'<b class="content-title">{html.escape(row["brand"])}</b>'
                         f'<small class="activity-inline-meta">{html.escape(meta)}</small>{retro_details}</span></div>'
                         '<div class="activity-metric metric-trio num">' + ''.join(metrics) + '</div>'
                         f'{retro_pop}</div>')
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
