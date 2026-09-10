import datetime as dt
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dashboard_forecast_state as forecast
import refresh_live_daily_from_duckdb as daily
import verify_dashboard as guard


class ForecastStateTest(unittest.TestCase):
    def setUp(self):
        self.html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.raw = dict(as_of="2026-09-09", range_label="9/1~9/9", ad_gen_won=222800000,
                        ad_int_won=20000000, live_won=0, total_won=242800000,
                        target_won=1277682548, team_targets_won=dict(ad_gen=865682548, ad_int=200000000, live=212000000),
                        progress_pct=19.003)
        self.pred = dict(ad_gen=726600000, ad_int=73333333, live=None, status="pending_scope")

    def test_current_four_roles_preserve_raw_and_closed_revenue_idempotently(self):
        before = guard._month_surface(self.html, "mvk", 8)
        updated = forecast.update_forecast_surfaces(self.html, self.raw, self.pred)
        top = guard._month_surface(updated, "mvk", 9)
        self.assertEqual(top.count('class="kpi"'), 4)
        for label in ("9월 마감예상액", "현재 RAW 누적", "월 목표", "마감예상 GAP"):
            self.assertIn(label, top)
        self.assertEqual(top.count('<div class="v num">확인 필요</div>'), 2)
        teams = guard._month_surface(updated, "mvr", 9)
        self.assertEqual(teams.count("RAW 누적 · 9/1~9/9"), 3)
        self.assertIn("7.27억", teams)
        self.assertIn("7,333만", teams)
        self.assertNotIn("1.74억", teams)
        self.assertNotIn('class="seg"', guard._gauge_surface(updated, 9))
        self.assertEqual(before, guard._month_surface(updated, "mvk", 8))
        self.assertEqual(updated, forecast.update_forecast_surfaces(updated, self.raw, self.pred))
        comparison = guard._month_surface(updated, "mvs", 9)
        self.assertIn("8월 확정 RAW", comparison)
        self.assertNotIn("8월 마감예상", comparison)

    def test_unknown_live_never_becomes_zero_or_unapproved_policy(self):
        with mock.patch.object(daily, "fetch_current_revenue_snapshot", return_value=self.raw) as fetch:
            result = forecast.fetch_forecast(Path("unused"), dt.date(2026, 9, 9))
        self.assertIsNone(result["live"])
        self.assertEqual(fetch.call_args.args[1], dt.date(2026, 9, 30))
        with self.assertRaisesRegex(ValueError, "unapproved"):
            forecast.update_forecast_surfaces(self.html, self.raw, dict(self.pred, live=0))

    def test_october_rollover_does_not_reuse_september_forecast(self):
        raw = dict(self.raw, as_of="2026-10-01", range_label="10/1~10/1")
        text = daily.update_default_month_state(self.html, 10, year=2026)
        text = daily.update_current_raw_surfaces(text, raw)
        text = forecast.update_forecast_surfaces(text, raw, self.pred)
        top = guard._month_surface(text, "mvk", 10)
        self.assertIn("10월 마감예상액", top)
        self.assertNotIn("목표 채움 · 부킹 진행", top)
        self.assertIn('data-phase="pending_close"', guard._month_surface(text, "mvk", 9))
        _, manifest = guard.extract_manifest(text)
        now = dt.datetime.fromisoformat(manifest["built_at_kst"])
        self.assertEqual(guard.verify(text, now), [])
        for marker in ('data-live-progress-count=', 'data-live-package-count=', '시그니처 하위'):
            with self.subTest(missing=marker):
                bad = text.replace(marker, 'removed-marker=', 1)
                self.assertTrue(any('live ' in error and ('count markers' in error or 'sub-promotion' in error)
                                    for error in guard.verify(bad, now)))

    def test_guard_rejects_fake_numeric_unknown_and_missing_stack_state(self):
        updated = forecast.update_forecast_surfaces(self.html, self.raw, self.pred)
        bad = updated.replace('<div class="v num">확인 필요</div>', '<div class="v num">0</div>', 1)
        self.assertTrue(any("unavailable forecast" in x for x in guard.verify(bad, dt.datetime.now(daily.KST))))

    def test_snapshot_clock_uses_aware_source_date_not_build_date(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.duckdb"
            con = duckdb.connect(str(path))
            con.execute("create schema snapshot")
            con.execute("create table snapshot.meta(captured_at timestamptz, source_mtime timestamptz)")
            con.execute("insert into snapshot.meta values ('2026-09-09T10:20:00+09:00', '2026-09-09T01:30:00+09:00')")
            con.close()
            result = daily.fetch_snapshot_clock(path, dt.datetime(2026, 9, 10, 1, tzinfo=daily.KST))
            self.assertEqual(result["as_of"], dt.date(2026, 9, 9))
            self.assertEqual(result["source_as_of"], "2026-09-09T01:30:00+09:00")
            with self.assertRaisesRegex(RuntimeError, "future"):
                daily.fetch_snapshot_clock(path, dt.datetime(2026, 9, 8, tzinfo=daily.KST))

    def test_stage_hash_isolated_and_snapshot_age_not_reset(self):
        built = "2026-09-10T01:00:00+09:00"
        updated = daily.update_manifest(self.html, built, {"test": 1},
                                        touched_sources={"live_quality", "revenue_mirror"},
                                        source_as_of="2026-09-09T01:30:00+09:00",
                                        captured_at="2026-09-09T10:20:00+09:00",
                                        update_payload_hash=False)
        _, before = guard.extract_manifest(self.html)
        _, after = guard.extract_manifest(updated)
        self.assertEqual(after["source_payload_sha256"], before["source_payload_sha256"])
        self.assertEqual(after["source_snapshot_as_of"]["yt_quality"], before["source_snapshot_as_of"]["yt_quality"])
        self.assertEqual(after["source_snapshot_as_of"]["live_quality"], "2026-09-09T01:30:00+09:00")
        self.assertRegex(after["stage_payload_sha256"]["live_daily"], r"^[0-9a-f]{64}$")
        self.assertEqual(after["built_at_kst"], built)

    def test_live_contract_detects_cross_surface_mismatch_and_hides_private_notes(self):
        import verify_live_window_contract as contract_guard
        import refresh_live_window_from_duckdb as window
        root = Path(__file__).resolve().parents[1]
        contract = json.loads((root / "data/live_window_contract.json").read_text())
        self.assertEqual(contract_guard.check(self.html, contract), [])
        bad = re.sub(r'data-live-quality-source-count="[^"]+"', 'data-live-quality-source-count="99999"', self.html)
        self.assertTrue(any("MAIN_DETAIL_QUALITY_MISMATCH" in e for e in contract_guard.check(bad, contract)))
        row = dict(date=dt.date(2026,9,1), brand="Public fixture", pd="PRIVATE_PD_CANARY",
                   package_key="스마트", pgm="일반", viewers=100, clicks=10, buyers=5,
                   gmv_1d=100000, gmv_1h=10000, broadcast_gmv=90000, af=500, cost=100, margin=400)
        rendered, public = window.render_card(row, 1, {})
        for private in ("PRIVATE_PD_CANARY", "비용", "마진"):
            self.assertNotIn(private, rendered)
            self.assertNotIn(private, json.dumps(public))


if __name__ == "__main__":
    unittest.main()
