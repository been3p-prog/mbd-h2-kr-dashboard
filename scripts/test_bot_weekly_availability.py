import copy
import datetime as dt
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('weekly',ROOT/'integrations/youtube_weekly_rules.py')
weekly=importlib.util.module_from_spec(spec);spec.loader.exec_module(weekly)


class WeeklyAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.p=dict(week=['2026-09-14','2026-09-20'], pending=None,
                    captured_at='2026-09-21T19:00:00+09:00',
                    groups={'new_LF':['a'],'new_SF':['b','c']}, windows={})
        for name in ('D5','D7'):
            self.p['windows'][name]=dict(actual_end='2026-09-18',complete=name=='D5',
                 previous_channel=500 if name=='D5' else None,
                 values=dict(channel=1000,new_LF=100,new_SF=200,prior_LF=300,prior_SF=300,LIVE=50,residual=50))

    def test_deployed_weekly_parser_accepts_actual_test_question(self):
        with patch.object(weekly,'CLIENT',ROOT/'integrations/dashboard_metrics_client.py'):
            self.assertEqual(weekly.weekly_request('지난주 유튜브 성과 핵심만 알려줘',dt.date(2026,9,21)),
                             (dt.date(2026,9,14),dt.date(2026,9,20)))
            with self.assertRaises(ValueError):
                weekly.weekly_request('지난주 삼성 유튜브 성과 핵심만 알려줘',dt.date(2026,9,21))

    def test_summary_is_concise_and_partial_week_is_not_complete(self):
        a=weekly.render(self.p,'지난주 유튜브 성과 핵심만 알려줘')
        self.assertLessEqual(len(a.splitlines()),7)
        self.assertIn('1,000회',a)
        self.assertIn('D7 채널 조회수 1,000회 · 2026-09-18까지(집계 중)',a)
        self.assertIn('편성 확인 중',a)
        self.assertIn('전주 대비 +100.0%',a)
        self.assertNotIn('출처',a);self.assertNotIn('재조회',a)

    def test_single_format_keeps_channel_and_filtered_views_distinct(self):
        a=weekly.render(self.p,'지난주 롱폼 조회수')
        self.assertIn('채널 조회수 1,000회',a)
        self.assertIn('LF 신규 100회 · 기발행 300회',a)

    def test_empty_window_is_pending_not_zero(self):
        self.p['windows']['D7']['values']=None
        a=weekly.render(self.p,'지난주 유튜브 성과')
        self.assertIn('D7: 집계 대기',a)
        self.assertNotIn('D7 채널 조회수 0회',a)

    def test_detail_preserves_cohort_arithmetic(self):
        a=weekly.render(self.p,'지난주 유튜브 상세')
        self.assertIn('신규 LF/SF: 300회',a)
        self.assertIn('기발행 LF/SF: 600회',a)


if __name__=='__main__':unittest.main()
