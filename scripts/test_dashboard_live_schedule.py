import datetime as dt
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

import duckdb
from dashboard_live_schedule import fetch_schedule, update_schedule


class FullLiveScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'fixture.duckdb'
        con = duckdb.connect(str(self.db))
        con.execute('create schema live')
        con.execute('''create table live.raw_slots(
            "온에어 일자" varchar, "브랜드명" varchar, "패키지" varchar, "PGM" varchar,
            "라이브 시청자 (비로그인 포함)" varchar, "일 전체 GMV (라이브 브랜드 전체)" varchar,
            "라이브 1H GMV" varchar, "비고 (프로모션)" varchar)''')
        con.executemany('insert into live.raw_slots values (?, ?, ?, ?, ?, ?, ?, ?)', [
            ('2026-09-02', '완료', '스마트', '일반', '1,000', '100000000', None, 'private note'),
            ('2026-09-09', '대기', '스마트', '일반', None, '0', '0', ''),
            ('2026-09-29', '예정<x>', '스마트', '일반', '999', '999', '999', ''),
            ('2026-09-29', '예정<x>', '에센셜', '일반', None, None, None, ''),
            ('2026-09-30', '무료', '무료', '일반', None, None, None, ''),
            ('2026-09-30', '취소 브랜드', '스마트', '일반', None, None, None, 'cancelled'),
            ('2026-10-01', '차월', '스마트', '일반', None, None, None, ''),
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
        self.assertEqual(len(rows), 5)
        ledger = self.ledger(self.render())
        self.assertEqual(ledger.count('class="activity-row"'), 5)
        self.assertEqual(len(re.findall(r'data-week-group="9-\d" open', ledger)), 5)
        self.assertIn('data-live-main-source-measured="1"', ledger)
        self.assertIn('data-live-main-source-pending="1"', ledger)
        self.assertIn('data-live-main-source-future="3"', ledger)
        self.assertIn('예정&lt;x&gt;', ledger)
        self.assertNotIn('999', ledger)
        self.assertNotIn('취소 브랜드', ledger)
        self.assertNotIn('private note', ledger)
        self.assertIn('실적 집계 대기', ledger)
        self.assertIn('무료 · 품질 평균 제외', ledger)
        self.assertNotIn('<b>0</b>', ledger)

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
        self.assertIn('data-live-main-source-measured="3"', ledger)
        self.assertIn('data-live-main-source-future="0"', ledger)
        errors = []
        _check_live_schedule(result, errors)
        self.assertEqual(errors, [])

    def test_release_guard_detects_count_and_week_tampering(self):
        from verify_dashboard import _check_live_schedule
        document = self.render()
        for before, after in [('data-live-main-source-count="5"', 'data-live-main-source-count="6"'),
                              ('data-week-group="9-5" open', 'data-week-group="9-5"')]:
            with self.subTest(before=before):
                errors = []
                _check_live_schedule(document.replace(before, after), errors)
                self.assertTrue(errors)


if __name__ == '__main__':
    unittest.main()
