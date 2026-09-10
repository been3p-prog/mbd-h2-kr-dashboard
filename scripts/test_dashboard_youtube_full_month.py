import datetime as dt
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import refresh_owned_youtube_window_from_duckdb as youtube


class YoutubeFullMonthLedgerTest(unittest.TestCase):
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
