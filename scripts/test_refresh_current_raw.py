import contextlib
import datetime as dt
import hashlib
import io
import json
import re
import shutil
import subprocess
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
import fetch_target_youtube_snapshot as target_snapshot  # noqa: E402


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
        yt_source = "2026-08-24T08:30:00+09:00"
        owned_source = "2026-08-24T08:45:00+09:00"
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
        updated = owned_refresh.update_manifest_sources(
            html,
            built,
            payload={"main_surface": {"publish_count": 24}},
            source_as_of={"yt_quality": yt_source, "owned_media": owned_source},
            default_month=8,
        )
        raw = updated.split('id="mbd-public-guard">', 1)[1].split("</script>", 1)[0]
        result = json.loads(raw)
        self.assertEqual(result["built_at_kst"], built)
        self.assertEqual(result["default_month"], 8)
        self.assertEqual(result["source_snapshot_as_of"]["yt_quality"], yt_source)
        self.assertEqual(result["source_snapshot_as_of"]["owned_media"], owned_source)
        self.assertEqual(result["source_snapshot_as_of"]["revenue_mirror"], old)
        self.assertEqual(result["source_snapshot_as_of"]["live_quality"], old)
        self.assertEqual(result["source_snapshot_as_of"]["okr_targets"], old)

    def test_owned_refresh_binds_manifest_hash_to_youtube_payload(self):
        old = "2026-08-01T00:00:00+09:00"
        manifest = {
            "source_payload_sha256": "0" * 64,
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
        payload = {"main_surface": {"publish_count": 24, "average_views": 52646}}
        updated = owned_refresh.update_manifest_sources(
            html,
            "2026-08-31T13:00:00+09:00",
            payload=payload,
            source_as_of={"yt_quality": old, "owned_media": old},
        )
        raw = updated.split('id="mbd-public-guard">', 1)[1].split("</script>", 1)[0]
        result = json.loads(raw)
        expected = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        self.assertEqual(result["source_payload_sha256"], expected)

    def test_youtube_main_ledger_renders_all_elapsed_calendar_weeks(self):
        rows = [
            {
                "publish_date": dt.date(2026, 8, 3),
                "video_id": "AAAAAAAAAAA",
                "form": "LF",
                "title": "첫 콘텐츠",
                "url": "https://www.youtube.com/watch?v=AAAAAAAAAAA",
                "views_total": 1000,
                "d7_views": 700,
                "pis": 70,
                "d7_complete": True,
            },
            {
                "publish_date": dt.date(2026, 8, 17),
                "video_id": "BBBBBBBBBBB",
                "form": "SF",
                "title": "지난주 콘텐츠",
                "url": "https://www.youtube.com/watch?v=BBBBBBBBBBB",
                "views_total": 2000,
                "d7_views": 1500,
                "pis": 80,
                "d7_complete": True,
            },
            {
                "publish_date": dt.date(2026, 8, 24),
                "video_id": "CCCCCCCCCCC",
                "form": "LF",
                "title": "이번주 콘텐츠",
                "url": "https://www.youtube.com/watch?v=CCCCCCCCCCC",
                "views_total": 3000,
                "d7_views": None,
                "pis": None,
                "d7_complete": False,
            },
        ]

        rendered = owned_refresh.render_main_ledger(
            year=2026,
            month=8,
            as_of=dt.date(2026, 8, 30),
            rows=rows,
            snapshot_date=dt.date(2026, 8, 30),
        )

        for index, span in ((1, "8/1–8/7"), (2, "8/8–8/14"),
                            (3, "8/15–8/21"), (4, "8/22–8/28"),
                            (5, "8/29–8/31")):
            self.assertIn(f'data-week-group="8-{index}"', rendered)
            self.assertIn(span, rendered)
        self.assertIn("지난주 콘텐츠", rendered)
        self.assertIn("이번주 콘텐츠", rendered)
        self.assertIn("D+7 수집중", rendered)
        self.assertIn('data-yt-main-source-publish-count="3"', rendered)
        self.assertIn('data-yt-main-source-latest-publish-date="2026-08-24"', rendered)
        self.assertIn('data-yt-main-source-snapshot-date="2026-08-30"', rendered)

    def test_youtube_empty_new_month_keeps_elapsed_week_and_source_snapshot(self):
        rendered = owned_refresh.render_main_ledger(
            year=2026,
            month=9,
            as_of=dt.date(2026, 9, 1),
            rows=[],
            snapshot_date=dt.date(2026, 8, 31),
        )

        self.assertIn('data-week-group="9-1"', rendered)
        self.assertIn("9/1–9/7", rendered)
        self.assertIn("이 주차 발행 없음", rendered)
        self.assertIn('data-yt-main-source-publish-count="0"', rendered)
        self.assertIn('data-yt-main-source-latest-publish-date="none"', rendered)
        self.assertIn('data-yt-main-source-snapshot-date="2026-08-31"', rendered)
        self.assertIn('data-yt-main-source-elapsed-weeks="1"', rendered)

    def test_default_month_state_updates_selector_labels_and_js_cursor(self):
        html = (
            '<select id="msel">'
            '<option value="8" selected>2026년 8월 · 진행 중</option>'
            '<option value="9">2026년 9월 · 부킹 진행</option>'
            '<option value="10">2026년 10월 · 부킹 진행</option>'
            '</select><script>var CUR = 8;</script>'
            '<div class="mvk mv" data-m="8" data-phase="current"></div>'
            '<div class="mvr mv" data-m="9" data-phase="future"></div>'
            '<div class="mvs mv" data-m="10" data-phase="future"></div>'
        )

        updated = owned_refresh.update_default_month_state(html, 9)

        self.assertIn('<option value="8">2026년 8월 · 확정</option>', updated)
        self.assertIn('<option value="9" selected>2026년 9월 · 진행 중</option>', updated)
        self.assertIn('<option value="10">2026년 10월 · 부킹 진행</option>', updated)
        self.assertIn('var CUR = 9;', updated)
        self.assertIn('class="mvk mv" data-m="8" data-phase="closed"', updated)
        self.assertIn('class="mvr mv" data-m="9" data-phase="current"', updated)
        self.assertIn('class="mvs mv" data-m="10" data-phase="future"', updated)

    def test_default_month_state_updates_real_cur_phase_aliases(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
        updated = owned_refresh.update_default_month_state(html, 9)

        self.assertNotIn('data-phase="cur"', updated)
        for class_name in ("mvk", "mvs", "mvr"):
            self.assertRegex(updated, rf'class="{class_name} mv" data-m="8" data-phase="closed"')
            self.assertRegex(updated, rf'class="{class_name} mv" data-m="9" data-phase="current"')

    def test_fetch_main_content_uses_global_snapshot_when_current_month_is_empty(self):
        path = Path(self.tmp.name) / "youtube_empty.duckdb"
        con = duckdb.connect(str(path))
        con.execute('''
            create table dim_video(
                video_id varchar, url varchar, title varchar, publish_date date,
                form varchar, is_active boolean
            )
        ''')
        con.execute('''
            create table fact_analytics_d7(
                video_id varchar, fetched_at timestamp, d7_complete boolean,
                metric_end_date date, view_count bigint, like_count bigint,
                comment_count bigint, share_count bigint
            )
        ''')
        con.execute('''
            create table latest_snapshot_fixture(
                video_id varchar, cumulative_view_count bigint, snapshot_date date
            )
        ''')
        con.execute("create view v_latest_snapshot as select * from latest_snapshot_fixture")
        con.execute("create table fact_snapshot(snapshot_date date)")
        con.execute("insert into fact_snapshot values ('2026-08-31')")

        rows, snapshot_date = owned_refresh.fetch_main_content(
            con, dt.date(2026, 9, 1), dt.date(2026, 9, 1)
        )
        con.close()

        self.assertEqual(rows, [])
        self.assertEqual(snapshot_date, dt.date(2026, 8, 31))

    def test_fetch_month_synthesizes_immediate_next_month_while_waiting_for_analytics(self):
        path = Path(self.tmp.name) / "youtube_month_rollover.duckdb"
        con = duckdb.connect(str(path))
        con.execute('''
            create table v_youtube_monthly_analytics(
                period_start date, period_end date,
                metric_start_date date, metric_end_date date,
                period_complete boolean,
                channel_view_count bigint, channel_like_count bigint,
                channel_comment_count bigint, channel_share_count bigint,
                channel_engagement_count bigint,
                new_published_view_count bigint, prior_published_view_count bigint,
                unknown_publish_view_count bigint,
                fetched_at timestamp, raw_status varchar
            )
        ''')
        con.execute('''
            insert into v_youtube_monthly_analytics values (
                '2026-08-01', '2026-08-31', '2026-08-01', '2026-08-31', true,
                100, 10, 2, 1, 13, 60, 40, 0,
                '2026-09-01 00:45:00', 'ok'
            )
        ''')

        month = owned_refresh.fetch_month(con, 2026, 9)
        con.close()

        self.assertEqual(month["period_start"], dt.date(2026, 9, 1))
        self.assertEqual(month["period_end"], dt.date(2026, 9, 30))
        self.assertEqual(month["metric_start_date"], dt.date(2026, 9, 1))
        self.assertEqual(month["metric_end_date"], dt.date(2026, 9, 1))
        self.assertFalse(month["period_complete"])
        self.assertEqual(month["views"], 0)
        self.assertEqual(month["engagement"], 0)
        self.assertEqual(month["raw_status"], "awaiting_current_month_analytics")
        self.assertEqual(month["fetched_at"], dt.datetime(2026, 9, 1, 0, 45))

    def test_fetch_month_does_not_hide_a_multi_month_gap(self):
        path = Path(self.tmp.name) / "youtube_month_gap.duckdb"
        con = duckdb.connect(str(path))
        con.execute('''
            create table v_youtube_monthly_analytics(
                period_start date, period_end date,
                metric_start_date date, metric_end_date date,
                period_complete boolean,
                channel_view_count bigint, channel_like_count bigint,
                channel_comment_count bigint, channel_share_count bigint,
                channel_engagement_count bigint,
                new_published_view_count bigint, prior_published_view_count bigint,
                unknown_publish_view_count bigint,
                fetched_at timestamp, raw_status varchar
            )
        ''')
        con.execute('''
            insert into v_youtube_monthly_analytics values (
                '2026-08-01', '2026-08-31', '2026-08-01', '2026-08-31', true,
                100, 10, 2, 1, 13, 60, 40, 0,
                '2026-09-01 00:45:00', 'ok'
            )
        ''')

        with self.assertRaisesRegex(RuntimeError, "monthly YouTube analytics row not found"):
            owned_refresh.fetch_month(con, 2026, 10)
        con.close()

    def test_quality_series_carries_latest_subscriber_into_empty_new_month(self):
        path = Path(self.tmp.name) / "youtube_subscriber_rollover.duckdb"
        con = duckdb.connect(str(path))
        con.execute('''
            create table dim_video(
                video_id varchar, publish_date date, form varchar, is_active boolean
            )
        ''')
        con.execute('''
            create table fact_analytics_d7(
                video_id varchar, fetched_at timestamp, d7_complete boolean,
                metric_end_date date, view_count bigint
            )
        ''')
        con.execute('''
            create table v_channel_daily_subscribers(
                snapshot_date date, subscriber_count bigint, captured_at timestamp
            )
        ''')
        con.execute("insert into v_channel_daily_subscribers values ('2026-08-31', 803000, '2026-08-31 23:55:00')")

        series = owned_refresh.fetch_quality_series(
            con, 2026, 9, as_of=dt.date(2026, 9, 1)
        )
        con.close()

        self.assertEqual(series[9]["subscriber"], 803000)
        self.assertEqual(series[9]["subscriber_date"], dt.date(2026, 8, 31))

    def test_quality_series_uses_prior_december_for_january_mom(self):
        path = Path(self.tmp.name) / "youtube_year_boundary.duckdb"
        con = duckdb.connect(str(path))
        con.execute('''
            create table dim_video(
                video_id varchar, publish_date date, form varchar, is_active boolean
            )
        ''')
        con.executemany(
            "insert into dim_video values (?, ?, ?, true)",
            [("DECEMBER01", "2026-12-20", "LF"), ("JANUARY0001", "2027-01-02", "SF")],
        )
        con.execute('''
            create table fact_analytics_d7(
                video_id varchar, fetched_at timestamp, d7_complete boolean,
                metric_end_date date, view_count bigint
            )
        ''')
        con.executemany(
            "insert into fact_analytics_d7 values (?, ?, true, ?, ?)",
            [
                ("DECEMBER01", "2026-12-28 00:00:00", "2026-12-26", 100),
                ("JANUARY0001", "2027-01-10 00:00:00", "2027-01-08", 200),
            ],
        )
        con.execute('''
            create table v_channel_daily_subscribers(
                snapshot_date date, subscriber_count bigint, captured_at timestamp
            )
        ''')
        con.executemany(
            "insert into v_channel_daily_subscribers values (?, ?, ?)",
            [
                ("2026-12-31", 800000, "2026-12-31 23:00:00"),
                ("2027-01-10", 801000, "2027-01-10 23:00:00"),
            ],
        )

        series = owned_refresh.fetch_quality_series(con, 2027, 1)
        con.close()

        self.assertEqual(series[0]["overall"], 100)
        self.assertEqual(series[0]["LF"], 100)
        self.assertEqual(series[0]["subscriber"], 800000)
        self.assertEqual(series[1]["overall"], 200)
        self.assertEqual(series[1]["SF"], 200)
        rendered = owned_refresh.render_main_quality(
            '<span>1월 목표 100 대비</span>', 1, series
        )
        self.assertIn("MoM ▲ 100.0%", rendered)

    def test_quality_series_excludes_future_scheduled_publications(self):
        path = Path(self.tmp.name) / "youtube_future_schedule.duckdb"
        con = duckdb.connect(str(path))
        con.execute('''
            create table dim_video(
                video_id varchar, publish_date date, form varchar, is_active boolean
            )
        ''')
        con.executemany(
            "insert into dim_video values (?, ?, ?, true)",
            [("PUBLISHED01", "2026-08-10", "LF"), ("SCHEDULED01", "2026-08-20", "SF")],
        )
        con.execute('''
            create table fact_analytics_d7(
                video_id varchar, fetched_at timestamp, d7_complete boolean,
                metric_end_date date, view_count bigint
            )
        ''')
        con.executemany(
            "insert into fact_analytics_d7 values (?, ?, true, ?, ?)",
            [
                ("PUBLISHED01", "2026-08-15 00:00:00", "2026-08-15", 100),
                ("SCHEDULED01", "2026-08-27 00:00:00", "2026-08-26", 200),
            ],
        )
        con.execute('''
            create table v_channel_daily_subscribers(
                snapshot_date date, subscriber_count bigint, captured_at timestamp
            )
        ''')
        con.execute("insert into v_channel_daily_subscribers values ('2026-08-15', 800000, '2026-08-15 23:00:00')")

        series = owned_refresh.fetch_quality_series(
            con, 2026, 8, as_of=dt.date(2026, 8, 15)
        )
        con.close()

        self.assertEqual(series[8]["published"], 1)
        self.assertEqual(series[8]["LF_count"], 1)
        self.assertEqual(series[8]["SF_count"], 0)
        self.assertEqual(series[8]["completed"], 1)

    def test_youtube_main_surfaces_update_current_month_and_pass_parity(self):
        html = owned_refresh.DEFAULT_HTML.read_text(encoding="utf-8")
        rows = [
            {
                "publish_date": dt.date(2026, 8, 17),
                "video_id": "BBBBBBBBBBB",
                "form": "SF",
                "title": "지난주 콘텐츠",
                "url": "https://www.youtube.com/watch?v=BBBBBBBBBBB",
                "views_total": 2000,
                "d7_views": 1500,
                "pis": 80,
                "d7_complete": True,
            },
            {
                "publish_date": dt.date(2026, 8, 24),
                "video_id": "CCCCCCCCCCC",
                "form": "LF",
                "title": "이번주 콘텐츠",
                "url": "https://www.youtube.com/watch?v=CCCCCCCCCCC",
                "views_total": 3000,
                "d7_views": None,
                "pis": None,
                "d7_complete": False,
            },
        ]
        series = {
            7: {
                "published": 4, "LF_count": 2, "SF_count": 2,
                "completed": 4, "overall": 1000, "LF": 1500, "SF": 500,
                "subscriber": 800000, "subscriber_date": dt.date(2026, 7, 31),
            },
            8: {
                "published": 2, "LF_count": 1, "SF_count": 1,
                "completed": 1, "overall": 1500, "LF": 0, "SF": 1500,
                "subscriber": 803000, "subscriber_date": dt.date(2026, 8, 30),
            },
        }

        updated = owned_refresh.update_main_youtube_surfaces(
            html,
            year=2026,
            month=8,
            as_of=dt.date(2026, 8, 30),
            snapshot_date=dt.date(2026, 8, 30),
            rows=rows,
            quality_series=series,
        )
        start8, end8 = owned_refresh.month_block_bounds(updated, 8)
        august = updated[start8:end8]
        start9, end9 = owned_refresh.month_block_bounds(updated, 9)
        september = updated[start9:end9]

        self.assertIn("지난주 콘텐츠", august)
        self.assertIn("이번주 콘텐츠", august)
        self.assertIn('data-yt-main-source-publish-count="2"', august)
        self.assertIn('data-yt-main-source-latest-publish-date="2026-08-24"', august)
        self.assertIn('data-yt-main-quality-basis="analytics-d7"', august)
        self.assertIn("D+7 완료 1/2건", august)
        self.assertIn("8월 발행</div><div class=\"qn num\">2건", august)
        self.assertIn("SF 1건 · LF 1건", august)
        self.assertIn("Analytics 지속시간 미적재", august)
        self.assertNotIn("08-09 기준 · 조회수 1,126,735", august)
        self.assertNotIn("지난주 콘텐츠", september)
        owned_refresh.assert_main_parity(
            updated,
            month=8,
            expected_published=2,
            expected_latest_publish_date=dt.date(2026, 8, 24),
            expected_snapshot_date=dt.date(2026, 8, 30),
            expected_elapsed_weeks=5,
        )
        bad = updated.replace('data-yt-main-source-publish-count="2"',
                              'data-yt-main-source-publish-count="1"', 1)
        with self.assertRaisesRegex(RuntimeError, "publish count"):
            owned_refresh.assert_main_parity(
                bad,
                month=8,
                expected_published=2,
                expected_latest_publish_date=dt.date(2026, 8, 24),
                expected_snapshot_date=dt.date(2026, 8, 30),
                expected_elapsed_weeks=5,
            )

    def test_youtube_contract_records_actual_duckdb_path(self):
        month = {
            "period_start": dt.date(2026, 8, 1),
            "period_end": dt.date(2026, 8, 31),
            "metric_start_date": dt.date(2026, 8, 1),
            "metric_end_date": dt.date(2026, 8, 30),
            "period_complete": False,
            "views": 100,
            "new_views": 60,
            "prior_views": 40,
            "engagement": 10,
            "likes": 7,
            "comments": 2,
            "shares": 1,
            "unknown_views": 0,
            "fetched_at": dt.datetime(2026, 8, 31, 10, 0),
            "raw_status": "ok",
        }
        source = Path("/tmp/target-youtube.duckdb")
        _, contract = owned_refresh.render_section(
            month, [], {"total": 1, "LF": 1, "SF": 0}, [],
            dt.datetime(2026, 8, 31, 12, 0), db_path=source,
        )
        self.assertEqual(contract["source"]["duckdb"], str(source))

    def test_youtube_detail_marks_empty_dplusn_content_as_a_valid_state(self):
        month = {
            "period_start": dt.date(2026, 9, 1),
            "period_end": dt.date(2026, 9, 30),
            "metric_start_date": dt.date(2026, 9, 1),
            "metric_end_date": dt.date(2026, 9, 1),
            "period_complete": False,
            "views": 0,
            "new_views": 0,
            "prior_views": 0,
            "engagement": 0,
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "unknown_views": 0,
            "fetched_at": dt.datetime(2026, 9, 1, 0, 45),
            "raw_status": "awaiting_current_month_analytics",
        }

        section, _ = owned_refresh.render_section(
            month, [], {"total": 0}, [], dt.datetime(2026, 9, 1, 10, 0)
        )

        self.assertIn('data-yt-content-empty="true"', section)
        self.assertIn("D+N 완료 콘텐츠 없음", section)
        self.assertIn('data-yt-weekly-empty="true"', section)
        self.assertIn("주간 Analytics 집계 대기 중", section)

    def test_youtube_renderer_default_never_points_to_retired_local_db(self):
        self.assertEqual(
            owned_refresh.DEFAULT_YT_DB,
            Path("/tmp/mbd_h2_youtube_target_snapshot.duckdb"),
        )

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


class TargetYoutubeSnapshotTest(unittest.TestCase):
    def _fixture(self, path: Path, snapshot_date: dt.date) -> None:
        con = duckdb.connect(str(path))
        fresh_at = dt.datetime(2026, 8, 31, 23, 58)
        con.execute("create table fact_snapshot(snapshot_date date, captured_at timestamp, video_id varchar)")
        con.execute("insert into fact_snapshot values (?, ?, 'video-1')", [snapshot_date, fresh_at])
        con.execute("create table dim_video(video_id varchar, publish_date date, is_active boolean, form varchar, title varchar, url varchar)")
        con.execute("insert into dim_video values ('video-1', '2026-08-29', true, 'LF', 'title', 'https://youtu.be/video-1')")
        con.execute("create table fact_channel_snapshot(snapshot_date date, captured_at timestamp, subscriber_count bigint)")
        con.execute("insert into fact_channel_snapshot values (?, ?, 803000)", [snapshot_date, fresh_at])
        con.execute("create table v_channel_daily_subscribers(snapshot_date date, captured_at timestamp, subscriber_count bigint, raw_status varchar)")
        con.execute("insert into v_channel_daily_subscribers values (?, ?, 803000, 'ok')", [snapshot_date, fresh_at])
        con.execute("create table v_latest_snapshot(video_id varchar, cumulative_view_count bigint, snapshot_date date)")
        con.execute("insert into v_latest_snapshot values ('video-1', 100, ?)", [snapshot_date])
        con.execute("""create table fact_analytics_d7(
            video_id varchar, d7_complete boolean, fetched_at timestamp,
            metric_end_date date, view_count bigint, like_count bigint,
            comment_count bigint, share_count bigint, watch_seconds bigint,
            avg_view_duration_seconds double
        )""")
        con.execute("insert into fact_analytics_d7 values ('video-1', true, ?, ?, 100, 1, 1, 1, 1000, 10)", [fresh_at, snapshot_date])
        con.execute("""create table v_public_dplusn_video(
            d_plus_n integer, video_id varchar, publish_date date, form varchar,
            ip varchar, title varchar, url varchar, views bigint, complete boolean
        )""")
        con.execute("insert into v_public_dplusn_video values (7, 'video-1', '2026-08-29', 'LF', 'ALL', 'title', 'https://youtu.be/video-1', 100, true)")
        for name in ("v_youtube_monthly_analytics", "v_youtube_weekly_analytics"):
            con.execute("""create table """ + name + """(
                period_start date, period_end date, metric_start_date date,
                metric_end_date date, period_complete boolean,
                channel_view_count bigint, channel_like_count bigint,
                channel_comment_count bigint, channel_share_count bigint,
                channel_engagement_count bigint, new_published_view_count bigint,
                prior_published_view_count bigint, unknown_publish_view_count bigint,
                fetched_at timestamp, raw_status varchar
            )""")
            con.execute("insert into " + name + " values ('2026-08-01', '2026-08-31', '2026-08-01', ?, true, 100, 1, 1, 1, 3, 60, 40, 0, ?, 'ok')", [snapshot_date, fresh_at])
        con.close()

    def test_target_snapshot_validator_accepts_previous_day_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "target.duckdb"
            self._fixture(path, dt.date(2026, 8, 31))
            result = target_snapshot.validate_snapshot(path, as_of=dt.date(2026, 9, 1))
            self.assertEqual(result["snapshot_date"], "2026-08-31")
            self.assertEqual(result["latest_publish_date"], "2026-08-29")

    def test_target_snapshot_validator_rejects_stale_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "target.duckdb"
            self._fixture(path, dt.date(2026, 8, 29))
            with self.assertRaisesRegex(RuntimeError, "stale target YouTube snapshot"):
                target_snapshot.validate_snapshot(path, as_of=dt.date(2026, 9, 1))

    def test_target_snapshot_validator_rejects_missing_required_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "target.duckdb"
            self._fixture(path, dt.date(2026, 8, 31))
            con = duckdb.connect(str(path))
            con.execute("alter table fact_analytics_d7 drop column d7_complete")
            con.close()
            with self.assertRaisesRegex(RuntimeError, "missing columns"):
                target_snapshot.validate_snapshot(path, as_of=dt.date(2026, 9, 1))

    def test_target_snapshot_validator_rejects_empty_required_relation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "target.duckdb"
            self._fixture(path, dt.date(2026, 8, 31))
            con = duckdb.connect(str(path))
            con.execute("delete from v_youtube_monthly_analytics")
            con.close()
            with self.assertRaisesRegex(RuntimeError, "empty relation"):
                target_snapshot.validate_snapshot(path, as_of=dt.date(2026, 9, 1))

    def test_target_snapshot_validator_rejects_stale_analytics_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "target.duckdb"
            self._fixture(path, dt.date(2026, 8, 31))
            con = duckdb.connect(str(path))
            con.execute("update v_youtube_monthly_analytics set fetched_at='2026-08-29 23:58:00'")
            con.close()
            with self.assertRaisesRegex(RuntimeError, "stale target YouTube source"):
                target_snapshot.validate_snapshot(path, as_of=dt.date(2026, 9, 1))

    def test_remote_snapshot_copy_rejects_unsafe_paths_before_sql(self):
        proc = subprocess.run(
            [sys.executable, "-", "/tmp/unsafe'path.duckdb", "/tmp/out.duckdb"],
            input=target_snapshot.REMOTE_COPY_SCRIPT,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unsafe DuckDB path", proc.stderr)

    def test_copy_failure_is_not_masked_by_cleanup_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "key"
            key.touch()
            with mock.patch.object(
                target_snapshot.subprocess,
                "run",
                side_effect=[
                    SimpleNamespace(returncode=1),
                    subprocess.TimeoutExpired("ssh", 30),
                ],
            ):
                warning = io.StringIO()
                with contextlib.redirect_stderr(warning):
                    with self.assertRaisesRegex(RuntimeError, "snapshot copy failed"):
                        target_snapshot.sync_snapshot(root / "out.duckdb", key=key)
                self.assertIn("temp cleanup failed", warning.getvalue())

    def test_cleanup_nonzero_returncode_fails_successful_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "key"
            key.touch()
            source = root / "source.duckdb"
            self._fixture(source, dt.date(2026, 8, 31))

            def fake_run(args, **kwargs):
                if args[0] == "scp":
                    shutil.copy2(source, args[-1])
                    return SimpleNamespace(returncode=0)
                if "/bin/rm" in args:
                    return SimpleNamespace(returncode=1)
                return SimpleNamespace(returncode=0)

            with mock.patch.object(target_snapshot.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "temp cleanup failed rc=1"):
                    target_snapshot.sync_snapshot(root / "out.duckdb", key=key)

    def test_transfer_failure_keeps_last_good_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "key"
            key.touch()
            output = root / "out.duckdb"
            output.write_bytes(b"last-good")
            with mock.patch.object(
                target_snapshot.subprocess,
                "run",
                side_effect=[
                    SimpleNamespace(returncode=0),
                    SimpleNamespace(returncode=1),
                    SimpleNamespace(returncode=0),
                ],
            ):
                with self.assertRaisesRegex(RuntimeError, "snapshot transfer failed"):
                    target_snapshot.sync_snapshot(output, key=key)
            self.assertEqual(output.read_bytes(), b"last-good")

    def test_stale_transfer_keeps_last_good_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "key"
            key.touch()
            output = root / "out.duckdb"
            output.write_bytes(b"last-good")
            stale = root / "stale.duckdb"
            self._fixture(stale, dt.date(2020, 1, 1))

            def fake_run(args, **kwargs):
                if args[0] == "scp":
                    shutil.copy2(stale, args[-1])
                return SimpleNamespace(returncode=0)

            with mock.patch.object(target_snapshot.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "stale target YouTube snapshot"):
                    target_snapshot.sync_snapshot(output, key=key)
            self.assertEqual(output.read_bytes(), b"last-good")


if __name__ == "__main__":
    unittest.main()
