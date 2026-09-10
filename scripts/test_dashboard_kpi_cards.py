import datetime as dt
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dashboard_kpi_cards as kpi
import refresh_live_daily_from_duckdb as daily
import verify_dashboard as guard
import smoke_dashboard as smoke
import dashboard_forecast_state as forecast
import test_refresh_current_raw as raw_fixture
import test_verify_dashboard as smoke_fixture
import test_dashboard_forecast_state as forecast_fixture
import test_finalize_month_review as close_fixture
from finalize_month_review_from_duckdb import finalize_month_review


class TwoCardContractTest(unittest.TestCase):
    def setUp(self):
        self.html = (Path(__file__).resolve().parents[1] / 'index.html').read_text()
        fixture = forecast_fixture.ForecastStateTest()
        fixture.setUp()
        self.raw = dict(fixture.raw, previous_same_period={'as_of': '2026-08-09', 'total_won': 200000000})

    def test_cutoffs_handle_short_months_leap_year_and_year_boundary(self):
        for current, expected in [('2026-09-10', '2026-08-10'), ('2026-03-31', '2026-02-28'),
                                  ('2024-03-31', '2024-02-29'), ('2026-01-10', '2025-12-10')]:
            self.assertEqual(kpi.previous_cutoff(dt.date.fromisoformat(current)).isoformat(), expected)

    def test_same_period_uses_identical_three_team_filters_not_whole_month(self):
        fixture = raw_fixture.CurrentRawRefreshTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        previous = daily.fetch_same_period_comparison(fixture.db_path, dt.date(2026, 9, 10))
        self.assertEqual(previous, {'as_of': '2026-08-10', 'total_won': 245840909})
        whole = daily.fetch_current_revenue_snapshot(fixture.db_path, dt.date(2026, 8, 31))
        self.assertLess(previous['total_won'], whole['total_won'])

    def test_current_mom_and_repeated_refresh_keep_two_cards(self):
        text = forecast.update_forecast_surfaces(self.html, self.raw,
                dict(ad_gen=726600000, ad_int=73333333, live=None, status='pending_scope'))
        text = daily.update_current_raw_surfaces(text, self.raw)
        self.assertEqual(text, daily.update_current_raw_surfaces(text, self.raw))
        top = guard._month_surface(text, 'mvk', 9)
        self.assertEqual(top.count('class="kpi"'), 2)
        self.assertIn('MoM ▲ 21.4%', top)
        self.assertIn('전월 동일기간 8/1~8/9', top)
        self.assertNotIn('예상 달성률 0', top)
        self.assertEqual(guard.verify(text, dt.datetime.now(daily.KST)), [])
        for bad in [text.replace('data-previous-as-of="2026-08-09"', 'data-previous-as-of="2026-08-31"'),
                    text.replace('MoM ▲ 21.4%', 'MoM ▲ 99.9%', 1)]:
            self.assertTrue(any('same-period' in error for error in guard.verify(bad, dt.datetime.now(daily.KST))))

    def test_missing_or_zero_prior_never_becomes_zero_percent(self):
        for previous in [None, {'as_of': '2026-08-09', 'total_won': 0}]:
            rendered = kpi.current_cards(dict(self.raw, previous_same_period=previous))
            self.assertIn('MoM —', rendered)
            self.assertNotIn('MoM 0.0%', rendered)

    def test_closed_and_future_layouts_remain_two_and_idempotent(self):
        self.assertEqual(self.html, kpi.normalize_legacy_tops(self.html))
        fixture = close_fixture.FinalizeMonthReviewTest()
        fixture.setUp()
        closed = finalize_month_review(self.html, fixture.current, fixture.previous)
        top = guard._month_surface(closed, 'mvk', 8)
        self.assertEqual(top.count('class="kpi"'), 2)
        self.assertIn('8월 마감확정치', top)
        self.assertIn('달성률 87.1%', top)
        self.assertIn('확정 GAP -1.65억', top)
        self.assertNotIn('data-current-forecast-status=', top)
        self.assertEqual(closed, finalize_month_review(closed, fixture.current, fixture.previous,
                         built_at=dt.datetime.fromisoformat(guard.extract_manifest(closed)[1]['built_at_kst'])))

    def test_two_card_smoke_rejects_missing_extra_or_wrong_roles(self):
        for width, height, tag in smoke.VIEWPORTS:
            for count in [1, 2, 3]:
                result = smoke_fixture.SmokeViewportPolicyTest()._result(width)
                result['layout'].update(currentKpiLayout=kpi.LAYOUT, currentKpiCount=count,
                    currentKpiPhase='current',
                    currentKpiRoles=['forecast', 'current_raw'], currentForecastStates=['pending_scope'],
                    currentKpiLabels=['9월 마감예측치', '9월 현황누적치'])
                errors = smoke._check_viewport(result, width, height, tag, switch_expected=None)
                self.assertEqual(not errors, count == 2)
            result['layout']['currentKpiCount'] = 2
            result['layout']['currentKpiRoles'] = ['forecast', 'target']
            self.assertTrue(smoke._check_viewport(result, width, height, tag, switch_expected=None))

    def test_daily_refresh_does_not_reopen_accepted_current_month_close(self):
        text = daily.update_default_month_state(self.html, 8, year=2026)
        top = guard._month_surface(text, 'mvk', 8)
        self.assertIn('data-phase="closed"', top)
        updated = forecast.update_current_revenue_state(text, dict(self.raw, as_of='2026-08-31'), {})
        self.assertEqual(text, updated)

    def test_closed_visible_values_must_match_metadata(self):
        top = guard._month_surface(self.html, 'mvk', 8)
        for original, replacement in [('<div class="v num">11.12억</div>', '<div class="v num">99억</div>'),
                                      (' · 달성률 87.1%', ' · 달성률 99.9%')]:
            bad_top = top.replace(original, replacement, 1)
            self.assertNotEqual(top, bad_top)
            bad = self.html.replace(top, bad_top, 1)
            self.assertIn('two-card closed visible values invalid: month 8',
                          guard.verify(bad, dt.datetime.now(daily.KST)))

    def test_canonical_close_after_rollover_seals_all_pending_surfaces(self):
        text = daily.update_default_month_state(self.html, 10, year=2026)
        self.assertIn('data-phase="pending_close"', guard._month_surface(text, 'mvk', 9))
        fixture = close_fixture.FinalizeMonthReviewTest()
        fixture.setUp()
        current = dict(fixture.current, as_of='2026-09-30', range_label='9/1~9/30')
        closed = finalize_month_review(text, current, fixture.current)
        for group in ('mvk', 'mvs', 'mvr'):
            self.assertIn('data-phase="closed"', guard._month_surface(closed, group, 9))
        self.assertFalse(any('two-card' in error for error in guard.verify(closed, dt.datetime.now(daily.KST))))

    def test_smoke_accepts_initial_closed_month_with_no_current_month(self):
        for width, height, tag in smoke.VIEWPORTS:
            result = smoke_fixture.SmokeViewportPolicyTest()._result(width)
            result['layout'].update(currentKpiLayout=kpi.LAYOUT, currentKpiCount=2,
                currentKpiMonth='12', currentKpiPhase='closed',
                currentKpiRoles=['closed_actual', 'target_gap'], currentForecastStates=[],
                currentKpiLabels=['12월 마감확정치', '월 목표'])
            result['lower'].update(futureRootCount=0, futureExpectedCount=0)
            self.assertEqual(smoke._check_viewport(result, width, height, tag, switch_expected=None), [])
            result['layout']['currentKpiPhase'] = 'current'
            self.assertTrue(smoke._check_viewport(result, width, height, tag, switch_expected=None))
            result['layout']['currentKpiPhase'] = 'closed'
            result['lower']['futureExpectedCount'] = 3
            self.assertTrue(smoke._check_viewport(result, width, height, tag, switch_expected=None))

    def test_smoke_probes_initial_month_before_switching_even_when_closed(self):
        script = smoke._probe_script(9)
        self.assertLess(script.index('var initialMonth=sel?sel.value:null'), script.index("sel.value='9'"))
        self.assertIn("'+initialMonth+'", script)
        self.assertNotIn("data-phase=\\\"current\\\"] > .kpis", script)

    def test_actual_layout_probe_reads_closed_initial_month_without_current_root(self):
        script = smoke._probe_script(11)
        fragment = script[script.index("var main="):script.index("out.errors=")]
        harness = r'''
const vm = require('node:vm');
const source = require('node:fs').readFileSync(0, 'utf8');
const cards = ['closed_actual','target_gap'].map((role,i) => ({
  dataset:{kpiRole:role}, querySelector(){return {firstChild:{textContent:i?'월 목표':'12월 마감확정치'}};}
}));
const kpis = {dataset:{kpiLayout:'two-card-v1'},parentElement:{dataset:{m:'12',phase:'closed'}},
 querySelectorAll(s){return s===':scope > .kpi'?cards:[];}};
const document = {querySelector(s){
 if(s==='main')return {contains(){return true;}};
 if(s==='.mvk[data-m="12"] > .kpis')return kpis;
 return null;
},querySelectorAll(){return [];}};
const sandbox={document,out:{},initialMonth:'12',sel:{value:'11'}};
vm.runInNewContext(source,sandbox);
process.stdout.write(JSON.stringify(sandbox.out.layout));
'''
        result = subprocess.run(['node', '-e', harness], input=fragment, text=True,
                                capture_output=True, check=True)
        layout = json.loads(result.stdout)
        self.assertEqual(layout['currentKpiMonth'], '12')
        self.assertEqual(layout['currentKpiPhase'], 'closed')
        self.assertEqual(layout['currentKpiCount'], 2)
        self.assertEqual(layout['currentKpiRoles'], ['closed_actual', 'target_gap'])


if __name__ == '__main__':
    unittest.main()
