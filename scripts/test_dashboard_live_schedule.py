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
    WEEK_BOUNDARY_CLOSE,
    fetch_schedule,
    update_schedule,
)


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
            ('2026-09-03', '회고 대기', '스마트', '일반', '10', '1000', '500', '', '', '', '', 'PRIVATE_SENTINEL_7b2f'),
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

    def test_completed_review_pending_escape_multiline_and_future_negative_control(self):
        ledger = self.ledger(self.render())
        self.assertEqual(ledger.count('<details class="wk-retro"><summary>'), 2)
        self.assertEqual(ledger.count('<div class="wk-retro-body">'), 2)
        self.assertEqual(ledger.count(f'</div>{RETRO_BODY_END_MARKER}</details>'), 2)
        self.assertIn('<summary>회고 · 발송 완료</summary>', ledger)
        self.assertIn('<summary>회고 · 입력 대기</summary>', ledger)
        self.assertIn('<b>공식 회고</b>\n회고 입력 대기', ledger)
        self.assertIn('&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &quot;quote&quot;\n둘째 줄', ledger)
        self.assertIn('A&amp;B &lt;타사&gt;', ledger)
        self.assertNotIn('<script>alert', ledger)
        self.assertNotIn('미래 회고 노출 금지', ledger)
        self.assertNotIn('미래 타사', ledger)
        self.assertNotIn('PRIVATE_SENTINEL_7b2f', ledger)
        self.assertNotIn('내부회고', ledger)

    def test_nested_retro_never_emits_the_week_boundary_close_sequence(self):
        # [2026-10-02] refresh_live_daily_from_duckdb.update_live_activity_rows의 주차 삽입
        # 정규식은 `<div class="week-items">` 이후 첫 `</div></details>`를 주차 종료로 본다.
        # 중첩 wk-retro가 그 리터럴을 내보내면 주차 경계가 회고 닫기에 걸려 삽입 위치가 깨진다.
        ledger = self.ledger(self.render())
        retros = re.findall(r'<details class="wk-retro">.*?</details>', ledger, flags=re.DOTALL)
        self.assertEqual(len(retros), 2)
        for retro in retros:
            # 필수 마크업은 유지되어야 한다
            self.assertTrue(retro.startswith('<details class="wk-retro"><summary>'), retro)
            self.assertIn('<div class="wk-retro-body">', retro)
            self.assertTrue(retro.endswith(f'</div>{RETRO_BODY_END_MARKER}</details>'), retro)
            # 어떤 wk-retro도 주차 경계와 충돌하는 리터럴 종료 시퀀스를 내보내지 않는다
            self.assertNotIn(WEEK_BOUNDARY_CLOSE, retro)

        # 소비자와 동일한 정규식으로, 회고가 중첩된 주차에서도 경계가 주차 자신의 닫기에 걸리는지 검증.
        # 9월 1주차(9/1–9/7)에는 실적 반영 2건이 있어 회고 2개가 모두 이 주차에 중첩된다.
        match = re.compile(
            r'(<details class="week-group" data-week-group="9-1"[^>]*>.*?<div class="week-items">)'
            r'(?P<body>.*?)(' + re.escape(WEEK_BOUNDARY_CLOSE) + r')',
            re.DOTALL,
        ).search(ledger)
        self.assertIsNotNone(match, 'week 9-1 boundary not found')
        body = match.group('body')
        self.assertEqual(body.count('<div class="activity-row">'), 2)
        self.assertEqual(body.count('<details class="wk-retro">'), 2)
        # 주차 body 안에서 열린 details는 모두 같은 body 안에서 닫힌다 (경계 조기 종료 아님)
        self.assertEqual(body.count('</details>'), 2)
        self.assertEqual(body.count(RETRO_BODY_END_MARKER), 2)

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
        self.assertIn('.wk-retro[open]>.wk-retro-body{display:block}', rerendered)
        # [2026-10-02] 행 class는 정확히 "activity-row" — hover는 해당 셀렉터로 스코프된다
        self.assertIn('@media (hover:hover){.activity-row:hover .wk-retro-body{display:block}}', rerendered)
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
        self.assertIn('회고 입력 대기', self.ledger(update_schedule(self.document, rows, as_of=self.as_of)))

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
