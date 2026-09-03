import datetime as dt
import html as html_lib
import re
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))

from finalize_month_review_from_duckdb import (
    fetch_canonical_actual_snapshot,
    finalize_month_review,
)
from verify_dashboard import extract_manifest, verify

ROOT = Path(__file__).resolve().parents[1]


class FinalizeMonthReviewTest(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "index.html").read_text()
        self.current = {
            "as_of": "2026-08-31",
            "range_label": "8/1~8/31",
            "ad_gen_won": 868_200_000,
            "ad_int_won": 29_090_909,
            "live_won": 215_000_000,
            "target_won": 1_277_682_548,
            "team_targets_won": {
                "ad_gen": 865_682_548,
                "ad_int": 200_000_000,
                "live": 212_000_000,
            },
            "total_won": 1_112_290_909,
            "progress_pct": 87.0553352594,
            "live_package_revenue": {
                "시그니처": 101_000_000,
                "에센셜": 60_000_000,
                "스마트": 54_000_000,
            },
            "live_package_count": {"시그니처": 9, "에센셜": 15, "스마트": 8},
            "live_pgm_revenue": {
                "시그니처": {
                    "PGM · 12주년": 41_000_000,
                    "PGM · 어서오!세일": 24_000_000,
                    "일반": 24_000_000,
                    "PGM · 오늘it집": 12_000_000,
                },
                "에센셜": {
                    "PGM · 12주년": 48_000_000,
                    "PGM · 어서오!세일": 12_000_000,
                },
                "스마트": {
                    "PGM · 12주년": 47_000_000,
                    "PGM · 어서오!세일": 7_000_000,
                },
            },
        }
        self.previous = {
            "as_of": "2026-07-31",
            "ad_gen_won": 804_600_000,
            "ad_int_won": 54_000_000,
            "live_won": 185_000_000,
            "total_won": 1_043_600_000,
            "live_package_revenue": {
                "시그니처": 72_000_000,
                "에센셜": 64_000_000,
                "스마트": 49_000_000,
            },
        }

    def test_closes_august_with_actuals_across_review_surfaces(self):
        result = finalize_month_review(self.html, self.current, self.previous)

        self.assertIn('<option value="8" selected>2026년 8월 · 확정</option>', result)
        self.assertIn('<option value="9">2026년 9월 · 진행 중</option>', result)
        self.assertIn('class="mvk mv" data-m="8" data-phase="closed"', result)
        self.assertIn('<div class="k">8월 확정 총액<span class="phase">확정</span></div>', result)
        self.assertIn('<div class="v num">11.1억</div>', result)
        self.assertIn('전월 대비 <span class="pill up num">▲ 6.6%</span>', result)
        self.assertIn('확정 RAW · 8/1~8/31', result)
        self.assertIn('ACTUAL 2026-08', result)
        self.assertNotIn('FORECAST 2026-08', result)
        self.assertNotIn('8월 마감예상액', result)
        self.assertNotIn('8월 · 진행 중', result)

    def test_team_cards_and_monthly_bar_use_the_same_actuals(self):
        result = finalize_month_review(self.html, self.current, self.previous)
        month_start = result.index('<div class="mvr mv" data-m="8"')
        month_end = result.index('<div class="mvr mv" data-m="9"', month_start)
        team_block = result[month_start:month_end]
        decoded_team_block = html_lib.unescape(team_block)

        self.assertRegex(team_block, r'<span class="nm">일반광고</span><div class="bigv num">8\.68억</div>')
        self.assertRegex(team_block, r'<span class="nm">통광마</span><div class="bigv num">2,909만</div>')
        self.assertRegex(team_block, r'<span class="nm">라이브</span><div class="bigv num">2\.15억</div>')
        self.assertIn('aria-label="달성률 100.3%"', team_block)
        self.assertIn('aria-label="달성률 14.5%"', team_block)
        self.assertIn('aria-label="달성률 101.4%"', team_block)
        self.assertIn('<span>1.01억</span><small class="up">MoM ▲ 40.3%</small>', decoded_team_block)
        self.assertIn('<span>6,000만</span><small class="dn">MoM ▼ 6.2%</small>', decoded_team_block)
        self.assertIn('확정 매출 32건 · 편성 36건', decoded_team_block)
        self.assertNotIn('구분 미기재', decoded_team_block)

        chart = re.search(
            r'<div class="g closed" data-m="8".*?</div></div></div>',
            result,
            re.S,
        )
        self.assertIsNotNone(chart)
        self.assertIn('<div class="lab num">11.1</div>', chart.group(0))
        self.assertIn('<div class="glab num neg">−1.7</div>', chart.group(0))
        self.assertIn('8월 · 확정', chart.group(0))
        self.assertIn('확정 합계', chart.group(0))

    def test_rejects_a_partial_month_as_final_review(self):
        partial = dict(self.current, as_of="2026-08-30", range_label="8/1~8/30")
        with self.assertRaisesRegex(ValueError, "month-end snapshot required"):
            finalize_month_review(self.html, partial, self.previous)

    def test_canonical_actual_snapshot_uses_integrated_ssot_not_direct_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "actual.duckdb"
            con = duckdb.connect(str(db))
            con.execute("create schema revenue")
            con.execute("""
                create table revenue.integrated_ssot(
                    revenue_month varchar,
                    include_in_mbd_revenue boolean,
                    revenue_team varchar,
                    team_attributed_revenue double,
                    package_or_slot_type varchar,
                    source_date varchar,
                    brand_name varchar
                )
            """)
            con.executemany(
                "insert into revenue.integrated_ssot values (?, true, ?, ?, ?, ?, ?)",
                [
                    ("2026-08", "일반광고", 868_200_000, "스토어홈배너", "2026-08-31", "일반광고"),
                    ("2026-08", "통합광고", 29_090_909, "라이브", "2026-08-31", "통광마"),
                    ("2026-08", "라이브커머스", 101_000_000, "시그니처", "2026-08-03", "A"),
                    ("2026-08", "라이브커머스", 60_000_000, "에센셜", "2026-08-04", "B"),
                    ("2026-08", "라이브커머스", 54_000_000, "스마트", "2026-08-05", "C"),
                ],
            )
            con.execute("create schema meta")
            con.execute("create table meta.targets(team varchar, metric varchar, ym varchar, kind varchar, value_num double)")
            con.executemany(
                "insert into meta.targets values (?, '매출', '2026-08', 'target', ?)",
                [("ad_gen", 865_682_548), ("ad_int", 200_000_000), ("live", 212_000_000)],
            )
            con.close()

            actual = fetch_canonical_actual_snapshot(db, dt.date(2026, 8, 31))

        self.assertEqual(actual["ad_gen_won"], 868_200_000)
        self.assertEqual(actual["ad_int_won"], 29_090_909)
        self.assertEqual(actual["live_won"], 215_000_000)
        self.assertEqual(actual["total_won"], 1_112_290_909)
        self.assertEqual(actual["live_package_revenue"]["시그니처"], 101_000_000)

    def test_finalized_august_passes_the_release_guard(self):
        _, base_manifest = extract_manifest(self.html)
        built = max(
            dt.datetime.fromisoformat(value)
            for value in base_manifest["source_snapshot_as_of"].values()
        ) + dt.timedelta(hours=1)
        result = finalize_month_review(self.html, self.current, self.previous, built_at=built)
        _, manifest = extract_manifest(result)
        now = dt.datetime.fromisoformat(manifest["built_at_kst"]) + dt.timedelta(hours=1)
        self.assertEqual(verify(result, now, require_fresh=True), [])

    def test_release_guard_rejects_forecast_badge_on_closed_august(self):
        _, base_manifest = extract_manifest(self.html)
        built = max(
            dt.datetime.fromisoformat(value)
            for value in base_manifest["source_snapshot_as_of"].values()
        ) + dt.timedelta(hours=1)
        result = finalize_month_review(self.html, self.current, self.previous, built_at=built)
        mutated = result.replace("ACTUAL 2026-08", "FORECAST 2026-08", 1)
        _, manifest = extract_manifest(mutated)
        now = dt.datetime.fromisoformat(manifest["built_at_kst"]) + dt.timedelta(hours=1)
        errors = verify(mutated, now, require_fresh=True)
        self.assertTrue(any("closed August badge" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
