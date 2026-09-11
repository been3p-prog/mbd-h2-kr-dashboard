import copy
import datetime as dt
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import duckdb
import youtube_schedule as ys
import refresh_owned_youtube_window_from_duckdb as yt
import fetch_target_youtube_snapshot as transport

NOW = dt.datetime(2026, 9, 11, 17, tzinfo=ys.KST)


class ScheduleTest(unittest.TestCase):
    def payload(self, rows=()):
        year, source = ys.source_contract()
        values = [[] for _ in range(5)] + [source['headers']] + list(rows)
        return ys.parse_sheet(values, captured_at=NOW.isoformat(), year=year, source=source)

    def published(self, video_id='AAAAAAAAAAA', day=2):
        return dict(video_id=video_id, publish_date=dt.date(2026, 9, day),
                    title='발행 영상', form='LF', views_total=5,
                    d7_views=3, pis=1, d7_complete=True)

    def render(self, payload, published=(), **kwargs):
        result = ys.reconcile(payload, list(published), year=2026, month=9,
                              as_of=NOW.date(), **kwargs)
        rendered = yt.render_main_ledger(year=2026, month=9, as_of=NOW.date(),
                    rows=list(published), snapshot_date=NOW.date(), schedule=result)
        ys.verify_coverage(rendered, result['coverage'], now=NOW, require_fresh=True)
        return rendered, result

    def test_missing_header_is_not_empty_but_verified_empty_is_valid(self):
        year, source = ys.source_contract()
        for values in ([], [[]] * 6):
            with self.assertRaisesRegex(RuntimeError, 'header'):
                ys.parse_sheet(values, captured_at=NOW.isoformat(), year=year, source=source)
        rendered, result = self.render(self.payload())
        self.assertEqual(result['coverage']['source_count'], 0)
        self.assertIn('편성 원천 0건', rendered)

    def test_all_rows_slots_and_community_preserved_without_metric_denominator_change(self):
        payload = self.payload([
            ['AAAAAAAAAAA', '9월 2일 수', '12:00', 'LF', 'IP', '발행 영상'],
            ['', '9월 30일 수', '18:00', 'SF', 'IP', '미래 영상'],
            ['', '9월 30일 수', '18:00', 'SF', 'IP', '미래 영상'],
            ['', '9월 23일 수', '', 'SF'],
            ['', '9월 12일 토', '12:00'],
            ['', '9월 10일 목', '', '커뮤니티', '', '공지'],
            ['', '9월 8일 화', '', 'SF', '', '발행 영상'],
        ])
        rendered, result = self.render(payload, [self.published()])
        counts = result['coverage']
        self.assertEqual((counts['source_count'], counts['matched_count'], counts['extra_count']), (6, 1, 5))
        self.assertEqual((counts['planned'], counts['slot'], counts['community'], counts['unmatched'], counts['empty_slot_count']), (2, 1, 1, 1, 1))
        self.assertEqual(rendered.count('data-content-link="youtube"'), 1)
        self.assertIn('D+7 완료 1건', rendered)
        self.assertEqual(rendered.count('<b>—</b>'), 15)
        dates = re.findall(r'<time class="activity-date" datetime="([^"]+)">', rendered)
        self.assertEqual(dates, ['2026-09-02'])
        self.assertEqual(rendered.count(' open>'), 5)

    def test_source_titles_are_escaped_and_unmatched_has_no_fake_link(self):
        rendered, _ = self.render(self.payload([['', '9월 30일 수', '', 'SF', '<ip>', '<script>x</script>']]))
        self.assertNotIn('<script>', rendered)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('href=', rendered)

    def test_exact_identity_and_cross_month_actual(self):
        payload = self.payload([['BBBBBBBBBBB', '9월 20일 일', '', 'SF', '', '발행 영상']])
        rendered, result = self.render(payload, [self.published()],
            known_videos={'BBBBBBBBBBB': {'publish_date': dt.date(2026, 8, 31)}})
        self.assertEqual(result['coverage']['published_unmatched_count'], 1)
        self.assertEqual(result['coverage']['other_period'], 1)
        self.assertEqual(result['coverage']['planned'], 0)
        self.assertIn('2026-08-31', rendered)

    def test_rebuild_handles_source_deletion_and_replanning(self):
        first = self.payload([['', '9월 20일 일', '', 'SF', '', '예정']])
        later = self.payload([['', '9월 30일 수', '', 'SF', '', '예정']])
        self.assertNotEqual(first['sha256'], later['sha256'])
        rendered, _ = self.render(later)
        self.assertNotIn('datetime="2026-09-20"', rendered)
        self.assertIn('datetime="2026-09-30"', rendered)
        self.assertEqual(self.render(self.payload())[1]['coverage']['source_count'], 0)

    def test_bad_dates_duplicate_ids_and_conflicts_fail_closed(self):
        for rows in (
            [['', '9월 31일', '', 'SF']],
            [['', '', '', 'SF']],
            [['', '2025-09-01', '', 'SF']],
            [['AAAAAAAAAAA', '9월 1일', '', 'LF']] * 2,
            [['AAAAAAAAAAA', '9월 1일', '', 'LF', '', '', 'https://youtu.be/BBBBBBBBBBB']],
        ):
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                self.payload(rows)

    def test_only_allowlisted_youtube_identifiers(self):
        for url in ('https://youtu.be/AAAAAAAAAAA', 'https://www.youtube.com/watch?v=AAAAAAAAAAA',
                    'https://www.youtube.com/shorts/AAAAAAAAAAA'):
            self.assertEqual(ys.identity('', url, 'SF'), 'AAAAAAAAAAA')
        for url in ('javascript:alert(1)', 'https://evil.test/watch?v=AAAAAAAAAAA',
                    'https://www.youtube.com@evil.test/watch?v=AAAAAAAAAAA',
                    'https://www.youtube.com/watch?v=AAAAAAAAAAA&v=BBBBBBBBBBB'):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                ys.identity('', url, 'SF')
        self.assertEqual(ys.identity('', 'https://www.youtube.com/post/Ug123abc', '커뮤니티'), 'Ug123abc')

    def test_coverage_rejects_missing_duplicate_wrong_identity_and_count(self):
        rendered, result = self.render(self.payload([['', '9월 30일 수', '', 'SF', '', '예정']]))
        key = result['coverage']['source_keys'][0]
        for bad in (rendered.replace(f'data-yt-schedule-key="{key}"', ''),
                    rendered + f'<div data-yt-schedule-key="{key}"></div>',
                    rendered.replace(key, '0' * 24),
                    rendered.replace('data-yt-schedule-planned="1"', 'data-yt-schedule-planned="0"')):
            with self.assertRaises(RuntimeError):
                ys.verify_coverage(bad, result['coverage'])

    def test_capture_integrity_freshness_and_missing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'snapshot.duckdb'
            with duckdb.connect(str(path)) as con:
                with self.assertRaisesRegex(RuntimeError, 'not connected'):
                    ys.load_snapshot(con, now=NOW)
            payload = self.payload()
            ys.store_snapshot(path, payload)
            with duckdb.connect(str(path), read_only=True) as con:
                self.assertEqual(ys.load_snapshot(con, now=NOW), payload)
                with self.assertRaisesRegex(RuntimeError, 'stale'):
                    ys.load_snapshot(con, now=NOW + dt.timedelta(days=3))
            bad = copy.deepcopy(payload)
            bad['source_rows'] = 1
            ys.store_snapshot(path, bad)
            with duckdb.connect(str(path), read_only=True) as con:
                with self.assertRaisesRegex(RuntimeError, 'integrity'):
                    ys.load_snapshot(con, now=NOW)

    def test_schedule_metric_mutation_is_rejected(self):
        rendered, result = self.render(self.payload([['', '9월 30일 수', '', 'SF', '', '예정']]))
        with self.assertRaisesRegex(RuntimeError, 'unavailable metrics'):
            ys.verify_coverage(rendered.replace('<b>—</b>', '<b>999</b>', 1), result['coverage'])

    def test_previous_month_keeps_schedule_rows_and_current_observation_cutoff(self):
        payload = self.payload([['AAAAAAAAAAA', '8월 31일 월', '', 'LF', '', '월말 편성'],
                                ['', '8월 30일 일', '', '커뮤니티', '', '공지']])
        result = ys.reconcile(payload, [], year=2026, month=8, as_of=NOW.date(),
            known_videos={'AAAAAAAAAAA': {'publish_date': dt.date(2026, 9, 1)}})
        rendered = yt.render_main_ledger(year=2026, month=8, as_of=dt.date(2026, 8, 31),
                                        rows=[], snapshot_date=NOW.date(), schedule=result)
        ys.verify_coverage(rendered, result['coverage'])
        self.assertEqual(result['coverage']['other_period'], 1)
        self.assertEqual(result['coverage']['community'], 1)
        self.assertEqual(result['coverage']['rendered_count'], 2)

    def test_full_grid_is_read_and_http_failure_not_zero(self):
        _, source = ys.source_contract()
        responses = [SimpleNamespace(status_code=200, json=lambda: {'sheets': [{'properties': {
            'sheetId': source['sheet_id'], 'title': source['sheet_title'], 'gridProperties': {'rowCount': 1086}}}]}),
            SimpleNamespace(status_code=200, json=lambda: {'values': [[]] * 5 + [source['headers']]})]
        session = SimpleNamespace(get=unittest.mock.Mock(side_effect=responses))
        self.assertEqual(ys.fetch_sheet(session=session, now=NOW)['source_rows'], 0)
        self.assertIn('A1%3AG1086', session.get.call_args_list[1].args[0])
        session.get = unittest.mock.Mock(return_value=SimpleNamespace(status_code=403))
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
            ys.fetch_sheet(session=session, now=NOW)

    def test_sheet_failure_preserves_last_good_transport_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, key = Path(tmp) / 'last-good.duckdb', Path(tmp) / 'key'
            output.write_bytes(b'last-good')
            key.touch()
            def run(args, **kwargs):
                if args[0] == 'scp':
                    Path(args[-1]).write_bytes(b'candidate')
                return SimpleNamespace(returncode=0, stderr='')
            with patch.object(transport.subprocess, 'run', side_effect=run), \
                 patch.object(transport, 'validate_snapshot', return_value={}), \
                 patch.object(ys, 'fetch_sheet', side_effect=RuntimeError('Sheet unavailable')):
                with self.assertRaisesRegex(RuntimeError, 'Sheet unavailable'):
                    transport.sync_snapshot(output, key=key, include_schedule=True)
            self.assertEqual(output.read_bytes(), b'last-good')
            self.assertEqual(list(Path(tmp).glob('*.partial')), [])

    def test_production_cli_requires_schedule_capture(self):
        with patch.object(sys, 'argv', ['fetch', '--quiet']), patch.object(transport, 'sync_snapshot') as sync:
            transport.main()
        self.assertTrue(sync.call_args.kwargs['include_schedule'])


if __name__ == '__main__':
    unittest.main()
