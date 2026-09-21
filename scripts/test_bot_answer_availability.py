"""Incident regressions exercise the actual validation+answer entry point."""
import datetime as dt
import unittest

from test_bot_metric_parity import client, fixture, NOW


class AnswerAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.p = fixture()
        self.p['youtube']['verified_api'] = dict(
            schema='youtube-verified-api-v1', channel_id='UCBKtitA1RwY7F32rCniV1dA',
            captured_at=NOW.isoformat(), coverage_start='2026-09-01', actual_end='2026-09-10',
            periods=[dict(period_type='month', period_start='2026-09-01', period_end='2026-09-30',
                          metric_end_date='2026-09-10', views=12345, formats={'shorts':2345,'videoOnDemand':10000})],
            daily=[dict(date=f'2026-09-{i:02d}', views=100) for i in range(1,11)],
            daily_formats=[], discovered=[])

    def ask(self, q, domain):
        return client.verified_answer(self.p, q, domain, NOW, b'html')

    def stale(self, key):
        self.p['source_as_of'][key] = (NOW-dt.timedelta(hours=49)).isoformat()

    def test_conversational_requests_preserve_numeric_answer(self):
        for domain, q in [('youtube','9월 유튜브 성과'), ('live','9월 라이브 성과'),
                          ('ads','9월 10일 광고 매출')]:
            expected = self.ask(q, domain)
            for suffix in ['핵심만 알려줘','간단히 알려줘','요약해줘','간결하게 보여주세요']:
                with self.subTest(domain=domain, suffix=suffix):
                    self.assertEqual(expected, self.ask(q+' '+suffix, domain))

    def test_unknown_filter_is_never_dropped_with_presentation_request(self):
        for domain, q in [('youtube','9월 삼성 유튜브 성과'), ('live','9월 삼성 라이브 성과'),
                          ('ads','9월 삼성 광고 매출'), ('youtube','9월 핵심브랜드 유튜브 성과')]:
            with self.subTest(q=q):
                self.assertIn('확인 필요', self.ask(q+' 핵심만 알려줘',domain))

    def test_weekly_runtime_scope_entry_accepts_summary_language(self):
        # The deployed weekly lane calls check_scope directly, before answer().
        client.check_scope('지난주 유튜브 성과 핵심만 알려줘', 'youtube')
        with self.assertRaises(ValueError):
            client.check_scope('지난주 삼성 유튜브 성과 핵심만 알려줘', 'youtube')

    def test_stale_youtube_does_not_block_live_or_ads(self):
        for key in ('yt_quality','owned_media'): self.stale(key)
        self.p['youtube']['verified_api']['captured_at'] = (NOW-dt.timedelta(days=3)).isoformat()
        self.assertIn('200원', self.ask('9월 라이브 성과 핵심만 알려줘','live'))
        self.assertIn('50원', self.ask('9월 10일 광고 매출 핵심만 알려줘','ads'))

    def test_stale_used_source_still_rejects(self):
        self.stale('live_quality')
        with self.assertRaises(ValueError): self.ask('9월 라이브 성과','live')
        self.stale('revenue_mirror')
        with self.assertRaises(ValueError): self.ask('9월 광고 매출','ads')

    def test_fresh_api_returns_partial_answer_without_stale_cohort(self):
        for key in ('yt_quality','owned_media'): self.stale(key)
        for q in ['9월 유튜브 성과 핵심만 알려줘','9월 유튜브 상세']:
            a = self.ask(q,'youtube')
            self.assertIn('12,345회',a)
            self.assertIn('2026-09-10',a)
            self.assertIn('발행·D7·구독자 지표는 갱신 지연',a)
            self.assertNotIn('발행 1건',a)
            self.assertNotIn('평균',a)
            self.assertLessEqual(len(a.splitlines()),5)

    def test_subscriber_request_does_not_return_unrelated_views(self):
        self.stale('owned_media')
        a=self.ask('9월 유튜브 구독자 수 핵심만 알려줘','youtube')
        self.assertIn('구독자 수는 데이터 갱신 지연',a)
        self.assertNotIn('조회수',a)

    def test_unknown_attribution_is_not_reported_as_zero_revenue(self):
        a=self.ask('9월 라이브 성과','live')
        self.assertIn('귀속 확인 중',a)
        self.assertNotIn('0원',a.split('*귀속 매출(확인분)*:')[1].splitlines()[0])

    def test_api_view_does_not_depend_on_stale_revenue(self):
        self.stale('revenue_mirror'); self.stale('live_quality')
        self.assertIn('12,345회', self.ask('9월 유튜브 성과','youtube'))

    def test_partial_api_coverage_is_not_presented_as_full_week(self):
        self.stale('yt_quality');self.stale('owned_media')
        a=self.ask('이번주 유튜브 성과 핵심만 알려줘','youtube')
        self.assertIn('400회',a)
        self.assertIn('2026-09-07~2026-09-10',a)
        self.assertIn('집계 대기',a)

    def test_api_gaps_are_not_zero_filled(self):
        self.stale('yt_quality');self.stale('owned_media')
        self.p['youtube']['verified_api']['daily'].pop()
        self.assertIn('누락·중복',self.ask('이번주 유튜브 성과','youtube'))

    def test_hash_and_clock_guards_remain_effective(self):
        for domain in ('live','youtube','ads'):
            with self.assertRaises(ValueError):
                client.verified_answer(self.p,'9월 성과',domain,NOW,b'wrong release')
        self.p['youtube']['verified_api']['channel_id']='wrong-channel'
        with self.assertRaises(ValueError):self.ask('9월 유튜브 성과','youtube')

    def test_stale_api_is_not_used(self):
        self.p['youtube']['verified_api']['captured_at']=(NOW-dt.timedelta(hours=49)).isoformat()
        with self.assertRaises(ValueError):self.ask('9월 유튜브 성과','youtube')


if __name__ == '__main__': unittest.main()
