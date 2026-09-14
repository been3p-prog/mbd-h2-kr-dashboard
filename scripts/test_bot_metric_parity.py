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
    def answer(self,q,domain='live'): return client.answer(self.p,q,domain,NOW.date(),now=NOW)
    def schedule(self):
        matched = dict(key='a'*24,date='2026-09-10',time='',form='LF',ip='',title='발행 영상',item_id='abcdefghijk',community=False)
        extras = [
            dict(key='b'*24,date='2026-09-14',time='18:00',form='SF',ip='시리즈',title='예정 영상',item_id='',community=False,state='planned',actual_date=None),
            dict(key='c'*24,date='2026-09-10',time='',form='SF',ip='',title='확인 영상',item_id='',community=False,state='unmatched',actual_date=None),
            dict(key='d'*24,date='2026-09-12',time='',form='커뮤니티',ip='',title='커뮤니티 공지',item_id='',community=True,state='community',actual_date=None),
        ]
        coverage = dict(source_count=4,matched_count=1,extra_count=3,published_unmatched_count=0,
                        published_count=1,empty_slot_count=2,rendered_count=4,captured_at=NOW.isoformat(),
                        source_sha256='e'*64,source_keys=[row['key'] for row in [matched]+extras],
                        planned=1,unmatched=1,community=1,slot=0,other_period=0)
        return dict(coverage=coverage,extras=extras,by_video={'abcdefghijk':matched})
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
        a=self.answer('9월 10일 유튜브 성과','youtube');self.assertIn('채널 조회수: 확인 못 함',a);self.assertNotIn('100회',a)
    def test_lf_not_channel_total(self):
        a=self.answer('9월 롱폼 조회수','youtube');self.assertIn('LF 조회수: 확인 못 함',a);self.assertNotIn('100회',a)
    def test_pending_d7_not_zero(self):
        a=self.answer('9월 유튜브 상세','youtube');self.assertIn('D7 —',a);self.assertIn('평균 —',a)
    def test_progressive_d7_is_labeled_and_not_in_completed_average(self):
        self.p['youtube']['content'][0].update(d7_views=123,pis=7,d7_metric_end='2026-09-10')
        a=self.answer('9월 유튜브 상세','youtube')
        self.assertIn('D7 123 · PIS 7 (집계중 · 2026-09-10까지)',a)
        self.assertIn('D+7 완료 0/1건 · 평균 —',a)
    def test_top_d7_does_not_rank_incomplete_windows(self):
        self.p['youtube']['content'][0].update(d7_views=999,title='PARTIAL_TITLE',d7_metric_end='2026-09-10')
        self.assertNotIn('PARTIAL_TITLE',self.answer('9월 유튜브 상위 상세','youtube'))
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
                self.assertNotIn('revenue.v_revenue_forecast_monthly', self.answer(question, domain))
                self.assertNotIn('integrated_ssot', self.answer(question, domain))
    def test_brand_does_not_become_total(self):
        a=self.answer('9월 시몬스 라이브 성과');self.assertIn('성과 0/1건',a);self.assertNotIn('200원',a)
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
        self.assertIn('성과 0/2건',self.answer('9월 라이브 성과'))
    def test_week_not_month_week(self):
        self.assertEqual(client.period('지난주 라이브 성과',NOW.date())[:2],(dt.date(2026,8,31),dt.date(2026,9,6)))
        self.assertEqual(client.period('9월 1주차 라이브 성과',NOW.date())[:2],(dt.date(2026,9,1),dt.date(2026,9,7)))
    def test_day_ads_supported(self):
        a=self.answer('9월 10일 광고 매출','ads');self.assertIn('50원',a);self.assertIn('기간 누적',a)
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
        self.assertIn('*방송 GMV*: 확인 못 함',self.answer('9월 라이브 성과'))
    def test_missing_af_not_zero(self):
        self.p['live'][0].update(af=None,attributed=True,unknown_party=False)
        self.assertIn('*귀속 매출(확인분)*: 확인 못 함',self.answer('9월 라이브 성과'))
    def test_month_week_answer_not_just_parser(self):
        self.assertIn('월내 1~7일',self.answer('9월 1주차 라이브 성과'))
    def test_multiple_revenue_teams(self):
        a=self.answer('9월 10일 일반광고와 통광마 매출','ads')
        self.assertIn('50원',a);self.assertNotIn('라이브 RAW:',a)
    def test_live_only_revenue(self):
        a=self.answer('9월 10일 라이브 매출','ads')
        self.assertTrue(a.startswith('*라이브 매출'))
        self.assertNotIn('일반광고',a);self.assertIn('기간 누적',a)
    def test_filtered_schedule_not_total(self):
        a=self.answer('9월 롱폼 유튜브 편성','youtube')
        self.assertIn('롱폼·숏폼 합계로 바꾸지 않습니다',a)
    def test_schedule_answer_is_verified_concise_and_user_facing(self):
        self.p['youtube']['schedules']['2026-09']=self.schedule()
        self.p['youtube']['verified_api']={'discovered':[dict(published_date='2026-09-11')]}
        a=self.answer('9월 유튜브 편성','youtube')
        self.assertIn('총 4건 · 편성 내 발행 1건 · 예정 1건 · 확인 필요 1건',a)
        self.assertIn('확인 필요: 발행 확인 1건',a)
        self.assertIn('그 외: 커뮤니티 1건',a)
        self.assertIn('최신 반영: 2026년 9월 11일 20:00',a)
        self.assertIn('예정 1건은 등록 기준 · 취소 상태 미반영',a)
        self.assertIn('9/14 18:00 예정 영상 · SF · 예정',a)
        self.assertIn('날짜만 등록된 빈 구좌 2건 별도',a)
        for internal in ('편성 원천','발행 연결','미매칭','시트 조회','공개 업로드','DB 미연결','출처:'):
            self.assertNotIn(internal,a)
    def test_schedule_count_mismatch_fails_closed(self):
        schedule=self.schedule();schedule['coverage']['source_count']=5
        self.p['youtube']['schedules']['2026-09']=schedule
        a=self.answer('9월 유튜브 편성','youtube')
        self.assertIn('합계가 맞지 않아 답변을 중단',a)
        self.assertNotIn('총 5건',a)
    def test_schedule_detail_identity_mismatch_fails_closed(self):
        schedule=self.schedule();schedule['extras'][0]['key']='f'*24
        self.p['youtube']['schedules']['2026-09']=schedule
        self.assertIn('상세와 전체 합계가 맞지 않아',self.answer('9월 유튜브 편성','youtube'))
    def test_stale_schedule_fails_closed(self):
        schedule=self.schedule();schedule['coverage']['captured_at']=(NOW-dt.timedelta(days=3)).isoformat()
        self.p['youtube']['schedules']['2026-09']=schedule
        self.assertIn('최신 반영 시각을 확인하지 못했습니다',self.answer('9월 유튜브 편성','youtube'))
    def test_schedule_published_id_mismatch_fails_closed(self):
        schedule=self.schedule()
        schedule['by_video']={'zzzzzzzzzzz':dict(schedule['by_video']['abcdefghijk'],item_id='zzzzzzzzzzz')}
        self.p['youtube']['schedules']['2026-09']=schedule
        a=self.answer('9월 유튜브 편성','youtube')
        self.assertIn('발행 영상 연결을 확인하지 못했습니다',a)
        self.assertNotIn('· 편성 내 발행 1건',a)
    def test_schedule_staleness_is_measured_from_answer_time(self):
        self.p['youtube']['schedules']['2026-09']=self.schedule()
        a=client.answer(self.p,'9월 유튜브 편성','youtube',NOW.date(),now=NOW+dt.timedelta(hours=49))
        self.assertIn('최신 반영 시각을 확인하지 못했습니다',a)
    def test_schedule_capture_is_rendered_in_kst(self):
        schedule=self.schedule();schedule['coverage']['captured_at']='2026-09-11T11:00:00+00:00'
        self.p['youtube']['schedules']['2026-09']=schedule
        self.assertIn('최신 반영: 2026년 9월 11일 20:00',self.answer('9월 유튜브 편성','youtube'))
    def test_schedule_details_include_upcoming_and_recent(self):
        schedule=self.schedule()
        schedule['extras']=[dict(key=str(i)*24,date=f'2026-09-{12+i:02d}',time='18:00',form='SF',
                                      ip='',title=f'예정 {i}',item_id='',community=False,state='planned',actual_date=None)
                            for i in range(1,6)]
        lines=client.schedule_detail_lines(schedule,self.p['youtube']['content'],NOW)
        self.assertEqual(len(lines),5)
        self.assertTrue(any('· 발행' in line for line in lines))
        self.assertTrue(any('예정 1' in line for line in lines))
        self.assertEqual(lines[-1].split()[1], '9/10')
    def test_future_neutral_schedule_does_not_displace_recent_rows(self):
        schedule=self.schedule()
        schedule['extras'][2].update(date='2026-09-30')
        lines=client.schedule_detail_lines(schedule,self.p['youtube']['content'],NOW)
        self.assertTrue(any('9/30' in line and '커뮤니티' in line for line in lines))
        self.assertTrue(any('9/10' in line and '· 발행' in line for line in lines))
    def test_same_day_future_schedule_is_not_action_required(self):
        schedule=self.schedule()
        schedule['extras'][1].update(date='2026-09-11',time='21:00')
        self.p['youtube']['schedules']['2026-09']=schedule
        a=self.answer('9월 유튜브 편성','youtube')
        self.assertIn('예정 2건 · 확인 필요 0건',a)
        self.assertIn('9/11 21:00 확인 영상 · SF · 예정',a)
        self.assertNotIn('확인 필요:',a)
    def test_same_day_unknown_time_is_neutral(self):
        schedule=self.schedule()
        schedule['extras'][1].update(date='2026-09-11',time='')
        self.p['youtube']['schedules']['2026-09']=schedule
        a=self.answer('9월 유튜브 편성','youtube')
        self.assertIn('확인 필요 0건',a)
        self.assertIn('시간 미정인 오늘 편성 1건',a)
    def test_schedule_bad_metadata_fails_closed_without_python_error(self):
        schedule=self.schedule();schedule['coverage']['source_sha256']=None
        self.p['youtube']['schedules']['2026-09']=schedule
        a=self.answer('9월 유튜브 편성','youtube')
        self.assertIn('원본 대조를 확인하지 못했습니다',a)
        self.assertNotIn('NoneType',a)
    def test_schedule_unhashable_source_key_fails_closed_without_python_error(self):
        schedule=self.schedule();schedule['coverage']['source_keys'][0]=[]
        self.p['youtube']['schedules']['2026-09']=schedule
        a=self.answer('9월 유튜브 편성','youtube')
        self.assertIn('원본 대조를 확인하지 못했습니다',a)
        self.assertNotIn('unhashable',a)
    def test_schedule_bad_date_fails_closed_without_python_error(self):
        schedule=self.schedule();schedule['extras'][0]['date']='not-a-date'
        self.p['youtube']['schedules']['2026-09']=schedule
        a=self.answer('9월 유튜브 편성','youtube')
        self.assertIn('날짜 연결을 확인하지 못했습니다',a)
        self.assertNotIn('Invalid isoformat',a)
    def test_published_outside_schedule_is_not_double_counted_as_review(self):
        schedule=self.schedule()
        self.p['youtube']['content'].append(dict(publish_date='2026-09-11',video_id='lmnopqrstuv',form='SF',
                                                  title='편성표 밖 영상',d7_views=None,d7_complete=False,views_total=10))
        schedule['coverage'].update(published_unmatched_count=1,published_count=2,rendered_count=5)
        self.p['youtube']['schedules']['2026-09']=schedule
        a=self.answer('9월 유튜브 편성','youtube')
        self.assertIn('확인 필요 1건',a)
        self.assertIn('편성표 밖 발행 1건',a)
        self.assertEqual(a.count('확인 필요'),2)  # summary + review-detail label only
    def test_unit_tokens_not_stripped(self):
        self.assertIn('1D 거래액',self.answer('9월 라이브 1D 거래액'))
        self.assertIn('D+7 완료',self.answer('9월 유튜브 D7 성과','youtube'))
    def test_live_answer_starts_with_decision_summary(self):
        lines=self.answer('9월 라이브 성과').splitlines()
        self.assertTrue(lines[1].startswith('• *1D 거래액*:'))
        self.assertIn('성과 1/2건',lines[1])
        self.assertIn('귀속 확인 필요',self.answer('9월 라이브 성과'))
    def test_ads_answer_starts_with_decision_summary(self):
        lines=self.answer('9월 10일 일반광고와 통광마 매출','ads').splitlines()
        self.assertEqual(lines[1],'• *기간 누적*: 0.00억 (50원)')
    def test_youtube_answer_starts_with_decision_summary(self):
        lines=self.answer('9월 유튜브 성과','youtube').splitlines()
        self.assertEqual(lines[1],'• 요약: 조회수 100회(2026-09-10까지) · 발행 1건 · D+7 완료 0/1건')
    def test_youtube_month_answer_stays_compact(self):
        a=self.answer('9월 유튜브 성과','youtube')
        self.assertLessEqual(len(a.splitlines()),8)
        self.assertNotIn('플랫폼 포맷:',a)
        self.assertNotIn('기간 내 발행 기여',a)
        self.assertNotIn('발행 cohort',a)
        self.assertNotIn('공식 기간 조회',a)
        self.assertNotIn('공식 일별 조회',a)
    def test_live_and_ads_answers_stay_compact_and_user_facing(self):
        live=self.answer('9월 라이브 성과')
        self.p['revenue']['months']['2026-09']={
            'closed':False,
            'forecast':{'ad_gen':100,'ad_int':200,'live':300,'total_won':600,'previous_total_won':500},
            'raw':{'ad_gen_won':10,'ad_int_won':20,'live_won':30,'as_of':'2026-09-11','target_won':1000},
        }
        ads=self.answer('9월 통광마 매출','ads')
        self.assertLessEqual(len(live.splitlines()),6)
        self.assertLessEqual(len(ads.splitlines()),3)
        self.assertIn('*현황 누적*',ads)
        for internal in ('RAW', 'revenue.v_', 'integrated_ssot', '대시보드 동일'):
            self.assertNotIn(internal,live)
            self.assertNotIn(internal,ads)


if __name__=='__main__':unittest.main()
