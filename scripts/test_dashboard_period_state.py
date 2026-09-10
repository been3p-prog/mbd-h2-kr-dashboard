import datetime as dt
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import refresh_live_daily_from_duckdb as live
import refresh_owned_youtube_window_from_duckdb as youtube

ROOT = Path(__file__).resolve().parents[1]


class PeriodStateTest(unittest.TestCase):
    def test_rollover_does_not_finalize_unclosed_month(self):
        source = (ROOT / 'index.html').read_text()
        for renderer in (live, youtube):
            updated = renderer.update_default_month_state(source, 10)
            self.assertIn('2026년 9월 · 마감 확인 필요', updated)
            self.assertIn('data-m="9" data-phase="pending_close"', updated)
            self.assertIn('data-m="8" data-phase="closed"', updated)
            self.assertEqual(renderer.update_default_month_state(updated, 10), updated)

    def test_next_year_fails_instead_of_reusing_old_data(self):
        source = (ROOT / 'index.html').read_text()
        for renderer in (live, youtube):
            with self.assertRaisesRegex(ValueError, 'year'):
                renderer.update_default_month_state(source, 1, year=2027)

    def test_current_live_rows_reconcile_metadata_and_hour_label(self):
        source = (ROOT / 'index.html').read_text()
        start, end = live._month_bounds(source, 'mvr', 9)
        fixture = source[start:end].replace('1H 거래액', '3H 거래액')
        fixture, count = re.subn(
            r'(<b class="content-title">매일유업</b><small class="activity-inline-meta">)[^<]*',
            r'\g<1>스마트 · 일반 · 오래된 일정', fixture,
        )
        self.assertEqual(count, 1)
        self.assertIn('datetime="2026-09-03">9/3</time>', fixture)
        self.assertIn('3H 거래액', fixture)
        source = source[:start] + fixture + source[end:]
        row = dict(date=dt.date(2026, 9, 3), brand='매일유업', package='에센셜',
                   package_key='essential', pgm='오세일', viewers=123, gmv_1d=456, gmv_1h=789)
        updated = live.update_live_activity_rows(source, [row], year=2026, month=9)
        start, end = live._month_bounds(updated, 'mvr', 9)
        block = updated[start:end]
        self.assertIn('1H 거래액', block)
        self.assertNotIn('3H 거래액', block)
        self.assertIn('<b class="content-title">매일유업</b><small class="activity-inline-meta">에센셜 · 오세일 · 실적 원천</small>', block)
        self.assertNotIn('오래된 일정', block)
        self.assertEqual(block.count('<b class="content-title">매일유업</b>'), 1)
        self.assertEqual(block.count('class="activity-row"'), fixture.count('class="activity-row"'))
        self.assertEqual(live.update_live_activity_rows(updated, [row], year=2026, month=9), updated)
        # Do not relabel unrefreshed historical measurements.
        a, b = live._month_bounds(source, 'mvr', 8)
        c, d = live._month_bounds(updated, 'mvr', 8)
        self.assertEqual(source[a:b], updated[c:d])

    def test_current_raw_chip_requires_selected_month(self):
        source = (ROOT / 'index.html').read_text()
        rolled = youtube.update_default_month_state(source, 10)
        script = re.search(r'var currentRawChip = .*?\n(?=function sourceTime)', rolled, re.S).group(0)
        phase = re.search(r'class="mvk mv" data-m="10" data-phase="([^"]+)"', rolled).group(1)
        self.assertEqual(phase, 'current')
        cases = [
            ('RAW 9/1~9/9', phase, 'RAW 확인 필요 · 10월', 'FORECAST'),
            ('RAW 10/1~10/9', phase, 'RAW 10/1~10/9', 'FORECAST'),
            ('LIVE RAW 10/1~확인중', phase, 'LIVE RAW 10/1~확인중', 'FORECAST'),
            ('RAW 10/1~9/9', phase, 'RAW 확인 필요 · 10월', 'FORECAST'),
            ('RAW 날짜 미확인', phase, 'RAW 확인 필요 · 10월', 'FORECAST'),
            ('RAW 9/1~9/9', 'future', '월전체 부킹 · 실적 아님', 'BOOKING'),
            ('RAW 9/1~9/9', 'pending_close', '마감 확인 필요 · 이전 집계', 'PENDING CLOSE'),
            ('RAW 9/1~9/9', 'closed', '매출 확정 · 10월', 'ACTUAL'),
        ]
        harness = r'''
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const results = input.cases.map(([raw, phase]) => {
  const chips = [{textContent: raw}, {textContent: ''}];
  const document = {
    querySelector(selector) {
      if (selector === '.chips .chip') return chips[0];
      if (selector === '.mvk[data-m="10"]') return {dataset: {phase}};
      if (selector === '#msel option[value="10"]') return {textContent: '2026년 10월'};
      throw new Error('Unexpected selector: ' + selector);
    },
    querySelectorAll() { return chips; },
    getElementById() { return {textContent: ''}; }
  };
  vm.runInNewContext(input.script + '\nsyncPeriodStatus(10);', {document});
  return chips.map(chip => chip.textContent);
});
process.stdout.write(JSON.stringify(results));
'''
        result = subprocess.run(
            ['node', '-e', harness], input=json.dumps(dict(script=script, cases=cases)),
            text=True, capture_output=True, check=True,
        )
        self.assertEqual(json.loads(result.stdout), [
            [expected, basis + ' 2026-10'] for _, _, expected, basis in cases
        ])


if __name__ == '__main__':
    unittest.main()
