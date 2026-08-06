import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_dashboard import ALLOWED_REVENUE_TEAMS, extract_json_assignment, verify


class DashboardGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path(__file__).resolve().parents[1].joinpath("index.html").read_text(encoding="utf-8")
        cls.now = dt.datetime.fromisoformat("2026-08-05T00:06:59+09:00")

    def test_stale_snapshot_can_ship_only_as_visible_ui_hotfix(self):
        errors = verify(self.html, self.now, require_fresh=False)
        self.assertFalse(errors)

    def test_scheduled_freshness_probe_rejects_stale_snapshot(self):
        errors = verify(self.html, self.now, require_fresh=True)
        self.assertTrue(any("stale snapshot" in error for error in errors))

    def test_missing_diagnosis_wiring_fails_closed(self):
        mutated = self.html.replace("renderGapDiagnosis(period,p);", "/* diagnosis call removed */", 1)
        errors = verify(mutated, self.now)
        self.assertTrue(any("diagnosis renderer wiring" in error for error in errors))

    def test_bypassing_raw_semantic_guard_fails_closed(self):
        mutated = self.html.replace("const raw=rawActualPresentation(period,p);", "const raw={label:'현재 RAW 누적',value:p.current_actual,small:'',className:''};", 1)
        errors = verify(mutated, self.now)
        self.assertTrue(any("raw semantic guard wiring" in error for error in errors))

    def test_disabling_current_month_auto_selection_fails_closed(self):
        mutated = self.html.replace("month:defaultMonthSelection()", "month:topSummary.month?.current_index||0", 1)
        errors = verify(mutated, self.now)
        self.assertTrue(any("current-month auto selection wiring" in error for error in errors))

    def test_scoped_month_chart_must_be_wired(self):
        self.assertIn("function renderScopedMonthChart(idx)", self.html)
        self.assertIn("renderScopedMonthChart(idx);", self.html)

    def test_scoped_week_chart_must_be_wired(self):
        self.assertIn("function renderScopedWeekChart(idx)", self.html)
        self.assertIn("renderScopedWeekChart(idx);", self.html)

    def test_scoped_week_chart_wiring_fails_closed(self):
        mutated = self.html.replace("if(period==='week')renderScopedWeekChart(idx);", "/* week chart call removed */", 1)
        errors = verify(mutated, self.now)
        self.assertTrue(any("scoped week-chart renderer wiring" in error for error in errors))

    def test_scoped_month_chart_wiring_fails_closed(self):
        mutated = self.html.replace("if(period==='month')renderScopedMonthChart(idx);", "/* chart call removed */", 1)
        errors = verify(mutated, self.now)
        self.assertTrue(any("scoped month-chart renderer wiring" in error for error in errors))

    def test_runtime_scope_filter_must_be_wired_to_rendering(self):
        mutated = self.html.replace(
            "return (p?.segments||[]).filter(seg=>REVENUE_SCOPE_SET.has(seg.team));",
            "return p?.segments||[];",
            1,
        )
        errors = verify(mutated, self.now)
        self.assertTrue(any("revenue-scope render filter" in error for error in errors))

    def test_runtime_scope_is_exactly_b22n_revenue_teams(self):
        self.assertEqual(ALLOWED_REVENUE_TEAMS, {"ad_gen", "ad_int", "live"})

    def test_runtime_scope_cannot_add_ogam(self):
        mutated = self.html.replace("'ad_gen','ad_int','live']", "'ad_gen','ad_int','live','ogam']", 1)
        errors = verify(mutated, self.now)
        self.assertTrue(any("runtime revenue scope mismatch" in error for error in errors))

    def test_august_live_target_is_source_target_not_doubled(self):
        top = extract_json_assignment(self.html, "__TOP_SUMMARY_DATA__")
        august = next(p for p in top["month"]["periods"] if p["ym"] == "2026-08")
        live = next(segment for segment in august["segments"] if segment["team"] == "live")
        self.assertEqual(live["target"], 212_000_000.0)
        self.assertEqual(august["target"], 1_277_682_548.0)
        scoped_targets = sum(segment["target"] for segment in august["segments"] if segment["team"] in ALLOWED_REVENUE_TEAMS)
        self.assertAlmostEqual(august["target"], scoped_targets)
        self.assertEqual(live["target_label"], "2.1억")

    def test_august_summary_cards_preserve_reference_comparisons(self):
        top = extract_json_assignment(self.html, "__TOP_SUMMARY_DATA__")
        august = next(p for p in top["month"]["periods"] if p["ym"] == "2026-08")
        self.assertEqual(august["forecast_prev_delta_label"], "-2.6%")
        self.assertEqual(august["forecast_trailing3_delta_label"], "-9.3%")
        self.assertEqual(august["actual_same_day_prev_delta_label"], "-22.7%")
        self.assertEqual(august["actual_same_day_trailing3_delta_label"], "-22.6%")
        self.assertIn("전월대비 -2.6% | 최근 3개월 평균대비 -9.3%", self.html)
        self.assertIn("전월 동일자 대비 -22.7% | 최근 3개월 동일자 평균대비 -22.6%", self.html)

    def test_mobile_summary_grid_is_single_column_without_implicit_overflow(self):
        self.assertIn(
            'body[data-ux-contract="figma-editorial-v2"] .summary-period-view{width:100%;min-width:0;grid-template-columns:minmax(0,1fr);grid-template-areas:"kpis" "chart" "main"}',
            self.html,
        )

    def test_freshness_warning_cannot_be_nested_in_month_view(self):
        warning = '<div class="summary-freshness" data-dashboard-freshness hidden></div>'
        mutated = self.html.replace(warning, "", 1).replace(
            '<div class="summary-period-view active" data-period-view="month">',
            '<div class="summary-period-view active" data-period-view="month">' + warning,
            1,
        )
        errors = verify(mutated, self.now)
        self.assertTrue(any("freshness warning must remain visible" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
