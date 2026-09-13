import copy
import datetime as dt
import hashlib
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bot_client', ROOT/'integrations/dashboard_metrics_client.py')
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)
NOW = dt.datetime(2026,9,11,20,tzinfo=client.KST)


def fixture():
    r = dict(date='2026-09-10',brand='까사미아',package='에센셜',gmv=100,gmv_1d=200,gmv_1h=90,
             viewers=50,free=False,attributed=False,unknown_party=True,af=10)
    return dict(schema=client.SCHEMA,as_of='2026-09-11',built_at=NOW.isoformat(),dashboard_sha256=hashlib.sha256(b'html').hexdigest(),
        source_as_of={k:NOW.isoformat() for k in ('revenue_mirror','live_quality','yt_quality','owned_media')},
        live=[r,dict(r,date='2026-09-14',brand='시몬스',gmv=None,gmv_1d=None,gmv_1h=None,viewers=None,af=None)],
        revenue={'months':{},'days':[{'date':'2026-09-10','ad_gen_won':20,'ad_int_won':30,'live_won':0}]},
        youtube={'content':[dict(publish_date='2026-09-10',video_id='abcdefghijk',form='LF',title='<@everyone>',d7_views=None,d7_complete=False,views_total=50)],
                 'periods':[dict(period_type='month',period_start='2026-09-01',period_end='2026-09-30',metric_start_date='2026-09-01',metric_end_date='2026-09-10',period_complete=False,available=True,views=100,new_views=40,prior_views=50,residual=10)],
                 'subscribers':[],'schedules':{}})


