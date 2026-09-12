import copy
import datetime as dt
import unittest
from unittest.mock import patch, Mock
from pathlib import Path
import sys
import tempfile
import duckdb
sys.path.insert(0,str(Path(__file__).resolve().parent))
import youtube_verified_analytics as api
from test_bot_metric_parity import client, fixture

NOW=dt.datetime(2026,9,11,22,tzinfo=api.KST)


def payload():
    return dict(schema=api.SCHEMA,channel_id=api.CHANNEL,captured_at=NOW.isoformat(),
        coverage_start='2026-09-01',requested_end='2026-09-10',actual_end='2026-09-08',quality_cutoff='2026-09-08',
        daily=[dict(date=d,views=10) for d in api.dates(dt.date(2026,9,1),dt.date(2026,9,8))],
        daily_formats=[dict(date=d,form='shorts',views=6) for d in api.dates(dt.date(2026,9,1),dt.date(2026,9,8))],
        periods=[dict(period_type='month',period_start='2026-09-01',period_end='2026-09-30',metric_end_date='2026-09-08',
            views=85,new_views=20,prior_views=60,residual=5,likes=5,comments=2,shares=1,formats={'shorts':48,'videoOnDemand':37},daily_residual=5)],
        d7=[dict(video_id='abcdefghijk',start='2026-09-02',end='2026-09-08',actual_end='2026-09-08',complete=True,views=5,likes=2,comments=1,shares=0)],
        discovered=[dict(video_id='ABCDEFGHIJK',channel_id=api.CHANNEL,privacy='public',published_date='2026-09-10',
            published_at='2026-09-10T10:00:00+00:00',title='<script>alert(1)</script>',views=4)])


