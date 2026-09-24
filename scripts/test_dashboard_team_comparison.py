import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dashboard_forecast_state as forecast
import dashboard_team_comparison as comparison
import refresh_live_daily_from_duckdb as daily
import verify_dashboard as guard
import test_dashboard_forecast_state as fixture_module


class TeamComparisonTest(unittest.TestCase):
    def setUp(self):
        fixture = fixture_module.ForecastStateTest()
        fixture.setUp()
        self.html = fixture.html
        self.raw = dict(fixture.raw, previous_same_period={
            'as_of': '2026-08-09', 'ad_gen_won': 200000000,
            'ad_int_won': 40000000, 'live_won': 10000000, 'total_won': 250000000})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'fixture.duckdb'
            fixture._canonical_fixture(path)
            self.pred = forecast.fetch_forecast(path, dt.date(2026, 9, 9))

    def test_real_arithmetic_two_distinct_baselines_and_idempotency(self):
        rendered = forecast.update_forecast_surfaces(self.html, self.raw, self.pred)
        detail = guard._month_surface(rendered, 'mvr', 9)
        for expected in ('MoM ▲ 8.0%', 'MoM ▲ 186.5%', 'MoM ▼ 16.7%',
                         'MoM ▲ 11.4%', 'MoM ▼ 50.0%', 'MoM ▼ 100.0%',
                         '8/1~8/31 확정치 · 8.68억', '8/1~8/9 RAW · 2억'):
            self.assertIn(expected, detail)
        self.assertNotIn('비교 기준 확인 필요', detail)
        self.assertLess(detail.index('data-team-mom="raw-ad_gen"'), detail.index('data-team-mom="forecast-ad_gen"'))
        self.assertEqual(guard.verify(rendered, dt.datetime.now(daily.KST), allow_stale_sources=guard.OPTIONAL_STALE_SOURCES), [])
        self.assertEqual(rendered, forecast.update_forecast_surfaces(rendered, self.raw, self.pred))
        for group in ('mvk', 'mvr', 'mvs'):
            self.assertEqual(guard._month_surface(self.html, group, 8), guard._month_surface(rendered, group, 8))

    def test_zero_missing_and_flat_are_not_conflated(self):
        cutoff = dt.date(2026, 8, 31)
        for previous in (None, 0):
            row = comparison.comparison_row('forecast', 'live', 123, previous, cutoff)
            self.assertIn('MoM —', row)
            self.assertNotIn('0.0%', row)
        self.assertIn('비교값 없음', comparison.comparison_row('forecast', 'live', 123, None, cutoff))
        self.assertIn('전월 0원으로 증감률 산출 불가', comparison.comparison_row('forecast', 'live', 123, 0, cutoff))
        self.assertIn('MoM 0.0%', comparison.comparison_row('forecast', 'live', 123, 123, cutoff))
        self.assertIn('MoM ▼ 100.0%', comparison.comparison_row('forecast', 'live', 0, 123, cutoff))

    def test_absent_prior_team_does_not_become_zero(self):
        fixture = fixture_module.ForecastStateTest()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'fixture.duckdb'
            fixture._canonical_fixture(path)
            con = duckdb.connect(str(path))
            con.execute("delete from revenue.integrated_ssot where revenue_team='통합광고'")
            con.close()
            pred = forecast.fetch_forecast(path, dt.date(2026, 9, 9))
        self.assertIsNone(pred['previous_actual']['ad_int_won'])
        self.assertIsNone(pred['previous_total_won'])
        self.assertEqual(pred['previous_actual']['ad_gen_won'], 868200000)
        rendered = forecast.update_forecast_surfaces(self.html, self.raw, pred)
        self.assertEqual(guard.verify(rendered, dt.datetime.now(daily.KST), allow_stale_sources=guard.OPTIONAL_STALE_SOURCES), [])
        detail = guard._month_surface(rendered, 'mvr', 9)
        self.assertIn('MoM ▲ 8.0%', detail)
        self.assertIn('확정치 · 비교값 없음', detail)

    def test_month_end_leap_and_year_boundaries(self):
        for as_of, actual_end, raw_end in (
            ('2026-01-31', '2025-12-31', '2025-12-31'),
            ('2026-03-31', '2026-02-28', '2026-02-28'),
            ('2024-03-30', '2024-02-29', '2024-02-29'),
            ('2026-09-13', '2026-08-31', '2026-08-13'),
        ):
            with self.subTest(as_of=as_of):
                raw = dict(self.raw, as_of=as_of, previous_same_period=None)
                pred = dict(self.pred, previous_actual=None)
                text = comparison.team_comparisons(raw, pred, 'ad_gen')
                self.assertIn(f'data-comparison-as-of="{actual_end}"', text)
                self.assertIn(f'data-comparison-as-of="{raw_end}"', text)

    def test_payload_and_rendered_tampering_rejected(self):
        for mutation in ({'total_won': 1}, {'as_of': '2026-07-31'}, {'source': 'ad_gen.booking_pred'}, {'ad_gen_won': -1}):
            pred = dict(self.pred, previous_actual=dict(self.pred['previous_actual'], **mutation))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                forecast.update_forecast_surfaces(self.html, self.raw, pred)
        rendered = forecast.update_forecast_surfaces(self.html, self.raw, self.pred)
        for before, after in (
            ('MoM ▲ 186.5%', 'MoM ▲ 999.0%'),
            ('8/1~8/31 확정치 · 8.68억', '8/1~8/9 확정치 · 8.68억'),
            ('data-prior-ad-gen-won="868200000"', 'data-prior-ad-gen-won="1"'),
            ('data-team-mom="raw-ad_gen"', 'data-team-mom="raw-live"'),
            ('RAW 전월 동일기간', 'RAW 전월 전체'),
        ):
            with self.subTest(before=before):
                bad = rendered.replace(before, after, 1)
                self.assertNotEqual(bad, rendered)
                self.assertTrue(any('comparison invalid' in e for e in guard.verify(bad, dt.datetime.now(daily.KST))))


if __name__ == '__main__':
    unittest.main()