class ParityTests(unittest.TestCase):
    def setUp(self): self.p=fixture()
    def answer(self,q,domain='live'): return client.answer(self.p,q,domain,NOW.date())
    def test_valid_public_version(self): client.validate(self.p,NOW,b'html')
    def test_mismatched_public_version(self):
        with self.assertRaises(ValueError):client.validate(self.p,NOW,b'other')
    def test_schema(self):
        self.p['schema']='old'
        with self.assertRaises(ValueError):client.validate(self.p,NOW,b'html')
    def test_stale_build(self):
        self.p['built_at']=(NOW-dt.timedelta(days=3)).isoformat()
        with self.assertRaises(ValueError):client.validate(self.p,NOW,b'html')
    def test_missing_source_clock(self):
        self.p['source_as_of'].pop('revenue_mirror')
        with self.assertRaises(ValueError):client.validate(self.p,NOW,b'html')
    def test_rebuilding_does_not_refresh_source(self):
        self.p['source_as_of']['revenue_mirror']=(NOW-dt.timedelta(days=3)).isoformat()
        with self.assertRaises(ValueError):client.validate(self.p,NOW,b'html')
    def test_month_partial(self):
        a=self.answer('9월 유튜브 성과','youtube');self.assertIn('100회 · 진행 중',a);self.assertIn('2026-09-10',a)
    def test_day_not_month(self):
        a=self.answer('9월 10일 유튜브 성과','youtube');self.assertIn('채널 기간 조회수: 확인 못 함',a);self.assertNotIn('100회',a)
    def test_lf_not_channel_total(self):
        a=self.answer('9월 롱폼 조회수','youtube');self.assertIn('LF 기간 조회수: 확인 못 함',a);self.assertNotIn('100회',a)
    def test_pending_d7_not_zero(self):
        a=self.answer('9월 유튜브 상세','youtube');self.assertIn('D7 —',a);self.assertIn('평균 —',a)
    def test_source_text_cannot_mention(self):
        self.assertNotIn('<@everyone>',self.answer('9월 유튜브 상세','youtube'))
    def test_answers_do_not_emit_source_footer(self):
        for domain, question in [
            ('live','9월 라이브 성과'),
            ('youtube','9월 유튜브 성과'),
            ('ads','9월 10일 일반광고와 통광마 매출'),
        ]:
            with self.subTest(domain=domain):
                self.assertNotIn('출처:', self.answer(question, domain))
                self.assertNotIn('대시보드 동일 스냅샷', self.answer(question, domain))
    def test_brand_does_not_become_total(self):
        a=self.answer('9월 시몬스 라이브 성과');self.assertIn('성과 확인 0건',a);self.assertNotIn('200원',a)
    def test_unknown_brand_fails_closed(self):
        a=self.answer('9월 없는브랜드 라이브 성과');self.assertIn('조건을 확정하지 못했습니다',a);self.assertNotIn('200원',a)
    def test_day_live(self):
        a=self.answer('9월 10일 라이브 상세');self.assertIn('200원',a);self.assertIn('100원',a)
    def test_missing_attribution_disclosed(self):
        a=self.answer('9월 라이브 성과');self.assertIn('귀속 확인 필요',a);self.assertIn('합계에서 제외',a)
    def test_future_not_actual(self):
        a=self.answer('9월 시몬스 라이브 성과');self.assertIn('지표 —',a);self.assertIn('미래 편성 1건',a)
    def test_free_excluded_quality(self):
        self.p['live'][0]['free']=True
        self.assertIn('성과 확인 0건',self.answer('9월 라이브 성과'))
    def test_week_not_month_week(self):
        self.assertEqual(client.period('지난주 라이브 성과',NOW.date())[:2],(dt.date(2026,8,31),dt.date(2026,9,6)))
        self.assertEqual(client.period('9월 1주차 라이브 성과',NOW.date())[:2],(dt.date(2026,9,1),dt.date(2026,9,7)))
    def test_day_ads_supported(self):
        a=self.answer('9월 10일 광고 매출','ads');self.assertIn('50원',a);self.assertIn('기간 RAW',a)
    def test_not_admin_mutation(self):
        for q in ['9월 부킹 취소해','9월 광고 매출 메모 수정해','부킹 기준 8월 광고 매출','9월 전면배너 부킹률']:
            self.assertIsNone(self.answer(q,'ads'))
    def test_invalid_period_no_default(self):
        self.assertIn('확인 필요',self.answer('13월 라이브 성과'))
    def test_multiple_brands_no_default(self):
        self.assertIn('하나씩',self.answer('9월 시몬스 까사미아 라이브 성과'))
    def test_future_clock(self):
        self.p['built_at']=(NOW+dt.timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):client.validate(self.p,NOW,b'html')
    def test_missing_day_source_not_zero(self):
        a=self.answer('9월 11일 광고 매출','ads');self.assertIn('확인 못 함',a);self.assertNotIn('(0원)',a)
    def test_ads_unknown_brand_not_total(self):
        self.assertIn('조건을 확정하지 못했습니다',self.answer('9월 삼성 광고 매출','ads'))
    def test_youtube_unknown_brand_not_total(self):
        self.assertIn('조건을 확정하지 못했습니다',self.answer('9월 없는브랜드 유튜브 성과','youtube'))
    def test_two_periods(self):
        self.assertIn('기간을 하나씩',self.answer('이번주와 지난주 라이브 성과'))
    def test_submonth_not_whole_month(self):
        self.assertIn('시작일과 종료일',self.answer('8월 초 라이브 성과'))
    def test_month_week_preserves_year(self):
        self.assertEqual(client.period('2025년 9월 1주차',NOW.date())[0],dt.date(2025,9,1))
    def test_missing_gmv_not_zero(self):
        self.p['live'][0]['gmv']=None
        self.assertIn('방송별 GMV: 확인 못 함',self.answer('9월 라이브 성과'))
    def test_missing_af_not_zero(self):
        self.p['live'][0].update(af=None,attributed=True,unknown_party=False)
        self.assertIn('RAW 귀속 매출(3P AF): 확인 못 함',self.answer('9월 라이브 성과'))
    def test_month_week_answer_not_just_parser(self):
        self.assertIn('월내 1~7일',self.answer('9월 1주차 라이브 성과'))
    def test_multiple_revenue_teams(self):
        a=self.answer('9월 10일 일반광고와 통광마 매출','ads')
        self.assertIn('50원',a);self.assertNotIn('라이브 RAW:',a)
    def test_live_only_revenue(self):
        a=self.answer('9월 10일 라이브 매출','ads')
        self.assertNotIn('일반광고 RAW:',a);self.assertIn('라이브 RAW:',a)
    def test_filtered_schedule_not_total(self):
        a=self.answer('9월 롱폼 유튜브 편성','youtube')
        self.assertIn('폼 조건을 전체 합계로 바꾸지 않습니다',a)
    def test_unit_tokens_not_stripped(self):
        self.assertIn('1D 브랜드 거래액:',self.answer('9월 라이브 1D 거래액'))
        self.assertIn('D+7 완료',self.answer('9월 유튜브 D7 성과','youtube'))
    def test_live_answer_starts_with_decision_summary(self):
        lines=self.answer('9월 라이브 성과').splitlines()
        self.assertTrue(lines[1].startswith('• 요약: 성과 확인 1/2건'))
        self.assertIn('귀속 미확인 1건 별도',lines[1])
    def test_ads_answer_starts_with_decision_summary(self):
        lines=self.answer('9월 10일 일반광고와 통광마 매출','ads').splitlines()
        self.assertEqual(lines[1],'• 요약: 기간 RAW 0.00억 · 일반광고 + 통광마 · 확정/월전체 예측 아님')
    def test_youtube_answer_starts_with_decision_summary(self):
        lines=self.answer('9월 유튜브 성과','youtube').splitlines()
        self.assertEqual(lines[1],'• 요약: 조회수 100회(2026-09-10까지) · 발행 1건 · D+7 완료 0/1건')
    def test_youtube_month_answer_stays_compact(self):
        a=self.answer('9월 유튜브 성과','youtube')
        self.assertLessEqual(len(a.splitlines()),8)
        self.assertNotIn('플랫폼 포맷:',a)
        self.assertNotIn('기간 내 발행 기여',a)


if __name__=='__main__':unittest.main()
