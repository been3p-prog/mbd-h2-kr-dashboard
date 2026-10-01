import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
import dashboard_forecast_state as forecast
import dashboard_next_booking as booking
import verify_dashboard as guard
import test_dashboard_forecast_state as fixture


class NextBookingTest(unittest.TestCase):
    def setUp(self):
        # [2026-09-30] Reuse ForecastStateTest's September-forced html (see
        #   dashboard_calendar_fixture.synthetic_month_state) instead of
        #   re-reading the checked-in index.html directly: October must stay
        #   data-phase="future" for update_next_booking to accept it as the
        #   next-booking target, independent of the real artifact's momentary
        #   current month.
        forecast_fixture = fixture.ForecastStateTest()
        forecast_fixture.setUp()
        self.html = forecast_fixture.html
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'test.duckdb'
        fixture.ForecastStateTest()._canonical_fixture(self.path)
        con = duckdb.connect(str(self.path))
        con.execute("insert into revenue.v_revenue_forecast_monthly select '2026-10',team_code,case team_code when 'ad_gen' then 536500000 when 'ad_int' then 30000000 when 'live' then 226000000 else 792500000 end,source_table,source_column,rule_id from revenue.v_revenue_forecast_monthly")
        con.execute('create schema meta')
        con.execute('create table meta.targets(ym varchar, team varchar, metric varchar, kind varchar, value_num double)')
        con.executemany("insert into meta.targets values ('2026-10',?,'매출','target',?)",
                        [('ad_gen',865682548),('ad_int',300000000),('live',212000000)])
        con.close()

    def test_next_booking_updates_preview_target_month_chart_and_teams_idempotently(self):
        result = forecast.fetch_forecast(self.path, dt.date(2026,9,10), include_next=True)
        booked = result['next_booking']
        self.assertEqual(booked['total_won'], 792500000)
        self.assertEqual(booked['target_won'], 1377682548)
        text = booking.update_next_booking(self.html, dt.date(2026,9,10), booked)
        self.assertEqual(text, booking.update_next_booking(text, dt.date(2026,9,10), booked))
        side = guard._month_surface(text,'mvs',9)
        for marker in ('차월 부킹 · 10월','3,000만','2.26억','13.78억','57.5%'):
            self.assertIn(marker, side)
        self.assertNotIn('미편성',side)
        for group in ('mvk','mvr'):
            self.assertEqual(guard._month_surface(text,group,8),guard._month_surface(self.html,group,8))
        self.assertIn('7.92억',guard._month_surface(text,'mvk',10))
        self.assertIn('2.26억',guard._month_surface(text,'mvr',10))
        self.assertIn('7.9',guard._gauge_surface(text,10))
        self.assertEqual(guard.verify(text,dt.datetime.now().astimezone(), allow_stale_sources=guard.OPTIONAL_STALE_SOURCES),[])
        for group, selected, before, after in [
            ('mvs',9,'<div>라이브<b>2.26억</b></div>','<div>라이브<b>미편성</b></div>'),
            ('mvk',10,'<div class="v num">7.92억</div>','<div class="v num">3.54억</div>'),
            ('mvr',10,'<div class="bigv num">2.26억</div>','<div class="bigv num">0</div>')]:
            surface = guard._month_surface(text,group,selected)
            bad = text.replace(surface,surface.replace(before,after,1),1)
            self.assertNotEqual(bad,text)
            self.assertIn('next booking surfaces invalid: month 9',guard.verify(bad,dt.datetime.now().astimezone()))

    # [2026-10-01] 차월 라이브 예약 0건: 행은 없지만 canonical TOTAL이 나머지 합과 같으면 검증된 0으로 렌더
    def test_next_live_absent_with_confirming_total_renders_zero(self):
        con = duckdb.connect(str(self.path))
        con.execute("delete from revenue.v_revenue_forecast_monthly where ym='2026-10' and team_code='live'")
        con.execute("update revenue.v_revenue_forecast_monthly set forecast_revenue=566500000 where ym='2026-10' and team_code='MBD_TOTAL'")
        con.close()
        booked = forecast.fetch_forecast(self.path, dt.date(2026,9,10), include_next=True)['next_booking']
        self.assertEqual((booked['live'], booked['total_won']), (0, 566500000))
        text = booking.update_next_booking(self.html, dt.date(2026,9,10), booked)
        self.assertEqual(guard.verify(text,dt.datetime.now().astimezone(), allow_stale_sources=guard.OPTIONAL_STALE_SOURCES),[])

    def test_missing_next_live_is_not_zero_and_missing_target_fails(self):
        con = duckdb.connect(str(self.path))
        con.execute("delete from revenue.v_revenue_forecast_monthly where ym='2026-10' and team_code='live'")
        con.close()
        with self.assertRaisesRegex(ValueError,'missing teams'):
            forecast.fetch_forecast(self.path,dt.date(2026,9,10),include_next=True)

    def test_missing_target_fails_and_december_does_not_reuse_january(self):
        con = duckdb.connect(str(self.path))
        con.execute("delete from meta.targets where team='live'")
        con.close()
        with self.assertRaisesRegex(ValueError,'targets'):
            forecast.fetch_forecast(self.path,dt.date(2026,9,10),include_next=True)
        self.assertEqual(booking.attach_next_booking(self.path,dt.date(2026,12,10),{'x':1}),{'x':1})

    def test_wrong_period_and_closed_target_are_rejected(self):
        booked = forecast.fetch_forecast(self.path,dt.date(2026,9,10),include_next=True)['next_booking']
        with self.assertRaisesRegex(ValueError,'period'):
            booking.update_next_booking(self.html,dt.date(2026,9,11),booked)
        altered = self.html.replace('class="mvk mv" data-m="10" data-phase="future"',
                                    'class="mvk mv" data-m="10" data-phase="closed"')
        with self.assertRaisesRegex(ValueError,'overwrite'):
            booking.update_next_booking(altered,dt.date(2026,9,10),booked)


if __name__ == '__main__':
    unittest.main()
