import datetime as dt
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import refresh_owned_youtube_window_from_duckdb as youtube


class YoutubeFullMonthLedgerTest(unittest.TestCase):
    def test_period_kpi_discloses_live_in_total(self):
        period = dict.fromkeys(("views", "new_views", "prior_views", "engagement", "likes", "comments", "shares", "unknown_views"), 0)
        period.update(metric_start_date=dt.date(2026, 9, 1), metric_end_date=dt.date(2026, 9, 9))
        kpi = youtube.kpis_for_period(period, {"LF": 2, "SF": 21, "LIVE": 1, "total": 24})[1]
        self.assertEqual(kpi["value"], "24건")
        self.assertEqual(kpi["em"], "LF 2 · SF 21 · LIVE 1")

    def test_publish_cohorts_include_live_and_unknown_without_inflating_lf_sf(self):
        for extra in ("LIVE", "확인중"):
            with self.subTest(extra=extra):
                youtube.assert_publish_cohorts(
                    {"LF": 2, "SF": 21, extra: 1, "total": 24},
                    [{"form": "LF"}] * 2 + [{"form": "SF"}] * 21 + [{"form": extra}],
                    {"published": 24, "LF_count": 2, "SF_count": 21},
                )

    def test_publish_cohorts_reject_wrong_format_or_quality_counts(self):
        rows = [{"form": "LF"}, {"form": "SF"}, {"form": "LIVE"}]
        counts = {"LF": 1, "SF": 1, "LIVE": 1, "total": 3}
        quality = {"published": 3, "LF_count": 1, "SF_count": 1}
        for bad in (dict(counts, LIVE=2), dict(counts, total=2),
                    {"LF": 2, "SF": 1, "total": 3}):
            with self.subTest(counts=bad), self.assertRaises(RuntimeError):
                youtube.assert_publish_cohorts(bad, rows, quality)
        for bad in (dict(quality, published=2), dict(quality, LF_count=2),
                    dict(quality, SF_count=2)):
            with self.subTest(quality=bad), self.assertRaises(RuntimeError):
                youtube.assert_publish_cohorts(counts, rows, bad)

    def render(self, year=2026, month=9, day=10, rows=None):
        return youtube.render_main_ledger(
            year=year, month=month, as_of=dt.date(year, month, day),
            rows=rows or [], snapshot_date=dt.date(year, month, day),
        )

    def test_partial_month_shows_all_weeks_open_without_invented_publications(self):
        rendered = self.render()
        groups = re.findall(r'<details class="week-group" data-week-group="9-(\d+)"([^>]*)>', rendered)
        self.assertEqual([int(index) for index, _ in groups], [1, 2, 3, 4, 5])
        self.assertTrue(all(attrs.strip() == 'open' for _, attrs in groups))
        self.assertIn('data-yt-main-source-elapsed-weeks="2"', rendered)
        self.assertIn('data-yt-main-source-total-weeks="5"', rendered)
        self.assertEqual(rendered.count('이 주차 발행 없음'), 5)
        self.assertIn('9/29–9/30', rendered)
        self.assertNotIn('data-content-link="youtube"', rendered)

    def test_month_length_and_elapsed_clock_remain_separate(self):
        for year, month, day, total, elapsed, last_range in [
            (2026, 2, 1, 4, 1, '2/22–2/28'),
            (2024, 2, 1, 5, 1, '2/29–2/29'),
            (2026, 12, 31, 5, 5, '12/29–12/31'),
        ]:
            with self.subTest(year=year, month=month):
                rendered = self.render(year, month, day)
                self.assertEqual(rendered.count('<details class="week-group"'), total)
                self.assertEqual(rendered.count(' open>'), total)
                self.assertIn(f'data-yt-main-source-elapsed-weeks="{elapsed}"', rendered)
                self.assertIn(f'data-yt-main-source-total-weeks="{total}"', rendered)
                self.assertIn(last_range, rendered)

    def test_all_published_rows_and_unknown_metrics_are_preserved(self):
        rows = [
            dict(publish_date=dt.date(2026, 9, 2), video_id='AAAAAAAAAAA', title='완료 콘텐츠',
                 form='LF', views_total=0, d7_views=0, pis=0, d7_complete=True),
            dict(publish_date=dt.date(2026, 9, 10), video_id='BBBBBBBBBBB', title='최신 콘텐츠',
                 form='SF', views_total=None, d7_views=None, pis=None, d7_complete=False),
        ]
        rendered = self.render(rows=rows)
        self.assertEqual(rendered.count('data-content-link="youtube"'), 2)
        self.assertEqual(rendered.count('<b>—</b>'), 3)
        self.assertEqual(rendered.count('<b>0</b>'), 3)
        self.assertIn('data-yt-main-source-publish-count="2"', rendered)
        self.assertIn('data-yt-main-source-latest-publish-date="2026-09-10"', rendered)
        self.assertIn('D+7 완료 1건', rendered)
        self.assertIn('최신 콘텐츠', rendered)
        self.assertEqual(rendered.count('이 주차 발행 없음'), 3)
        self.assertEqual(rendered, self.render(rows=rows))

    def test_full_month_parity_rejects_missing_duplicate_or_collapsed_week(self):
        rendered = self.render()
        def check(ledger):
            text = ('<div class="mvr mv" data-m="9"><div class="card quality-card yt-quality">'
                    '<div data-yt-main-quality-basis="analytics-d7"></div>' + ledger +
                    '</div></div><div class="mvr mv" data-m="10"></div>')
            youtube.assert_main_parity(
                text, month=9, expected_published=0, expected_latest_publish_date=None,
                expected_snapshot_date=dt.date(2026, 9, 10), expected_elapsed_weeks=2,
                expected_total_weeks=5,
            )
        check(rendered)
        for bad in [
            rendered.replace('data-week-group="9-5"', 'data-week-group="9-4"'),
            rendered.replace('data-week-group="9-5" open', 'data-week-group="9-5"'),
            rendered.replace('data-yt-main-source-total-weeks="5"', 'data-yt-main-source-total-weeks="4"'),
            rendered + '<details class="week-group" data-week-group="9-5" open></details>',
        ]:
            with self.subTest(bad=bad[-100:]), self.assertRaisesRegex(RuntimeError, 'weeks'):
                check(bad)


if __name__ == '__main__':
    unittest.main()
