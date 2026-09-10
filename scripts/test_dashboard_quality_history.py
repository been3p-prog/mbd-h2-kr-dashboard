import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import dashboard_quality_history as history
import refresh_live_daily_from_duckdb as live
import refresh_owned_youtube_window_from_duckdb as youtube
import test_finalize_month_review as close_fixture
import verify_dashboard as guard
from finalize_month_review_from_duckdb import finalize_month_review

ROOT = Path(__file__).resolve().parents[1]


class QualityHistoryTest(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "index.html").read_text()

    def row(self, month, day, value, package="smart"):
        return {"date": dt.date(2026, month, day), "brand": "품질 회귀 fixture",
                "package": "스마트", "package_key": package, "pgm": "",
                "viewers": 100, "gmv_1d": value, "gmv_1h": 10}

    def test_prior_and_current_quality_refresh_preserves_revenue_and_is_idempotent(self):
        rows = {7: [self.row(7, 31, 100_000_000)],
                8: [self.row(8, 31, 200_000_000)],
                9: [self.row(9, 1, 300_000_000)]}
        with patch.object(live, "fetch_live_rows", side_effect=lambda db, y, m, **kw: (rows.get(m, []), None)):
            result = history.update_live_quality_history(self.html, Path("fixture"), 2026, 9, dt.date(2026, 9, 9))
            self.assertEqual(result, history.update_live_quality_history(result, Path("fixture"), 2026, 9, dt.date(2026, 9, 9)))
        for month, total in ((8, 200_000_000), (9, 300_000_000)):
            before_start, before_end = live._month_bounds(self.html, "mvr", month)
            after_start, after_end = live._month_bounds(result, "mvr", month)
            before, after = self.html[before_start:before_end], result[after_start:after_end]
            marker = '<div class="card quality-card live-quality">'
            self.assertEqual(before.split(marker)[0], after.split(marker)[0])
            self.assertIn(f'data-live-quality-source-total="{total}"', after)
            self.assertIn(f'data-live-trend-source-average="{total}.0"', after)
            self.assertIn('1H 거래액', after)
            for group in ("mvk", "mvs"):
                bs, be = live._month_bounds(self.html, group, month)
                rs, re = live._month_bounds(result, group, month)
                self.assertEqual(self.html[bs:be], result[rs:re])
        self.assertIn('MoM ▲ 50.0%', result)

    def test_finalizer_consumes_supplied_quality(self):
        fixture = close_fixture.FinalizeMonthReviewTest()
        fixture.setUp()
        fixture.current["live_quality"] = live.summarize([self.row(8, 31, 230_000_000)], [self.row(7, 31, 100_000_000)])
        result = finalize_month_review(self.html, fixture.current, fixture.previous)
        self.assertIn('data-live-quality-source-total="230000000"', result)
        self.assertIn('2.30억', result)

    def test_youtube_historical_renderer_preserves_revenue(self):
        record = {"published": 25, "completed": 25, "overall": 53471, "LF": 139384,
                  "SF": 26885, "LF_count": 6, "SF_count": 19, "LF_completed": 6,
                  "SF_completed": 19, "subscriber": None, "subscriber_date": None}
        result = self.html
        for month, day in ((8, 31), (9, 9)):
            rows = [{"publish_date": dt.date(2026, month, 1), "video_id": f"fixture{i:04d}",
                     "form": "LF" if i < 6 else "SF", "d7_complete": True}
                    for i in range(25)]
            result = youtube.update_main_youtube_surfaces(
                result, year=2026, month=month, as_of=dt.date(2026, month, day),
                snapshot_date=None, rows=rows, quality_series={7: record, 8: record, 9: record},
            )
            youtube.assert_main_parity(
                result, month=month, expected_published=25,
                expected_latest_publish_date=dt.date(2026, month, 1),
                expected_snapshot_date=None, expected_elapsed_weeks=(day + 6) // 7,
                expected_total_weeks=5,
            )
        start, end = live._month_bounds(result, "mvr", 8)
        before_start, before_end = live._month_bounds(self.html, "mvr", 8)
        marker = '<div class="card quality-card live-quality">'
        self.assertEqual(result[start:end].split(marker)[0], self.html[before_start:before_end].split(marker)[0])
        self.assertIn('data-yt-main-source-average-views="53471"', result[start:end])
        self.assertIn('D+7 완료 25/25건', result[start:end])
        self.assertIn('data-quality-trend="youtube-8" data-quality-trend-kind="yt-format-average"', result[start:end])
        self.assertIn('8월 D+7 완료 25/25건', result[start:end])

    def test_generated_pending_forecast_can_close_from_canonical_actuals(self):
        from dashboard_forecast_state import update_forecast_surfaces
        fixture = close_fixture.FinalizeMonthReviewTest()
        fixture.setUp()
        current = dict(fixture.current, as_of="2026-09-30", range_label="9/1~9/30")
        previous = dict(fixture.previous, as_of="2026-08-31")
        seeded = live.update_default_month_state(self.html, 9, year=2026)
        seeded = live.update_current_raw_surfaces(seeded, current)
        seeded = update_forecast_surfaces(seeded, current, {"ad_gen": 600_000_000, "ad_int": 30_000_000, "live": None, "status": "pending_scope"})
        result = finalize_month_review(seeded, current, previous)
        start, end = live._month_bounds(result, "mvr", 9)
        self.assertNotIn('data-current-forecast-team=', result[start:end])
        self.assertNotIn('라이브 예상매출 집계 기준 확인 필요', result[start:end])
        self.assertIn('월말 확정 매출', result[start:end])
        self.assertIn('data-achievement-ring="달성"', result[start:end])
        self.assertIn('9월 마감확정치', result)
        _, manifest = guard.extract_manifest(result)
        now = dt.datetime.fromisoformat(manifest["built_at_kst"])
        self.assertEqual(guard.verify(result, now), [])
        for marker in ('data-live-progress-count=', 'data-live-package-count=', '시그니처 하위'):
            with self.subTest(missing=marker):
                damaged = result[:start] + result[start:end].replace(marker, 'removed-marker=', 1) + result[end:]
                self.assertTrue(any('live ' in error and ('count markers' in error or 'sub-promotion' in error)
                                    for error in guard.verify(damaged, now)))

    def test_january_compares_previous_december_without_rewriting_future_december(self):
        calls = []
        def fetch(db, year, month, **kwargs):
            calls.append((year, month, kwargs["end_date"]))
            return [], None
        with patch.object(live, "fetch_live_rows", side_effect=fetch):
            result = history.update_live_quality_history(self.html, Path("fixture"), 2026, 1, dt.date(2026, 1, 2))
        self.assertEqual(calls[0], (2025, 12, dt.date(2025, 12, 31)))
        start, end = live._month_bounds(self.html, "mvr", 12)
        rs, re = live._month_bounds(result, "mvr", 12)
        self.assertEqual(self.html[start:end], result[rs:re])

    def test_excluded_completed_rows_lose_stale_metrics_but_keep_schedule(self):
        def rendered(brand, day):
            return ('<div class="activity-row"><time class="activity-date" '
                    f'datetime="2026-09-{day:02d}">9/{day}</time>'
                    f'<div class="activity-main"><b class="content-title">{brand}</b>'
                    '<small class="activity-inline-meta">예약 메타데이터 보존</small></div>'
                    '<div class="activity-metric metric-trio num">'
                    '<span class="metric-cell"><b>999</b></span>'
                    '<span class="metric-cell"><b>9.99억</b></span>'
                    '<span class="metric-cell"><b>888</b></span></div></div>')
        excluded = rendered("제외 전환 fixture", 3)
        known = rendered("품질 회귀 fixture", 4)
        future = rendered("미래 예약 fixture", 25)
        start, end = live._month_bounds(self.html, "mvr", 9)
        block = self.html[start:end]
        live_start = block.index('<div class="card quality-card live-quality">')
        insertion = block.index('<div class="week-items">', live_start) + len('<div class="week-items">')
        seeded = self.html[:start] + block[:insertion] + excluded + known + future + block[insertion:] + self.html[end:]
        rows = [self.row(9, 4, 100_000_000)]
        # Isolate the history clear callsite from the legacy row updater: the
        # late-fact seal must not rely on its historical markup matcher.
        with patch.object(live, "fetch_live_rows", side_effect=lambda db, y, m, **kw: (rows if m == 9 else [], None)), patch.object(live, "update_live_activity_rows", side_effect=lambda text, *a, **kw: text):
            result = history.update_live_quality_history(seeded, Path("fixture"), 2026, 9, dt.date(2026, 9, 9))
        self.assertFalse(excluded in result, "excluded completed row kept old metrics")
        self.assertIn(known, result)
        self.assertIn(future, result)
        cleared = excluded.replace('<b>999</b>', '<b>—</b>').replace('<b>9.99억</b>', '<b>—</b>').replace('<b>888</b>', '<b>—</b>')
        self.assertIn(cleared, result)
        self.assertIn('<i style="background:#1fb7a6"></i>스마트 평균', result)
        self.assertIn('<i style="background:#9b7af4"></i>에센셜 평균', result)


if __name__ == "__main__":
    unittest.main()
