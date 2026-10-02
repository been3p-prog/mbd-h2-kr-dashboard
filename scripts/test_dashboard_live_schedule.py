import datetime as dt
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

import duckdb
from dashboard_live_schedule import (
    RETRO_BODY_END_MARKER,
    RETRO_STYLE,
    RETRO_TRIGGER_ICON,
    RETRO_TRIGGER_LABEL,
    WEEK_BOUNDARY_CLOSE,
    _retro_html,
    fetch_schedule,
    update_schedule,
)


def _specificity(selector):
    """CSS 특이도 (id, class/attr/pseudo-class, element) — 주간 회고 리셋 검증 전용 최소 구현.

    [2026-10-02] 중첩 `.wk-retro>summary`가 일반 `.week-group summary`에 눌려 행 높이가
    42→51px로 부푼 회귀를 "순서가 아니라 특이도로" 막았는지 단정하기 위해 추가.
    """
    return (len(re.findall(r'#[\w-]+', selector)),
            len(re.findall(r'\.[\w-]+|\[[^\]]+\]|:(?!:)[\w-]+', selector)),
            len(re.findall(r'(?:^|[\s>+~])([a-zA-Z][\w-]*)', selector)))


class FullLiveScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'fixture.duckdb'
        con = duckdb.connect(str(self.db))
        con.execute('create schema live')
        con.execute('''create table live.raw_slots(
            "온에어 일자" varchar, "브랜드명" varchar, "패키지" varchar, "PGM" varchar,
            "라이브 시청자 (비로그인 포함)" varchar, "일 전체 GMV (라이브 브랜드 전체)" varchar,
            "라이브 1H GMV" varchar, "비고 (프로모션)" varchar,
            "공식 회고" varchar, "회고 발송" varchar, "타사 라이브 이력" varchar,
            "내부회고" varchar)''')
        con.executemany('insert into live.raw_slots values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', [
            ('2026-09-02', '완료', '스마트', '일반', '1,000', '100000000', None, 'private note',
             '<script>alert("x")</script> & "quote"\n둘째 줄', 'TRUE', 'A&B <타사>', 'PRIVATE_SENTINEL_7b2f'),
            # [2026-10-02] 실적 반영 행이면서 공식 회고만 비어 있다 — 발송 TRUE·타사 이력만으로는
            # 어떤 UI도 만들지 않음을 이 행으로 증명한다(픽스처 건수/지표는 그대로 유지).
            ('2026-09-03', '회고 대기', '스마트', '일반', '10', '1000', '500', '',
             '', 'TRUE', '타사만 있음', 'PRIVATE_SENTINEL_7b2f'),
            ('2026-09-09', '대기', '스마트', '일반', None, '0', '0', '', '', '', '', 'PRIVATE_SENTINEL_7b2f'),
            ('2026-09-29', '예정<x>', '스마트', '일반', '999', '999', '999', '', '미래 회고 노출 금지', 'TRUE', '미래 타사', 'PRIVATE_SENTINEL_7b2f'),
            ('2026-09-29', '예정<x>', '에센셜', '일반', None, None, None, '', '', '', '', 'PRIVATE_SENTINEL_7b2f'),
            ('2026-09-30', '무료', '무료', '일반', None, None, None, '', '', '', '', 'PRIVATE_SENTINEL_7b2f'),
            ('2026-09-30', '취소 브랜드', '스마트', '일반', None, None, None, 'cancelled', '', '', '', 'PRIVATE_SENTINEL_7b2f'),
            ('2026-10-01', '차월', '스마트', '일반', None, None, None, '', '', '', '', 'PRIVATE_SENTINEL_7b2f'),
        ])
        con.close()
        self.document = (Path(__file__).resolve().parents[1] / 'index.html').read_text()
        self.as_of = dt.date(2026, 9, 10)

    def tearDown(self):
        self.tmp.cleanup()

    def render(self):
        return update_schedule(self.document, fetch_schedule(self.db, 2026, 9), as_of=self.as_of)

    def ledger(self, document):
        block = document.split('<div class="mvr mv" data-m="9"', 1)[1]
        return block.split('data-content-ledger="live">', 1)[1].split('<div class="card quality-card yt-quality">', 1)[0]

    def row_chunks(self, ledger):
        """원장을 `<div class="activity-row">` 리터럴 경계로 쪼갠 행 조각 목록."""
        return re.findall(
            r'<div class="activity-row">.*?(?=<div class="activity-row">|</div></details>)',
            ledger, flags=re.DOTALL)

    def test_all_source_slots_with_empty_future_metrics_and_calendar_weeks(self):
        rows = fetch_schedule(self.db, 2026, 9)
        self.assertEqual(len(rows), 6)
        ledger = self.ledger(self.render())
        # [2026-10-02] 여는 태그를 정확 리터럴로 검증 — 다른 렌더러/가드가
        # `<div class="activity-row">`를 정확 일치로 스캔하므로 data-* 속성이 붙으면
        # 매칭 행이 0건이 된다. 느슨한 부분문자열 카운트는 이 회귀를 잡지 못했다.
        self.assertEqual(ledger.count('<div class="activity-row">'), 6)
        self.assertNotIn('data-live-schedule-row', ledger)
        # 행 식별은 커스텀 ID 대신 (일자, 타이틀) 쌍으로 한다
        self.assertEqual(sorted(re.findall(
            r'<div class="activity-row"><time class="activity-date" datetime="([^"]+)"[^>]*>'
            r'.*?<b class="content-title">([^<]+)</b>', ledger, flags=re.DOTALL)), [
                ('2026-09-02', '완료'),
                ('2026-09-03', '회고 대기'),
                ('2026-09-09', '대기'),
                ('2026-09-29', '예정&lt;x&gt;'),
                ('2026-09-29', '예정&lt;x&gt;'),
                ('2026-09-30', '무료'),
            ])
        self.assertEqual(len(re.findall(r'data-week-group="9-\d" open', ledger)), 5)
        self.assertIn('data-live-main-source-measured="2"', ledger)
        self.assertIn('data-live-main-source-pending="1"', ledger)
        self.assertIn('data-live-main-source-future="3"', ledger)
        self.assertIn('예정&lt;x&gt;', ledger)
        self.assertNotIn('999', ledger)
        self.assertNotIn('취소 브랜드', ledger)
        self.assertNotIn('private note', ledger)
        self.assertIn('실적 집계 대기', ledger)
        self.assertIn('무료 · 품질 평균 제외', ledger)
        self.assertNotIn('<b>0</b>', ledger)

    def test_completed_review_escape_multiline_and_future_negative_control(self):
        ledger = self.ledger(self.render())
        self.assertEqual(ledger.count('<details class="wk-retro">'), 1)
        self.assertEqual(ledger.count('<div class="wk-retro-body">'), 1)
        self.assertEqual(ledger.count(f'</div>{RETRO_BODY_END_MARKER}</details>'), 1)
        self.assertEqual(ledger.count('<div class="wk-retro-pop" aria-hidden="true">'), 1)
        self.assertIn('&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &quot;quote&quot;\n둘째 줄', ledger)
        self.assertIn('A&amp;B &lt;타사&gt;', ledger)
        self.assertNotIn('<script>alert', ledger)
        self.assertNotIn('미래 회고 노출 금지', ledger)
        self.assertNotIn('미래 타사', ledger)
        self.assertNotIn('PRIVATE_SENTINEL_7b2f', ledger)
        self.assertNotIn('내부회고', ledger)

    def test_touch_control_is_one_inline_icon_without_a_visible_status_line(self):
        # [2026-10-02] 16:05 Been 피드백 — 보이는 "회고 · 발송 완료"/대기 줄을 완전히 제거하고
        # 메타데이터 라인 끝에 아이콘 하나만 남긴다. 행 높이는 6303e38과 같아야 한다.
        ledger = self.ledger(self.render())
        for forbidden in ('회고 · ', '발송 완료', '발송 대기', '입력 대기', '<summary>회고'):
            self.assertNotIn(forbidden, ledger.split('<div class="wk-retro-body">')[0])
        self.assertNotIn('<summary>', ledger)
        self.assertIn(
            f'<summary aria-label="{RETRO_TRIGGER_LABEL}" title="{RETRO_TRIGGER_LABEL}">'
            f'<span aria-hidden="true">{RETRO_TRIGGER_ICON}</span></summary>', ledger)
        # 아이콘은 activity-title-line 안, 메타데이터 small 바로 뒤에 인라인으로 붙는다
        self.assertRegex(ledger, r'<small class="activity-inline-meta">[^<]*</small>'
                                 r'<details class="wk-retro">')
        self.assertRegex(ledger, r'</div>' + re.escape(RETRO_BODY_END_MARKER)
                         + r'</details></span></div>')
        self.assertNotIn('</span></div><details class="wk-retro">', ledger)

    def test_hover_pop_is_a_details_sibling_inside_the_same_activity_row(self):
        ledger = self.ledger(self.render())
        with_retro = [chunk for chunk in self.row_chunks(ledger) if 'wk-retro' in chunk]
        self.assertEqual(len(with_retro), 1)
        chunk = with_retro[0]
        self.assertEqual(chunk.count('<div class="wk-retro-pop" aria-hidden="true">'), 1)
        details = chunk[chunk.index('<details class="wk-retro">'):
                        chunk.index('</details>') + len('</details>')]
        # pop은 details 밖에 있고 같은 activity-row 안에 있다
        self.assertNotIn('wk-retro-pop', details)
        self.assertGreater(chunk.index('<div class="wk-retro-pop"'), chunk.index('</details>'))
        self.assertTrue(chunk.rstrip().endswith('</div></div>'), chunk)
        # pop 본문은 details 본문과 완전히 동일하다
        body = details.split('<div class="wk-retro-body">', 1)[1].split(
            f'</div>{RETRO_BODY_END_MARKER}', 1)[0]
        pop = chunk.split('<div class="wk-retro-pop" aria-hidden="true">', 1)[1]
        self.assertEqual(pop[:len(body)], body)
        self.assertEqual(pop[len(body):].rstrip(), '</div></div>')

    def test_blank_official_review_renders_no_retrospective_affordance(self):
        ledger = self.ledger(self.render())
        for chunk in self.row_chunks(ledger):
            if '<b class="content-title">완료</b>' in chunk:
                continue
            for token in ('wk-retro', 'wk-retro-body', 'wk-retro-pop',
                          RETRO_TRIGGER_ICON, RETRO_TRIGGER_LABEL):
                self.assertNotIn(token, chunk, chunk[:200])
        # [2026-10-02] 발송 TRUE·타사 이력만 있는 행도 UI를 만들지 않는다 — 공식 회고가 유일한
        # 게이트. 실적 반영(has_result) 행이어야 _retro_html이 실제로 호출되므로 상태도 함께 검증.
        sent_only = [chunk for chunk in self.row_chunks(ledger)
                     if '<b class="content-title">회고 대기</b>' in chunk]
        self.assertEqual(len(sent_only), 1)
        self.assertIn('실적 반영', sent_only[0])
        self.assertNotIn('타사만 있음', ledger)

    def test_retro_html_pairs_details_with_an_identical_pop_body(self):
        details, pop = _retro_html({'official_review': 'a <b> & "c"',
                                    'review_sent': True,
                                    'competitor_live_history': '타사 <x>'})
        body = details.split('<div class="wk-retro-body">', 1)[1].split(
            f'</div>{RETRO_BODY_END_MARKER}', 1)[0]
        self.assertEqual(pop, f'<div class="wk-retro-pop" aria-hidden="true">{body}</div>')
        self.assertEqual(body, '<b>공식 회고</b>\na &lt;b&gt; &amp; &quot;c&quot;'
                               '\n\n<b>회고 발송</b>\n발송 완료'
                               '\n\n<b>타사 라이브 이력</b>\n타사 &lt;x&gt;')
        self.assertEqual(
            _retro_html({'official_review': '  ', 'review_sent': True,
                         'competitor_live_history': '타사'}), ('', ''))
        self.assertEqual(
            _retro_html({'official_review': '', 'review_sent': False,
                         'competitor_live_history': ''}), ('', ''))

    def test_nested_retro_never_emits_the_week_boundary_close_sequence(self):
        # [2026-10-02] refresh_live_daily_from_duckdb.update_live_activity_rows의 주차 삽입
        # 정규식은 `<div class="week-items">` 이후 첫 `</div></details>`를 주차 종료로 본다.
        # 중첩 wk-retro가 그 리터럴을 내보내면 주차 경계가 회고 닫기에 걸려 삽입 위치가 깨진다.
        ledger = self.ledger(self.render())
        retros = re.findall(r'<details class="wk-retro">.*?</details>', ledger, flags=re.DOTALL)
        self.assertEqual(len(retros), 1)
        for retro in retros:
            # 필수 마크업은 유지되어야 한다
            self.assertTrue(retro.startswith('<details class="wk-retro"><summary '), retro)
            self.assertIn('<div class="wk-retro-body">', retro)
            self.assertTrue(retro.endswith(f'</div>{RETRO_BODY_END_MARKER}</details>'), retro)
            # 어떤 wk-retro도 주차 경계와 충돌하는 리터럴 종료 시퀀스를 내보내지 않는다
            self.assertNotIn(WEEK_BOUNDARY_CLOSE, retro)
        # hover pop 역시 주차 경계 리터럴을 만들지 않는다
        for pop in re.findall(r'<div class="wk-retro-pop" aria-hidden="true">.*?</div>',
                              ledger, flags=re.DOTALL):
            self.assertNotIn(WEEK_BOUNDARY_CLOSE, pop)

        # 소비자와 동일한 정규식으로, 회고가 중첩된 주차에서도 경계가 주차 자신의 닫기에 걸리는지 검증.
        # 9월 1주차(9/1–9/7)에는 실적 반영 2건이 있고 공식 회고가 있는 1건만 회고를 중첩한다.
        match = re.compile(
            r'(<details class="week-group" data-week-group="9-1"[^>]*>.*?<div class="week-items">)'
            r'(?P<body>.*?)(' + re.escape(WEEK_BOUNDARY_CLOSE) + r')',
            re.DOTALL,
        ).search(ledger)
        self.assertIsNotNone(match, 'week 9-1 boundary not found')
        body = match.group('body')
        self.assertEqual(body.count('<div class="activity-row">'), 2)
        self.assertEqual(body.count('<details class="wk-retro">'), 1)
        # 주차 body 안에서 열린 details는 모두 같은 body 안에서 닫힌다 (경계 조기 종료 아님)
        self.assertEqual(body.count('</details>'), 1)
        self.assertEqual(body.count(RETRO_BODY_END_MARKER), 1)

    def test_native_details_css_is_idempotent_and_has_no_state_handlers(self):
        stale_style = '''<style data-wk-retro-style="native-v1">
.wk-retro-body{position:absolute;left:0;top:100%;max-height:28rem}
</style>'''
        document = re.sub(
            r'<style data-wk-retro-style="native-v1">.*?</style>',
            stale_style,
            self.document,
            flags=re.DOTALL,
        )
        rendered = update_schedule(document, fetch_schedule(self.db, 2026, 9), as_of=self.as_of)
        rerendered = update_schedule(rendered, fetch_schedule(self.db, 2026, 9), as_of=self.as_of)
        self.assertEqual(rerendered.count('data-wk-retro-style="native-v1"'), 1)
        self.assertEqual(rerendered.count(RETRO_STYLE), 1)
        self.assertNotIn(stale_style, rerendered)
        # tap은 native <details>의 open 상태로만 body를 연다
        self.assertIn('.wk-retro[open]>.wk-retro-body{display:block}', rerendered)
        # [2026-10-02] 행 class는 정확히 "activity-row" — desktop hover 셀렉터는 pop 하나뿐이고,
        # body를 노출하던 구 규칙과 그것을 되돌리던 :not([open]) 보정 규칙은 모두 사라져야 한다.
        self.assertIn('@media (hover:hover){.activity-row:hover .wk-retro-pop{display:block}}', rerendered)
        style = re.search(r'<style data-wk-retro-style="native-v1">.*?</style>',
                          rerendered, flags=re.DOTALL).group(0)
        self.assertNotIn('.activity-row:hover .wk-retro-body', style)
        self.assertNotIn('.activity-row:hover>.wk-retro-body', style)
        self.assertNotIn(':not([open])', style)
        # :hover를 포함한 줄은 pop 규칙 단 하나 — body를 노출/재숨김하는 hover 규칙이 전무하다
        self.assertEqual(
            [line for line in style.splitlines() if ':hover' in line],
            ['@media (hover:hover){.activity-row:hover .wk-retro-pop{display:block}}'])
        self.assertEqual(
            [line for line in RETRO_STYLE.splitlines() if ':hover' in line],
            ['@media (hover:hover){.activity-row:hover .wk-retro-pop{display:block}}'])
        # 스타일과 재생성된 원장 어디에도 wk-row 토큰이 남지 않아야 한다 (다른 렌더러가
        # class="activity-row" 정확 일치로 스캔하므로). 원장 밖 과거월 잔존은 다음 refresh가 정리.
        self.assertNotIn('wk-row', RETRO_STYLE)
        self.assertNotIn('wk-row', self.ledger(rerendered))
        self.assertRegex(rerendered, r'\.wk-retro-body\{[^}]*position:fixed')
        self.assertRegex(rerendered, r'\.wk-retro-body\{[^}]*left:50%;top:50%;transform:translate\(-50%,-50%\)')
        self.assertRegex(rerendered, r'\.wk-retro-body\{[^}]*max-height:min\(60vh,calc\(100vh - 2rem\)\)')
        self.assertRegex(rerendered, r'\.wk-retro-body\{[^}]*overflow:auto')
        # [2026-10-02] 원장 조각이 아닌 전체 문서/추출 스크립트로 검증 — 원장 밖(head·body 말미)에
        # 주입된 주간 회고 상태 핸들러를 ledger-only 검사가 놓치던 구멍을 막음
        scripts = '\n'.join(
            re.findall(r'<script\b[^>]*>(.*?)</script>', rerendered, flags=re.DOTALL | re.I))
        self.assertTrue(scripts.strip(), 'rerendered document exposes no scripts to audit')
        # 주간 회고는 순수 CSS + native <details> — 어떤 스크립트도 주간 셀렉터를 참조하면 안 됨
        self.assertNotIn('wk-retro', scripts)
        # 열림 상태를 JS로 제어하는 토큰은 문서 전체에서 금지
        for token in ("classList.contains('is-open')", "querySelector('.wk-retro')",
                      "closest('.wk-retro')", 'data-wk-retro-open', 'is-open'):
            self.assertNotIn(token, rerendered)
        # live-retro-trigger·aria-expanded·click 핸들러는 MTD 편성별 회고와 좌측 내비가 정당하게
        # 사용하므로 주간 원장 범위로 한정해 검증한다
        for token in ('live-retro-trigger', 'aria-expanded', "addEventListener('click'"):
            self.assertNotIn(token, self.ledger(rerendered))

        without_style = re.sub(
            r'<style data-wk-retro-style="native-v1">.*?</style>',
            '',
            self.document,
            flags=re.DOTALL,
        )
        inserted = update_schedule(without_style, fetch_schedule(self.db, 2026, 9), as_of=self.as_of)
        self.assertEqual(inserted.count(RETRO_STYLE), 1)
        self.assertLess(inserted.index(RETRO_STYLE), inserted.index('</head>'))

        deduplicated = update_schedule(
            document.replace(stale_style, stale_style + stale_style),
            fetch_schedule(self.db, 2026, 9),
            as_of=self.as_of,
        )
        self.assertEqual(deduplicated.count(RETRO_STYLE), 1)

    def rule(self, selector):
        """RETRO_STYLE에서 `selector{...}` 한 줄을 그대로 돌려준다."""
        prefix = selector + '{'
        matches = [line for line in RETRO_STYLE.splitlines() if line.startswith(prefix)]
        self.assertEqual(len(matches), 1, f'{selector!r} must appear exactly once')
        return matches[0]

    def test_nested_summary_reset_outranks_the_generic_week_summary_rule(self):
        # [2026-10-02] Chrome 1280 실측 회귀 — 6303e38의 9/7 베베숲 행은 height=42,
        # activity-title-line=16.875였는데 회고 아이콘 도입 후 51 / 42로 부풀었다.
        # 원인은 index.html의 일반 `.week-group summary`가 중첩 `.wk-retro>summary`를
        # 덮어써(display:flex, min-height:42px, padding:8px 12px) 아이콘 summary가
        # 42px 박스가 된 것. 두 셀렉터는 특이도가 같아 순서 의존이었다.
        # 먼저 우리가 리셋해야 하는 상대 규칙이 실제로 그 값으로 존재하는지 고정한다.
        self.assertRegex(self.document, r'\.week-group summary\{[^}]*display:flex')
        self.assertRegex(self.document, r'\.week-group summary\{[^}]*min-height:42px')
        self.assertRegex(self.document, r'\.week-group summary\{[^}]*padding:8px 12px')
        self.assertRegex(self.document, r'\.week-group summary\{[^}]*gap:12px')
        self.assertIn('.week-group summary:hover{background:#F8FAFC}', self.document)
        # 모바일 오버라이드도 같은 (0,1,1)이라 특이도로 함께 눌려야 한다
        self.assertIn('.week-group summary{min-height:38px;padding:7px 10px;gap:8px}',
                      self.document)

        reset = self.rule('.week-group .wk-retro>summary')
        for declaration in ('display:block', 'min-height:0', 'padding:0', 'border:0',
                            'background:none', 'gap:0', 'font-size:.75rem', 'line-height:1'):
            self.assertIn(declaration, reset, reset)

        # 특이도가 실제로 더 높아야 한다 — 같으면 style 순서가 바뀌는 순간 회귀가 재발한다
        self.assertEqual(_specificity('.week-group summary'), (0, 1, 1))
        self.assertGreater(_specificity('.week-group .wk-retro>summary'),
                           _specificity('.week-group summary'))
        # hover 배경 규칙과는 동률이므로, 동률을 이기려면 회고 style이 뒤에 와야 한다
        self.assertEqual(_specificity('.week-group .wk-retro>summary'),
                         _specificity('.week-group summary:hover'))
        rendered = self.render()
        self.assertLess(rendered.rindex('.week-group summary{'), rendered.index(RETRO_STYLE))

        # 리셋은 반드시 .wk-retro로 스코프된다 — 일반 주차 summary는 건드리지 않는다
        for line in RETRO_STYLE.splitlines():
            if '.week-group' in line:
                self.assertIn('.wk-retro', line.split('{', 1)[0], line)
        # 아이콘 자체는 높이를 만들지 않고 메타데이터 라인 끝 baseline에 붙는다
        host = self.rule('.wk-retro')
        for declaration in ('flex:0 0 auto', 'align-self:baseline', 'line-height:1'):
            self.assertIn(declaration, host, host)
        # tap(open)과 pop hover는 이 수정으로 변하지 않는다
        self.assertIn('.wk-retro[open]>.wk-retro-body{display:block}', RETRO_STYLE)
        self.assertEqual(
            [line for line in RETRO_STYLE.splitlines() if ':hover' in line],
            ['@media (hover:hover){.activity-row:hover .wk-retro-pop{display:block}}'])

    def test_hover_pop_never_opts_out_of_hit_testing(self):
        # [2026-10-02] pop에 pointer-events:none이 있으면 계산된 display가 block이어도
        # document.elementFromPoint가 pop을 반환하지 않아 "맨 위에 떠서 포인터에 잡히는지"를
        # 증명할 수 없다(display만 녹색인 가짜 통과). 되돌아오지 못하게 못 박는다.
        self.assertNotIn('pointer-events', self.rule('.wk-retro-pop'))
        self.assertNotIn('pointer-events:none', RETRO_STYLE)
        self.assertNotIn('pointer-events', RETRO_STYLE)

    def test_optional_review_columns_may_be_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'legacy.duckdb'
            con = duckdb.connect(str(db))
            con.execute('create schema live')
            con.execute('''create table live.raw_slots(
                "온에어 일자" varchar, "브랜드명" varchar, "패키지" varchar, "PGM" varchar,
                "라이브 시청자 (비로그인 포함)" varchar,
                "일 전체 GMV (라이브 브랜드 전체)" varchar, "라이브 1H GMV" varchar,
                "비고 (프로모션)" varchar)''')
            con.execute("insert into live.raw_slots values ('2026-09-02','legacy','스마트','일반','1','1','1','')")
            con.close()
            rows = fetch_schedule(db, 2026, 9)
        self.assertEqual(rows[0]['official_review'], '')
        self.assertFalse(rows[0]['review_sent'])
        self.assertEqual(rows[0]['competitor_live_history'], '')
        # [2026-10-02] 공식 회고가 비면 어떤 회고 어포던스도 만들지 않는다 — 과거의
        # "회고 입력 대기" 플레이스홀더를 기대하던 단정은 폐기.
        ledger = self.ledger(update_schedule(self.document, rows, as_of=self.as_of))
        for token in ('wk-retro', 'wk-retro-body', 'wk-retro-pop',
                      RETRO_TRIGGER_ICON, RETRO_TRIGGER_LABEL, '회고 입력 대기'):
            self.assertNotIn(token, ledger)

    def test_private_sentinel_absent_from_public_artifacts(self):
        rendered = self.render()
        root = Path(__file__).resolve().parents[1]
        public = rendered + (root / 'data' / 'live_window_contract.json').read_text(encoding='utf-8')
        self.assertEqual(public.count('PRIVATE_SENTINEL_7b2f'), 0)
        self.assertNotIn('내부회고', public)

    def test_rebuild_removes_rescheduled_or_deleted_rows_and_is_idempotent(self):
        first = self.render()
        con = duckdb.connect(str(self.db))
        con.execute('update live.raw_slots set "온에어 일자" = \'2026-09-15\' where "브랜드명" = \'예정<x>\'')
        con.execute('delete from live.raw_slots where "브랜드명" = \'무료\'')
        con.close()
        rows = fetch_schedule(self.db, 2026, 9)
        second = update_schedule(first, rows, as_of=self.as_of)
        ledger = self.ledger(second)
        self.assertNotIn('datetime="2026-09-29"', ledger)
        self.assertEqual(ledger.count('datetime="2026-09-15"'), 2)
        self.assertEqual(update_schedule(second, rows, as_of=self.as_of), second)
        self.assertEqual(first.split('<div class="mvr mv" data-m="9"')[0], second.split('<div class="mvr mv" data-m="9"')[0])

    def test_empty_month_and_wrong_month_fail_closed(self):
        ledger = self.ledger(update_schedule(self.document, [], as_of=self.as_of))
        self.assertEqual(ledger.count('원천에 등록된 편성 없음'), 5)
        rows = fetch_schedule(self.db, 2026, 9)
        rows[0]['date'] = dt.date(2026, 8, 1)
        with self.assertRaisesRegex(RuntimeError, 'outside'):
            update_schedule(self.document, rows, as_of=self.as_of)

    def test_late_results_reconcile_previously_current_ledger_after_rollover(self):
        from dashboard_quality_history import update_live_quality_history
        from verify_dashboard import _check_live_schedule
        document = self.render()
        con = duckdb.connect(str(self.db))
        con.execute('update live.raw_slots set "일 전체 GMV (라이브 브랜드 전체)" = \'200000000\' where "브랜드명" = \'대기\'')
        con.close()
        with mock.patch('refresh_live_daily_from_duckdb.fetch_live_rows', return_value=([], None)), \
             mock.patch('dashboard_quality_history.update_live_quality_summary', side_effect=lambda text, *args, **kwargs: text):
            result = update_live_quality_history(document, self.db, 2026, 10, dt.date(2026, 10, 1))
        ledger = self.ledger(result)
        self.assertIn('data-live-main-source-as-of="2026-09-30"', ledger)
        self.assertIn('data-live-main-source-snapshot-date="2026-10-01"', ledger)
        self.assertIn('data-live-main-source-measured="4"', ledger)
        self.assertIn('data-live-main-source-future="0"', ledger)
        errors = []
        _check_live_schedule(result, errors)
        self.assertEqual(errors, [])

    def test_release_guard_detects_count_and_week_tampering(self):
        from verify_dashboard import _check_live_schedule
        document = self.render()
        for before, after in [('data-live-main-source-count="6"', 'data-live-main-source-count="7"'),
                              ('data-week-group="9-5" open', 'data-week-group="9-5"')]:
            with self.subTest(before=before):
                errors = []
                _check_live_schedule(document.replace(before, after), errors)
                self.assertTrue(errors)


if __name__ == '__main__':
    unittest.main()