class VerifiedTests(unittest.TestCase):
    def setUp(self):self.p=payload()
    def test_collector_uses_bounded_sdk_retries_without_swallowing_failure(self):
        yt, analytics = Mock(), Mock()
        yt.channels().list().execute.return_value = {'items': [{'id': api.CHANNEL}]}
        request = analytics.reports().query.return_value
        request.execute.side_effect = RuntimeError('API retries exhausted')
        with self.assertRaisesRegex(RuntimeError, 'retries exhausted'):
            api.collect(yt, analytics, Mock(), NOW)
        request.execute.assert_called_once_with(num_retries=2)
        self.assertEqual(analytics.reports().query.call_args.kwargs['maxResults'], 10000)
    def test_d7_uses_canonical_day_query_shape_and_leaves_missing_last_day_pending(self):
        yt, analytics, con = Mock(), Mock(), Mock()
        yt.channels().list().execute.return_value = {'items': [
            {'id': api.CHANNEL, 'contentDetails': {'relatedPlaylists': {'uploads': 'fixture'}}}]}
        yt.playlistItems().list().execute.return_value = {'items': []}
        con.execute.return_value.fetchall.side_effect = [
            [('abcdefghijk', dt.date(2026, 8, 27))], [], [('abcdefghijk',)]]
        def query(**kwargs):
            self.assertEqual(kwargs['maxResults'], 10000)
            start = dt.date.fromisoformat(kwargs['startDate'])
            end = dt.date(2026, 9, 1) if kwargs.get('filters') else dt.date(2026, 9, 8)
            if kwargs['dimensions'] == 'day,creatorContentType':
                rows = [[day, 'shorts', 1] for day in api.dates(start, end)]
            else:
                rows = [[day] + [1] * len(kwargs['metrics'].split(',')) for day in api.dates(start, end)]
            request = Mock()
            request.execute.return_value = {'rows': rows}
            return request
        analytics.reports().query.side_effect = query
        result = api.collect(yt, analytics, con, NOW)
        self.assertEqual(len(result['d7']), 1)
        self.assertFalse(result['d7'][0]['complete'])
        self.assertEqual(result['d7'][0]['actual_end'], '2026-09-01')
        self.assertIsNone(result['d7'][0]['views'])
    def test_valid(self):api.validate(self.p,NOW)
    def reject(self):
        with self.assertRaises(ValueError):api.validate(self.p,NOW)
    def test_wrong_channel(self):self.p['channel_id']='other';self.reject()
    def test_stale_collection(self):self.p['captured_at']=(NOW-dt.timedelta(days=3)).isoformat();self.reject()
    def test_daily_gap(self):self.p['daily'].pop(3);self.reject()
    def test_daily_duplicate(self):self.p['daily'].append(self.p['daily'][0]);self.reject()
    def test_negative_count(self):self.p['daily'][0]['views']=-1;self.reject()
    def test_bool_count(self):self.p['daily'][0]['views']=True;self.reject()
    def test_unknown_format(self):self.p['daily_formats'][0]['form']='guessedLF';self.reject()
    def test_future_cutoff(self):self.p['actual_end']='2026-09-12';self.reject()
    def test_d7_requested_not_actual(self):self.p['d7'][0]['actual_end']='2026-09-06';self.reject()
    def test_d7_after_available(self):self.p['d7'][0].update(end='2026-09-10',actual_end='2026-09-10');self.reject()
    def test_d7_pending_allowed(self):self.p['d7'][0].update(complete=False,views=None,actual_end=None);api.validate(self.p,NOW)
    def test_private_discovery(self):self.p['discovered'][0]['privacy']='private';self.reject()
    def test_wrong_discovery_channel(self):self.p['discovered'][0]['channel_id']='other';self.reject()
    def test_injected_id(self):self.p['discovered'][0]['video_id']='"><script>';self.reject()
    def test_future_publication(self):self.p['discovered'][0]['published_at']='2026-09-12T01:00:00+00:00';self.reject()
    def test_duplicate_discovery(self):self.p['discovered'].append(self.p['discovered'][0]);self.reject()
    def test_period_reconciliation(self):self.p['periods'][0]['residual']=0;self.reject()
    def test_duplicate_period(self):self.p['periods'].append(self.p['periods'][0]);self.reject()
    def test_no_canonical_write(self):
        with patch.object(api,'validate',return_value=self.p):
            with self.assertRaises(ValueError):api.apply_overlay(Path('/Users/cnc-media/automations/youtube-view-snapshot/youtube_views.duckdb'),self.p)
    def test_overlay_clears_old_completion_without_inserting_discovery_into_dim(self):
        with tempfile.TemporaryDirectory(prefix='mbd-api-test-',dir='/tmp') as root:
            path=Path(root)/'snapshot.duckdb'
            c=duckdb.connect(str(path))
            c.execute('create table dim_video(video_id varchar,publish_date date,is_active boolean)')
            c.execute("insert into dim_video values('abcdefghijk','2026-09-02',true)")
            c.execute('''create table fact_analytics_d7(video_id varchar,metric_start_date date,metric_end_date date,
                requested_end_date date,fetched_at timestamp,view_count bigint,like_count bigint,comment_count bigint,
                share_count bigint,api_rows bigint,d7_complete boolean,source varchar,raw_status varchar,error varchar,
                primary key(video_id,metric_start_date))''')
            c.execute("insert into fact_analytics_d7(video_id,metric_start_date,d7_complete) values('abcdefghijk','2026-09-01',true)")
            c.close()
            self.p['periods']=[]
            self.p['d7'][0].update(complete=False,actual_end=None,views=None)
            with patch.object(api,'validate',return_value=self.p):api.apply_overlay(path,self.p)
            c=duckdb.connect(str(path),read_only=True)
            self.assertEqual(c.execute('select count(*) from fact_analytics_d7 where d7_complete').fetchone()[0],0)
            self.assertEqual(c.execute('select count(*) from dim_video').fetchone()[0],1)
            self.assertEqual(c.execute('select count(*) from dashboard_verified_youtube').fetchone()[0],1)
            c.close()
            # A concurrent date change must rollback; last-good overlay remains intact.
            self.p['d7'][0]['start']='2026-09-03'
            with patch.object(api,'validate',return_value=self.p):
                with self.assertRaises(ValueError):api.apply_overlay(path,self.p)
            c=duckdb.connect(str(path),read_only=True)
            self.assertEqual(c.execute('select count(*) from fact_analytics_d7').fetchone()[0],2)
            c.close()
    def test_html_escape_and_separate_denominator(self):
        h=api.render_overlay(self.p,9,daily=True)
        self.assertNotIn('<script>alert',h);self.assertIn('분모에 합산하지 않습니다',h)
        self.assertIn('data-yt-api-day="2026-09-08"',h)
        self.assertNotIn('data-yt-api-day="2026-09-10"',h)
    def answer(self,q):
        p=fixture();p['youtube']['verified_api']=self.p
        return client.answer(p,q,'youtube',NOW.date())
    def test_bot_month_not_daily_sum(self):
        a=self.answer('9월 유튜브 조회수');self.assertIn('85회 · 공식 기간 조회',a);self.assertIn('차이 +5회',a)
    def test_bot_daily_not_month(self):
        a=self.answer('9월 8일 유튜브 조회수');self.assertIn('10회 · 공식 일별 조회',a);self.assertNotIn('85회',a)
    def test_bot_unavailable_day_not_zero(self):
        a=self.answer('9월 10일 유튜브 조회수');self.assertIn('집계 대기',a);self.assertNotIn('조회수: 0',a)
    def test_bot_form_basis_disclosed(self):
        a=self.answer('9월 숏폼 조회수');self.assertIn('수기 SF와 별도',a);self.assertIn('48회',a);self.assertNotIn('85회',a)
    def test_bot_range_partial(self):
        a=self.answer('9월 7일 9월 10일 유튜브 조회수');self.assertIn('20회',a);self.assertIn('이후 날짜는 집계 대기',a)
    def test_bot_missing_days_fail_closed(self):
        self.p['daily'].pop(7);self.assertIn('누락·중복',self.answer('9월 8일 유튜브 조회수'))
    def test_bot_discovery_escaped(self):
        a=self.answer('9월 유튜브 상세');self.assertNotIn('<script>alert',a);self.assertIn('공개 업로드 추가 확인 1건',a)
    def test_slack_title_emoji_only_transport_equivalence(self):
        import importlib.util
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'integrations'))
        spec=importlib.util.spec_from_file_location('slack_probe',Path(__file__).resolve().parents[1]/'integrations/test_slack_parity.py')
        probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
        n=probe.normalize_reply
        self.assertEqual(n('🔴 방송 🏠 1,439,009 · 9/8'),n(':red_circle: 방송 :house: 1,439,009 · 9/8'))
        self.assertNotEqual(n('🔴 1,439,009 · 9/8'),n(':red_circle: 1,439,010 · 9/8'))
        self.assertNotEqual(n('🔴 1,439,009 · 9/8'),n(':red_circle: 1,439,009 · 9/10'))


if __name__=='__main__':unittest.main()
