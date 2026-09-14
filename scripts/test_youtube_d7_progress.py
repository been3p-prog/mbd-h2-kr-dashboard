import copy
import datetime as dt
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import youtube_verified_analytics as api
import refresh_owned_youtube_window_from_duckdb as renderer
import test_youtube_verified_analytics as fixtures
import verify_dashboard as guard

NOW = fixtures.NOW
START = dt.date(2026, 9, 2)
VIDEO = 'abcdefghijk'


def query(a, b, metrics, **extra):
    if extra.get('dimensions') == 'day':
        return [[day, 10, 2, 1, 0] for day in api.dates(a, b if isinstance(b, dt.date) else dt.date.fromisoformat(b))]
    n = (dt.date.fromisoformat(str(b))-a).days+1
    return [[10*n, 2*n, n, 0]]


class D7ProgressTest(unittest.TestCase):
    def test_grows_daily_and_caps_window_at_seven_days(self):
        for n in range(1, 10):
            r = api.collect_d7(query, VIDEO, START, START+dt.timedelta(days=n-1), NOW)
            self.assertEqual(r['views'], 10*min(n, 7))
            self.assertEqual(r['likes']+r['comments']+r['shares'], 3*min(n, 7))
            self.assertEqual(r['complete'], n>=7)
            self.assertEqual(r['actual_end'], str(START+dt.timedelta(days=min(n, 7)-1)))
            self.assertEqual('frozen_at' in r, n>=7)

    def test_missing_last_day_retains_partial_and_missing_first_day_stays_unavailable(self):
        def lagged(a, b, metrics, **kw):
            return query(a, dt.date(2026, 9, 7) if kw.get('dimensions') else b, metrics, **kw)
        r = api.collect_d7(lagged, VIDEO, START, dt.date(2026, 9, 8), NOW)
        self.assertEqual((r['views'], r['actual_end'], r['complete']), (60, '2026-09-07', False))
        empty = api.collect_d7(lambda *a, **kw: [], VIDEO, START, dt.date(2026, 9, 8), NOW)
        self.assertIsNone(empty['views'])
        self.assertIsNone(empty['actual_end'])
        future = api.collect_d7(Mock(side_effect=AssertionError('must not query')), VIDEO, START, START-dt.timedelta(days=1), NOW)
        self.assertIsNone(future['views'])

    def test_gaps_do_not_skip_into_later_days_and_real_zero_is_visible(self):
        def gap(a, b, metrics, **kw):
            if kw.get('dimensions'):
                return [['2026-09-02', 10, 1, 0, 0], ['2026-09-04', 100, 10, 1, 0]]
            self.assertEqual(str(b), '2026-09-02')
            return [[10, 1, 0, 0]]
        r=api.collect_d7(gap,VIDEO,START,dt.date(2026,9,8),NOW)
        self.assertEqual(r['views'],10)
        def zero(a,b,metrics,**kw):
            return [['2026-09-02',0,0,0,0]] if kw.get('dimensions') else [[0,0,0,0]]
        r=api.collect_d7(zero,VIDEO,START,START,NOW)
        self.assertEqual(r['views'],0)
        self.assertFalse(r['complete'])
        with self.assertRaises(ValueError):
            api.collect_d7(lambda *a, **k: [['2026-09-02',1],['2026-09-02',1]],VIDEO,START,START,NOW)

    def test_completed_video_is_not_requeried(self):
        frozen=api.collect_d7(query,VIDEO,START,dt.date(2026,9,8),NOW)
        yt, analytics, con=Mock(),Mock(),Mock()
        yt.channels().list().execute.return_value={'items':[{'id':api.CHANNEL,'contentDetails':{'relatedPlaylists':{'uploads':'x'}}}]}
        yt.playlistItems().list().execute.return_value={'items':[]}
        con.execute.return_value.fetchall.side_effect=[[(VIDEO,START)],[],[(VIDEO,)]]
        def sdk(**kw):
            self.assertNotIn('filters',kw)
            days=api.dates(dt.date.fromisoformat(kw['startDate']),dt.date(2026,9,8))
            rows=([[day,'shorts',1] for day in days] if kw['dimensions']=='day,creatorContentType'
                  else [[day]+[1]*len(kw['metrics'].split(',')) for day in days])
            r=Mock();r.execute.return_value={'rows':rows};return r
        analytics.reports().query.side_effect=sdk
        result=api.collect(yt,analytics,con,NOW,frozen=[frozen])
        self.assertEqual(result['d7'],[frozen])

    def snapshot(self,path):
        c=duckdb.connect(str(path))
        c.execute('create table dim_video(video_id varchar,publish_date date,is_active boolean,form varchar,title varchar,url varchar)')
        c.execute("insert into dim_video values (?,'2026-09-02',true,'SF','Example','https://www.youtube.com/watch?v=abcdefghijk')",[VIDEO])
        c.execute('''create table fact_analytics_d7(video_id varchar,metric_start_date date,metric_end_date date,
            requested_end_date date,fetched_at timestamp,view_count bigint,like_count bigint,comment_count bigint,
            share_count bigint,api_rows bigint,d7_complete boolean,source varchar,raw_status varchar,error varchar,
            primary key(video_id,metric_start_date))''')
        c.execute("create table v_latest_snapshot as select ? video_id, 999 cumulative_view_count, date '2026-09-11' snapshot_date",[VIDEO])
        c.execute("create table fact_snapshot as select date '2026-09-11' snapshot_date")
        c.execute('create table v_channel_daily_subscribers(snapshot_date date,subscriber_count bigint,captured_at timestamp)')
        c.close()

    def test_overlay_render_quality_and_freeze_survive_replacement_and_rollover(self):
        real_validate=api.validate
        with tempfile.TemporaryDirectory(dir='/tmp') as temp, patch.object(api,'validate',side_effect=lambda p,now=None:real_validate(p,now or NOW)):
            a=Path(temp)/'a.duckdb';b=Path(temp)/'b.duckdb'
            self.snapshot(a);self.snapshot(b)
            p=fixtures.payload();p['periods']=[]
            p['d7']=[api.collect_d7(query,VIDEO,START,dt.date(2026,9,4),NOW)]
            api.apply_overlay(a,p)
            c=duckdb.connect(str(a),read_only=True)
            rows,_=renderer.fetch_main_content(c,START,dt.date(2026,9,11))
            quality=renderer.fetch_quality_series(c,2026,9,as_of=dt.date(2026,9,11))
            self.assertEqual((rows[0]['d7_views'],rows[0]['pis'],rows[0]['d7_complete']),(30,9,False))
            self.assertEqual(quality[9]['completed'],0)
            rendered=renderer._activity_row(rows[0]);c.close()
            self.assertIn('3/7일 · 9/4까지',rendered)
            self.assertIn('<b>30</b>',rendered)
            self.assertIn('<b>9</b>',rendered)
            self.assertEqual(api.load_frozen_d7(a),[])
            p['d7']=[api.collect_d7(query,VIDEO,START,dt.date(2026,9,8),NOW)]
            p['frozen_d7']=copy.deepcopy(p['d7'])
            api.apply_overlay(a,p)
            frozen=api.load_frozen_d7(a)
            self.assertEqual(frozen[0]['views'],70)
            # Source copy may contain different values; archive still restores the freeze.
            p['d7']=copy.deepcopy(frozen);p['frozen_d7']=frozen
            api.apply_overlay(b,p)
            c=duckdb.connect(str(b),read_only=True)
            rows,_=renderer.fetch_main_content(c,START,dt.date(2026,9,11))
            self.assertEqual((rows[0]['d7_views'],rows[0]['pis'],rows[0]['d7_complete']),(70,21,True))
            self.assertIn('D7 확정',renderer._activity_row(rows[0]))
            self.assertEqual(renderer.fetch_quality_series(c,2026,9,as_of=dt.date(2026,9,11))[9]['completed'],1)
            c.close()
            self.assertEqual(api.load_frozen_d7(b),frozen)
            api.apply_overlay(b,p)
            self.assertEqual(api.load_frozen_d7(b),frozen)

    def test_progress_validation_rejects_fake_partial_or_changed_freeze(self):
        p=fixtures.payload()
        p['d7']=[api.collect_d7(query,VIDEO,START,dt.date(2026,9,4),NOW)]
        api.validate(p,NOW)
        for edit in ({'views':None},{'actual_end':'2026-09-09'},{'complete':True},{'views':-1}):
            bad=copy.deepcopy(p);bad['d7'][0].update(edit)
            with self.subTest(edit=edit),self.assertRaises(ValueError):api.validate(bad,NOW)
        p['d7']=[api.collect_d7(query,VIDEO,START,dt.date(2026,9,8),NOW)]
        p['frozen_d7']=copy.deepcopy(p['d7']);p['d7'][0]['views']=71
        with self.assertRaisesRegex(ValueError,'frozen value changed'):api.validate(p,NOW)

    def test_durable_archive_recovers_without_snapshot_and_rejects_bad_replacement(self):
        real_validate=api.validate
        with tempfile.TemporaryDirectory(dir='/tmp') as temp, patch.object(api,'validate',side_effect=lambda p,now=None:real_validate(p,now or NOW)):
            archive=Path(temp)/'durable/archive.json'
            p=fixtures.payload();p['periods']=[]
            p['d7']=[api.collect_d7(query,VIDEO,START,dt.date(2026,9,8),NOW)]
            p['frozen_d7']=copy.deepcopy(p['d7'])
            api.save_d7_archive(archive,p)
            original=archive.read_bytes()
            response=Mock(returncode=0,stdout=json.dumps(p))
            with patch.object(api.subprocess,'run',return_value=response) as run:
                result=api.fetch_overlay(['ssh','fixture'],'python',previous_snapshot=Path(temp)/'missing.duckdb',archive_path=archive)
            script=run.call_args.kwargs['input']
            self.assertIn('FROZEN_RECORDS = '+repr(p['frozen_d7']),script)
            compile(script,'remote-collector','exec')
            self.assertEqual(result['d7'],p['d7'])
            bad=copy.deepcopy(p);bad['channel_id']='wrong'
            with self.assertRaises(ValueError):api.save_d7_archive(archive,bad)
            bad=copy.deepcopy(p);bad['d7'][0]['views']=71;bad['frozen_d7'][0]['views']=71
            with self.assertRaisesRegex(ValueError,'durable freeze changed'):api.save_d7_archive(archive,bad)
            self.assertEqual(archive.read_bytes(),original)
            with patch.object(api.subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps(bad))):
                with self.assertRaisesRegex(ValueError,'collector changed'):api.fetch_overlay(['ssh','fixture'],'python',archive_path=archive)

    def test_missing_publication_aborts_overlay_without_changing_last_good(self):
        real_validate=api.validate
        with tempfile.TemporaryDirectory(dir='/tmp') as temp, patch.object(api,'validate',side_effect=lambda p,now=None:real_validate(p,now or NOW)):
            path=Path(temp)/'snapshot.duckdb';self.snapshot(path)
            p=fixtures.payload();p['periods']=[]
            api.apply_overlay(path,p)
            p['d7']=[]
            with self.assertRaisesRegex(ValueError,'publication coverage mismatch'):api.apply_overlay(path,p)
            c=duckdb.connect(str(path),read_only=True)
            self.assertEqual(json.loads(c.execute('select payload from dashboard_verified_youtube').fetchone()[0])['d7'][0]['views'],5)
            c.close()

    def test_transient_api_gap_cannot_erase_verified_partial_after_cache_loss(self):
        real_validate=api.validate
        with tempfile.TemporaryDirectory(dir='/tmp') as temp, patch.object(api,'validate',side_effect=lambda p,now=None:real_validate(p,now or NOW)):
            archive=Path(temp)/'archive.json'
            p=fixtures.payload();p['periods']=[]
            p['d7']=[api.collect_d7(query,VIDEO,START,dt.date(2026,9,4),NOW)]
            api.save_d7_archive(archive,p)
            for end in (None,dt.date(2026,9,3)):
                bad=copy.deepcopy(p)
                bad['d7']=[api.collect_d7(query if end else lambda *a,**kw: [],VIDEO,START,end or START,NOW)]
                with patch.object(api.subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps(bad))):
                    with self.assertRaisesRegex(ValueError,'coverage regressed'):
                        api.fetch_overlay(['ssh','fixture'],'python',archive_path=archive)
            self.assertEqual(json.loads(archive.read_text())['d7'][0]['views'],30)

    def test_static_guard_rejects_changed_d7_value_state_and_coverage(self):
        html=(Path(__file__).resolve().parents[1]/'index.html').read_text()
        row=re.search(r'<div class="activity-row"[^>]*data-yt-video-id="[^"]+"[^>]*data-yt-d7-state="frozen"[^>]*>.*?(?=<div class="activity-row"|</details>)',html,re.S)[0]
        self.assertEqual(guard.verify(html,dt.datetime.now(api.KST)),[])
        for bad_row in (
            re.sub(r'data-yt-d7-views="\d+"','data-yt-d7-views="999999999"',row),
            row.replace('data-yt-d7-state="frozen"','data-yt-d7-state="collecting"'),
            re.sub(r'data-yt-d7-end="[^"]+"','data-yt-d7-end="2020-01-01"',row),
            re.sub(r' data-yt-video-id="[^"]+"','',row),
        ):
            self.assertTrue(any('D7 display invalid' in e for e in guard.verify(html.replace(row,bad_row,1),dt.datetime.now(api.KST))))


if __name__=='__main__':unittest.main()
