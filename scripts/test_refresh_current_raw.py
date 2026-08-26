import datetime as dt
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import refresh_live_daily_from_duckdb as refresh  # noqa: E402
import refresh_owned_youtube_window_from_duckdb as owned_refresh  # noqa: E402


class CurrentRawRefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "fixture.duckdb"
        con = duckdb.connect(str(self.db_path))
        con.execute("create schema ad_gen")
        con.execute("create schema ad_int")
        con.execute("create schema live")
        con.execute("create schema meta")
        con.execute('''
            create table ad_gen.booking_pred(
                date varchar, status varchar, ad_type varchar, party_type varchar, revenue varchar
            )
        ''')
        con.executemany(
            "insert into ad_gen.booking_pred values (?, ?, ?, ?, ?)",
            [
                ("2026-08-09", "CONFIRMED", "일반광고", "3P", "159,900,000"),
                ("2026-08-24", "CONFIRMED", "일반광고", "제3자", "511,000,000"),
                ("2026-08-24", "CONFIRMED", "일반광고", "판촉결합", "10,000,000"),
                ("2026-08-25", "CONFIRMED", "일반광고", "3P", "77,700,000"),
                ("2026-08-20", "CANCEL", "일반광고", "3P", "99,000,000"),
                ("2026-08-20", "CONFIRMED", "일반광고", "1P", "60,000,000"),
                ("2026-08-20", "CONFIRMED", "일반광고", "미분류", "50,000,000"),
                ("2026-08-20", "CONFIRMED", "통합광고", "3P", "88,000,000"),
            ],
        )
        con.execute('''
            create table ad_int.contract(
                "계약 시작일" varchar, "매출 귀속월" varchar, "미셀 매출액" varchar
            )
        ''')
        con.executemany(
            'insert into ad_int.contract values (?, ?, ?)',
            [
                ("2026. 8. 1", "202608", "9,090,909"),
                ("2026. 8. 10", "202608", "20,000,000"),
                ("2026. 8. 25", "202608", "5,000,000"),
            ],
        )
        con.execute('''
            create table live.raw_slots(
                "온에어 일자" varchar, "브랜드명" varchar, "1P/3P" varchar,
                "패키지" varchar, "PGM" varchar, "비고 (프로모션)" varchar,
                "AF수취액" varchar, "라이브 시청자 (비로그인 포함)" varchar,
                "일 전체 GMV (라이브 브랜드 전체)" varchar, "라이브 1H GMV" varchar
            )
        ''')
        con.executemany(
            'insert into live.raw_slots values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            [
                ("2026-08-09", "A", "3P", "시그니처", "일반", "", "56,850,000", "100", "120,000,000", "60,000,000"),
                ("2026-08-24", "B", "3P", "스마트", "일반", "", "111,400,000", "200", "240,000,000", "90,000,000"),
                ("2026-08-25", "C", "3P", "에센셜", "일반", "", "48,900,000", "300", "360,000,000", "120,000,000"),
                ("2026-08-20", "D", "1P", "스마트", "일반", "", "1,100,000", "10", "0", "0"),
                ("2026-08-20", "E", "3P", "에센셜", "일반", "보상성 무상 지원", "9,900,000", "10", "0", "0"),
                ("2026-08-20", "F", "3P", "에센셜", "취소", "취소 편성", "8,800,000", "10", "0", "0"),
            ],
        )
        con.execute('''
            create table meta.targets(
                team varchar, metric varchar, ym varchar, kind varchar, value_num double
            )
        ''')
        con.executemany(
            'insert into meta.targets values (?, ?, ?, ?, ?)',
            [
                ("ad_gen", "매출", "2026-08", "target", 900_000_000),
                ("ad_int", "매출", "2026-08", "target", 200_000_000),
                ("live", "매출", "2026-08", "target", 212_000_000),
                ("live", "거래액", "2026-08", "target", 2_500_000_000),
            ],
        )
        con.execute('''
            create table meta.ingest_log(last_ingest_at timestamp, status varchar)
        ''')
        con.execute("insert into meta.ingest_log values ('2026-08-24 09:00:00', 'ok')")
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_excludes_future_cancelled_and_1p_rows(self):
        snapshot = refresh.fetch_current_revenue_snapshot(
            self.db_path,
            dt.date(2026, 8, 24),
        )
        self.assertEqual(snapshot["ad_gen_won"], 680_900_000)
        self.assertEqual(snapshot["ad_int_won"], 29_090_909)
        self.assertEqual(snapshot["live_won"], 168_250_000)
        self.assertEqual(snapshot["total_won"], 878_240_909)
        self.assertEqual(snapshot["target_won"], 1_312_000_000)
        self.assertEqual(snapshot["team_targets_won"]["ad_gen"], 900_000_000)
        self.assertAlmostEqual(
            snapshot["progress_pct"],
            snapshot["total_won"] / snapshot["target_won"] * 100,
            places=6,
        )
        self.assertEqual(snapshot["range_label"], "8/1~8/24")

    def test_live_quality_rows_exclude_future_and_nonrevenue_rows(self):
        rows, _ = refresh.fetch_live_rows(
            self.db_path,
            2026,
            8,
            end_date=dt.date(2026, 8, 24),
        )
        self.assertTrue(rows)
        self.assertLessEqual(max(row["date"] for row in rows), dt.date(2026, 8, 24))
        brands = {row["brand"] for row in rows}
        self.assertNotIn("C", brands)
        self.assertNotIn("D", brands)
        self.assertNotIn("E", brands)
        self.assertNotIn("F", brands)

    def test_live_activity_keeps_schedule_but_scrubs_excluded_metrics(self):
        def activity_row(day, brand):
            return (
                '<div class="activity-row">'
                f'<time class="activity-date" datetime="{day}">{day[-5:]}</time>'
                '<div class="activity-main activity-main-inline"><span class="activity-title-line">'
                f'<a class="content-link" data-content-link="live" href="#">{brand}'
                '<span aria-hidden="true">↗</span></a></span></div>'
                '<div class="activity-metric metric-trio num">'
                '<span class="metric-cell"><b>999</b></span>'
                '<span class="metric-cell"><b>9.99억</b></span>'
                '<span class="metric-cell"><b>9.99억</b></span></div></div>'
            )

        html = ''.join(
            activity_row(day, brand)
            for day, brand in (
                ("2026-08-09", "A"),
                ("2026-08-25", "C"),
                ("2026-08-20", "D"),
                ("2026-08-20", "E"),
                ("2026-08-20", "F"),
            )
        )
        rows, _ = refresh.fetch_live_rows(
            self.db_path,
            2026,
            8,
            end_date=dt.date(2026, 8, 24),
        )

        rendered = refresh.update_live_activity_rows(html, rows, year=2026, month=8)

        self.assertIn('<b>100</b>', rendered)
        rendered_rows = {
            match.group("brand"): match.group("metrics")
            for match in re.finditer(
                r'<div class="activity-row">.*?data-content-link="live"[^>]*>(?P<brand>[^<]+)'
                r'<span aria-hidden="true">↗</span>.*?'
                r'<div class="activity-metric metric-trio num">(?P<metrics>.*?)</div></div>',
                rendered,
                flags=re.S,
            )
        }
        for brand in ("C", "D", "E", "F"):
            self.assertEqual(rendered_rows[brand].count('<b>—</b>'), 3, brand)

    def test_manifest_advances_only_touched_source_timestamps(self):
        old = "2026-08-09T09:00:00+09:00"
        manifest = {
            "built_at_kst": old,
            "source_snapshot_as_of": {
                "revenue_mirror": old,
                "live_quality": old,
                "yt_quality": old,
                "okr_targets": old,
                "owned_media": old,
            },
        }
        html = (
            '<script type="application/json" id="mbd-public-guard">'
            + json.dumps(manifest)
            + "</script>"
        )
        built = "2026-08-24T10:00:00+09:00"
        updated = refresh.update_manifest(
            html,
            built,
            {"fixture": True},
            touched_sources={"revenue_mirror", "live_quality", "okr_targets"},
        )
        raw = updated.split('id="mbd-public-guard">', 1)[1].split("</script>", 1)[0]
        result = json.loads(raw)
        self.assertEqual(result["built_at_kst"], built)
        self.assertEqual(result["source_snapshot_as_of"]["revenue_mirror"], built)
        self.assertEqual(result["source_snapshot_as_of"]["live_quality"], built)
        self.assertEqual(result["source_snapshot_as_of"]["yt_quality"], old)
        self.assertEqual(result["source_snapshot_as_of"]["okr_targets"], built)
        self.assertEqual(result["source_snapshot_as_of"]["owned_media"], old)

    def test_owned_refresh_advances_only_owned_and_youtube_timestamps(self):
        old = "2026-08-09T09:00:00+09:00"
        built = "2026-08-24T10:00:00+09:00"
        manifest = {
            "built_at_kst": old,
            "source_snapshot_as_of": {
                "revenue_mirror": old,
                "live_quality": old,
                "yt_quality": old,
                "okr_targets": old,
                "owned_media": old,
            },
        }
        html = (
            '<script type="application/json" id="mbd-public-guard">'
            + json.dumps(manifest)
            + "</script>"
        )
        updated = owned_refresh.update_manifest_sources(html, built)
        raw = updated.split('id="mbd-public-guard">', 1)[1].split("</script>", 1)[0]
        result = json.loads(raw)
        self.assertEqual(result["built_at_kst"], built)
        self.assertEqual(result["source_snapshot_as_of"]["yt_quality"], built)
        self.assertEqual(result["source_snapshot_as_of"]["owned_media"], built)
        self.assertEqual(result["source_snapshot_as_of"]["revenue_mirror"], old)
        self.assertEqual(result["source_snapshot_as_of"]["live_quality"], old)
        self.assertEqual(result["source_snapshot_as_of"]["okr_targets"], old)

    def test_current_month_raw_surfaces_are_reconciled(self):
        html_path = Path(__file__).resolve().parents[1] / "index.html"
        html = html_path.read_text(encoding="utf-8")
        snapshot = refresh.fetch_current_revenue_snapshot(
            self.db_path,
            dt.date(2026, 8, 24),
            target_won=1_278_000_000,
        )
        updated = refresh.update_current_raw_surfaces(html, snapshot)
        month8 = updated.split('class="mvk mv" data-m="8"', 1)[1].split(
            'class="mvk mv" data-m="9"', 1
        )[0]
        self.assertIn("현재 RAW 누적 · 8/1~8/24", month8)
        self.assertIn("8.78억", month8)
        self.assertIn("목표 진척 68.7%", month8)
        self.assertIn("일반광고&lt;/span&gt;&lt;b&gt;6.81억", month8)
        self.assertIn("통광마&lt;/span&gt;&lt;b&gt;2,909만", month8)
        self.assertIn("라이브&lt;/span&gt;&lt;b&gt;1.68억", month8)
        self.assertNotIn("현재 RAW 누적 · 8/1~8/9", month8)

    def test_december_surface_update_does_not_require_month_13(self):
        html = (
            '<div><span class="chip">RAW 12/1~12/1</span>'
            '<span class="chip vi">FORECAST 2026-11</span></div>'
            '<div class="mvk mv" data-m="12"><div class="kpi" data-tip="RAW 누적 old">'
            '<div class="ic">icon</div><div><div class="k">현재 RAW 누적 · old</div>'
            '<div class="v num">0</div><div class="s num"><span class="pill flat num">목표 진척 0%</span>'
            '</div></div></div></div>'
            '<div class="mvr mv" data-m="12">'
            '<span class="nm">일반광고</span><div class="rows num"><div class="r"><span>RAW 누적 · old</span>'
            '<b>0 <span class="mutpct">진척 0%</span></b></div></div>'
            '<span class="nm">통광마</span><div class="rows num"><div class="r"><span>RAW 누적 · old</span>'
            '<b>0 <span class="mutpct">진척 0%</span></b></div></div>'
            '<span class="nm">라이브</span><div class="rows num"><div class="r"><span>RAW 누적 · old</span>'
            '<b>0 <span class="mutpct">진척 0%</span></b></div></div></div>'
        )
        snapshot = {
            "as_of": "2026-12-24",
            "range_label": "12/1~12/24",
            "ad_gen_won": 100_000_000,
            "ad_int_won": 20_000_000,
            "live_won": 30_000_000,
            "total_won": 150_000_000,
            "progress_pct": 50.0,
            "team_targets_won": {"ad_gen": 200_000_000, "ad_int": 40_000_000, "live": 60_000_000},
        }
        updated = refresh.update_current_raw_surfaces(html, snapshot)
        self.assertIn("현재 RAW 누적 · 12/1~12/24", updated)
        self.assertIn("FORECAST 2026-12", updated)

    def test_december_live_quality_updates_december_not_august(self):
        html = refresh.DEFAULT_HTML.read_text(encoding="utf-8")
        start8, end8 = refresh._month_bounds(html, "mvr", 8)
        august_before = html[start8:end8]
        stats = {"avg": 123_000_000, "sum": 123_000_000, "n": 1, "mom": 0.2}
        summary = {key: dict(stats) for key in refresh.TEAM_ORDER}

        updated = refresh.update_live_quality(html, summary, month=12)

        new_start8, new_end8 = refresh._month_bounds(updated, "mvr", 8)
        self.assertEqual(updated[new_start8:new_end8], august_before)
        start12, end12 = refresh._month_bounds(updated, "mvr", 12)
        december = updated[start12:end12]
        self.assertIn('12월 목표 1.00억 대비 123.0%', december)
        self.assertIn('data-live-quality-mom-main="12"', december)
        self.assertIn('data-live-quality-mom="12-overall"', december)
        self.assertIn('<div class="qn num">1.23억</div>', december)

    def test_default_refresh_refuses_dirty_operator_worktree(self):
        result = SimpleNamespace(returncode=0, stdout=" M index.html\n", stderr="")
        with mock.patch.object(refresh.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "dirty worktree"):
                refresh.assert_safe_default_refresh(refresh.DEFAULT_HTML, allow_dirty=False)

        with mock.patch.object(refresh.subprocess, "run", return_value=result):
            refresh.assert_safe_default_refresh(refresh.DEFAULT_HTML, allow_dirty=True)


if __name__ == "__main__":
    unittest.main()
