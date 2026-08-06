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

    def test_decision_panel_includes_all_three_teams_with_forecast_as_primary(self):
        start = self.html.index("function renderGapDiagnosis")
        end = self.html.index("function renderScopedMonthChart", start)
        renderer = self.html[start:end]
        self.assertIn("3팀 매출 요약", renderer)
        self.assertIn("data-gap-team='${esc(row.team||'')}'", renderer)
        self.assertIn("마감예측", renderer)
        self.assertIn("미달팀 GAP의", renderer)
        self.assertIn("GAP", renderer)
        self.assertNotIn(".filter(seg=>seg.displayGap>0)", renderer)

    def test_lower_cards_use_selected_summary_segment_as_revenue_sot(self):
        self.assertIn("const summarySeg=scopedSummarySegments(p).find(seg=>seg.team===team);", self.html)
        self.assertIn("actual_label:baseItem.actual_label||summarySeg.value_eok_label||summarySeg.value_label", self.html)
        self.assertIn("pct_label:summarySeg.achievement_label", self.html)
        top = extract_json_assignment(self.html, "__TOP_SUMMARY_DATA__")
        sot = extract_json_assignment(self.html, "__MBD_SOT_DATA__")
        for month in top["month"]["periods"]:
            for segment in (s for s in month["segments"] if s["team"] in ALLOWED_REVENUE_TEAMS):
                lower = next(p for p in sot["lower_revenue_card_periods"][segment["team"]]["month"] if p["id"] == month["ym"])
                if lower["actual"] is None:
                    continue
                self.assertEqual(lower["actual"], segment["value"], f'{month["ym"]} {segment["team"]} actual')
                self.assertEqual(lower["target"], segment["target"], f'{month["ym"]} {segment["team"]} target')
                if lower["pct"] is not None and segment["achievement"] is not None:
                    self.assertAlmostEqual(lower["pct"], segment["achievement"], msg=f'{month["ym"]} {segment["team"]} achievement')

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

    # [2026-08-06] 질적 KPI 세로형 레이아웃 계약 — 유튜브와 라이브를 각각 전체 폭으로 읽게 한다.
    def test_quality_cards_use_vertical_full_width_editorial_layout(self):
        self.assertIn('class="kpi-row quality-row quality-editorial-stack"', self.html)
        self.assertEqual(self.html.count('class="kpi-card quality-card quality-wide-card'), 2)
        self.assertIn('body[data-ux-contract="figma-editorial-v2"] .quality-editorial-stack{display:grid;grid-template-columns:minmax(0,1fr)', self.html)

    def test_youtube_quality_has_format_summary_content_rows_and_editable_insight(self):
        for marker in (
            'data-youtube-format-summary',
            'data-youtube-content-list',
            'data-youtube-content-row',
            'data-quality-insight-editor="owned_youtube"',
            '키 인사이트 · 이 브라우저 저장',
        ):
            self.assertIn(marker, self.html)
        self.assertIn('renderYoutubeFormatSummary', self.html)
        self.assertIn('renderYoutubeContentRows', self.html)

    def test_live_quality_has_package_summary_and_broadcast_sheet_insights(self):
        for marker in (
            'data-live-package-summary',
            'data-live-broadcast-list',
            'data-live-broadcast-row',
            'data-live-sheet-insight',
            '시트 인사이트',
        ):
            self.assertIn(marker, self.html)
        self.assertIn('renderLivePackageSummary', self.html)
        self.assertIn('renderLiveBroadcastRows', self.html)

    def test_quality_detail_payload_reconciles_to_certified_period_counts(self):
        self.assertIn('window.__QUALITY_DETAIL_DATA__ =', self.html)
        detail = extract_json_assignment(self.html, "__QUALITY_DETAIL_DATA__")
        sot = extract_json_assignment(self.html, "__MBD_SOT_DATA__")
        for period_type in ("month", "week"):
            for item in sot["quality_card_periods"]["owned_youtube"][period_type]:
                rows = detail["owned_youtube"][period_type][item["id"]]["content_rows"]
                self.assertEqual(len(rows), item["posts"], f'youtube {period_type} {item["id"]}')
                self.assertTrue(all(row["type_label"] in {"LF", "SF"} for row in rows))
            for item in sot["quality_card_periods"]["live_gmv"][period_type]:
                rows = detail["live_gmv"][period_type][item["id"]]["broadcast_rows"]
                self.assertEqual(len(rows), item["broadcast_count"], f'live count {period_type} {item["id"]}')
                self.assertEqual(sum(row["gmv"] for row in rows), round(item["total_gmv"] or 0), f'live total {period_type} {item["id"]}')
                self.assertTrue(all(row["gmv"] > 0 for row in rows))
                self.assertTrue(all(row["insight_source"] in {"공식 회고", "내부 회고", "미작성"} for row in rows))


if __name__ == "__main__":
    unittest.main()
