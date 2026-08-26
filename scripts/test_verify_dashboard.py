# [2026-08-07] DOM-first LIVE 아티팩트 릴리스 가드 회귀 테스트.
#   기본은 repo index.html(배포 후 = 승인 골격). 로컬/CI 검증 시 MBD_DASHBOARD_HTML 로
#   /tmp LIVE 후보를 주입해 결정론적으로 돌린다(CI 기본은 약화하지 않음).
#   now 는 manifest built_at 에서 파생 → 배포일자에 관계없이 freshness 판정이 안정적.
import datetime as dt
import html as html_mod
import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_dashboard as vd  # noqa: E402
import smoke_dashboard as sd  # noqa: E402


def _load_dashboard_html():
    override = os.environ.get("MBD_DASHBOARD_HTML")
    path = Path(override) if override else Path(__file__).resolve().parents[1] / "index.html"
    return path.read_text(encoding="utf-8"), str(path)


class DashboardGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html, cls.path = _load_dashboard_html()
        contract_path = Path(__file__).resolve().parents[1] / "data" / "owned_youtube_window_contract.json"
        cls.owned_youtube_contract = json.loads(contract_path.read_text(encoding="utf-8"))
        # manifest built_at 기준으로 fresh/stale now 파생 (아티팩트 실제 날짜와 무관하게 안정)
        _, manifest = vd.extract_manifest(cls.html)
        built = dt.datetime.fromisoformat(manifest["built_at_kst"])
        cls.now = built + dt.timedelta(hours=1)       # 모든 스탬프 48h 이내
        cls.stale_now = built + dt.timedelta(hours=100)  # 48h SLA 초과

    # ── 승인 골격 GREEN ─────────────────────────────────────────────
    def test_live_candidate_is_clean(self):
        self.assertEqual(vd.verify(self.html, self.now), [],
                         f"base artifact ({self.path}) must be a clean LIVE build")

    def test_live_candidate_passes_require_fresh(self):
        self.assertEqual(vd.verify(self.html, self.now, require_fresh=True), [])

    def test_scope_excludes_ogam(self):
        self.assertNotIn("ogam", vd.ALLOWED_REVENUE_TEAMS)
        self.assertEqual(vd.MANIFEST_SCOPE, ["ad_gen", "ad_int", "live"])

    def test_public_html_contains_approved_sanitized_weekly_content(self):
        # [2026-08-08] 승인 지표와 allowlisted player 링크만 공개하고 내부 회고/raw ID는 차단한다.
        self.assertNotIn('class="livetbl"', self.html)
        self.assertNotIn("시트 인사이트 전문", self.html)
        self.assertNotIn("콘텐츠별 성과", self.html)
        self.assertNotIn('"review_full"', self.html)
        self.assertNotIn('"live_id"', self.html)
        self.assertEqual(self.html.count('data-content-ledger="live"'), 12)
        self.assertEqual(self.html.count('data-content-ledger="youtube"'), 12)
        for marker in ("월간 보고 흐름", "MONTHLY BRIEFING", "data-report-flow",
                       'class="report-block', "data-note-edit", "monthly-brief-note",
                       "REPORT_DATA", "localStorage"):
            self.assertNotIn(marker, self.html)
        # [2026-08-09] 팝업/표와 중복되는 설명·provenance 장문은 공개 본문에 두지 않는다.
        for marker in ('<div class="cause"><b>유입 구성</b>',
                       '<div class="cause" data-live-revenue-breakdown=',
                       "direct Sheet CURRENT",
                       "목표 SoT: H2 고정 1억원 · OKR row는 참고",
                       "집계 6/6건 · 총 421,752",
                       "목표 SoT: OKR 14행 (H2)",
                       "마감예상액 · 월전체",
                       "확정 총액 · 월전체",
                       "부킹 총액 · 월전체",
                       "콘텐츠 링크 · 시청자수 / 1D 거래액 / 3H 거래액",
                       "콘텐츠 링크 · 누적조회수 / D7 조회수 / PIS",
                       "단위 억원 · DuckDB 미러",
                       "호버하면 PGM/프로모션 하위 금액",
                       "(첨부)"):
            self.assertNotIn(marker, self.html)
        # [2026-08-09] 영업 구조 KPI가 공개 DOM에도 유지되는지 검증한다.
        self.assertIn('data-yt-main-average="8"', self.html)
        self.assertIn('전체 평균 조회수', self.html)
        self.assertIn('data-yt-subscriber-card="8"', self.html)
        self.assertIn('data-yt-publish-card="8"', self.html)
        self.assertIn('data-yt-watch-duration-card="8"', self.html)
        self.assertIn('SF 5건 · LF 2건', self.html)
        self.assertIn('평균 시청지속시간', self.html)
        self.assertIn('2:50', self.html)
        self.assertIn('08-09 기준 · 조회수 1,126,735', self.html)
        self.assertIn('data-live-quality-mom-main="8"', self.html)
        self.assertIn('data-live-quality-mom="8-overall"', self.html)
        self.assertIn('data-live-quality-mom="8-signature"', self.html)
        self.assertIn('data-live-quality-mom="8-smart"', self.html)
        self.assertIn('data-live-quality-mom="8-essential"', self.html)
        self.assertIn('8월 목표 1.00억 대비', self.html)
        self.assertNotIn('6,246만', self.html)
        self.assertNotIn('8월 목표 1.00억 대비 62.5%', self.html)
        self.assertIn('data-yt-quality-mom-main="8"', self.html)
        self.assertIn('MoM ▼ 36.8%', self.html)
        self.assertIn('MoM ▲ 0.3%', self.html)
        self.assertIn('MoM ▲ 33.9%', self.html)
        self.assertIn('MoM ▼ 79.2%', self.html)
        self.assertIn('data-quality-trend="live-8"', self.html)
        self.assertIn('data-quality-trend="youtube-8"', self.html)
        self.assertIn('data-quality-trend-kind="live-package-average"', self.html)
        self.assertIn('data-quality-trend-kind="yt-format-average"', self.html)
        self.assertIn('평균치는 누적하지 않음', self.html)
        self.assertIn('square bar=패키지별 평균', self.html)
        self.assertIn('square bar=LF/SF 평균', self.html)
        self.assertIn('검은선=전체 평균', self.html)
        self.assertIn('.qt-bar{rx:0;shape-rendering:crispEdges}', self.html)
        self.assertEqual(self.html.count('data-quality-trend="live-'), 12)
        self.assertEqual(self.html.count('data-quality-trend="youtube-'), 12)
        self.assertNotIn('누적 높이=전체 평균', self.html)
        self.assertNotIn('평균 기여분(패키지 총', self.html)
        self.assertNotIn('YouTube Analytics 원천 미적재', self.html)
        self.assertNotIn('data-yt-channel-overview=', self.html)
        self.assertIn('data-adgen-booking-rate=', self.html)
        self.assertIn('비취소 구좌 부킹률', self.html)
        self.assertIn('비취소 부킹구좌수 ÷ 수용가능 구좌수', self.html)
        self.assertIn('835건 / 수용 1,952구좌', self.html)
        self.assertIn('42.8%', self.html)
        self.assertNotIn('부킹건수 목표', self.html)
        self.assertIn('data-live-revenue-breakdown=', self.html)
        self.assertIn('data-live-package-mom=', self.html)
        self.assertIn('MoM +33.3%', self.html)
        self.assertIn('패키지 총액 = AF 패키지비', self.html)
        self.assertIn('진행건수 = 확정 편성건', self.html)
        self.assertEqual(self.html.count('data-live-progress-count='), 9)
        self.assertEqual(self.html.count('data-live-package-count='), 27)
        self.assertIn('진행 36건', self.html)
        self.assertIn('전월 +6건', self.html)
        self.assertIn('시그니처&lt;small class=&quot;up&quot;&gt;8건 · +2건', self.html)
        self.assertIn('스마트&lt;small class=&quot;up&quot;&gt;13건 · +4건', self.html)
        self.assertEqual(self.html.count('시그니처 하위'), 8)
        self.assertEqual(self.html.count('에센셜 하위'), 8)
        self.assertEqual(self.html.count('스마트 하위'), 8)
        self.assertIn('PGM · 12주년', self.html)
        self.assertIn('PGM · 어서오!세일', self.html)
        self.assertNotIn('하위 구분 = PGM/프로모션', self.html)
        # [2026-08-09] 9월 신청 시트 30건, 패키지비 1.74억의 미래월 부킹 반영.
        self.assertIn('라이브 · 9월 패키지별 부킹', self.html)
        self.assertIn('8.03억', self.html)
        self.assertIn('목표 12.8억 대비 채움 62.9%', self.html)
        self.assertIn('data-achievement-ring="채움" style="--p:82.1"', self.html)
        self.assertIn("data-week-toggle=", self.html)
        self.assertIn('data-content-link="live"', self.html)
        self.assertIn('data-content-link="youtube"', self.html)
        for label in ("시청자수", "1D 거래액", "3H 거래액", "누적조회수", "D7 조회수", "PIS"):
            self.assertIn(label, self.html)
        self.assertIn('class="activity-column-head"', self.html)
        self.assertIn('class="activity-metric-head metric-trio"', self.html)
        self.assertIn('class="activity-date"', self.html)
        self.assertIn('class="activity-main activity-main-inline"', self.html)
        self.assertIn('class="activity-inline-meta"', self.html)
        self.assertIn('min-height:42px', self.html)
        self.assertIn('.team{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);padding:20px 22px}', self.html)
        self.assertIn('.team .hd2{display:grid;grid-template-columns:minmax(0,1fr) 108px;gap:14px;align-items:center;min-height:108px}', self.html)
        self.assertIn('.team .team-main{min-width:0;display:flex;flex-direction:column;justify-content:center;gap:14px}', self.html)
        self.assertIn('.team .achv{--p:0;--achv-ring:var(--violet);--achv-track:#E8EDF3;position:relative;justify-self:end;width:108px;height:108px', self.html)
        self.assertEqual(self.html.count('data-achievement-ring='), 34)
        self.assertIn('data-achievement-ring="달성"', self.html)
        self.assertIn('data-achievement-ring="채움"', self.html)
        self.assertIn('<div class="team-main"><span class="nm">일반광고</span><div class="bigv num">8.68억</div></div><span class="achv up num" data-achievement-ring="달성" style="--p:100" role="img" aria-label="달성률 100.3%"><span class="achv-in"><b>100.3%</b><small>달성률</small>', self.html)
        self.assertIn('<div class="team-main"><span class="nm">통광마</span><div class="bigv num">2,909만</div></div><span class="achv dn num" data-achievement-ring="달성" style="--p:14.5" role="img" aria-label="달성률 14.5%"><span class="achv-in"><b>14.5%</b><small>달성률</small>', self.html)
        self.assertNotIn('<span class="pill up num">달성 ', self.html)
        self.assertNotIn('<span class="pill dn num">달성 ', self.html)
        self.assertNotIn('<span class="pill flat num">달성 ', self.html)
        self.assertNotIn('<span class="pill flat num">채움 ', self.html)
        self.assertNotIn('padding:20px 110px 20px 22px', self.html)
        self.assertNotIn('.team .hd2{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;min-height:58px}', self.html)
        self.assertNotIn('.team .hd2{position:relative;display:block;min-height:84px;padding-right:98px}', self.html)
        self.assertNotIn('</div>\n          <div class="bigv num">', self.html)
        self.assertNotIn('position:relative;flex:0 0 58px;width:58px;height:58px', self.html)
        self.assertNotIn('width:84px;height:84px', self.html)
        self.assertNotIn('width:76px;height:76px', self.html)
        self.assertNotIn('.team .achv{--p:0;--achv-ring:#F59E0B', self.html)
        self.assertNotIn('<small>월간 기준</small>', self.html)
        self.assertIn('.team .rows .r:last-child{order:-1', self.html)
        self.assertIn('.pill.up{background:var(--red-soft);color:var(--red)', self.html)
        self.assertIn('.pill.dn{background:var(--blue-soft);color:var(--blue)', self.html)
        self.assertIn('.g .glab.neg{color:var(--blue)} .g .glab.pos{color:var(--red)', self.html)
        self.assertNotIn('.pill.up{background:var(--green-soft)', self.html)
        self.assertNotIn('.pill.dn{background:var(--red-soft)', self.html)
        self.assertNotIn('&lt;small&gt;MoM +', self.html)
        self.assertNotIn('&lt;small&gt;MoM -', self.html)
        self.assertNotIn('아래 빨강/초록 = 목표 대비 억원', self.html)
        self.assertIn('아래 빨강/파랑 = 목표 대비 억원', self.html)
        self.assertNotIn('class="week-counts"', self.html)
        self.assertNotIn('class="activity-state', self.html)
        self.assertNotIn('data-content-status=', self.html)
        _, manifest = vd.extract_manifest(self.html)
        self.assertEqual(manifest.get("schema"), "mbd-public-guard-v3")
        self.assertTrue(manifest.get("sanitized_rows_included"))
        self.assertEqual(manifest.get("public_detail_fields"), vd.PUBLIC_DETAIL_FIELDS)

    def test_left_live_nav_opens_dedicated_live_window_contract(self):
        for marker in ('data-live-launch', 'aria-controls="liveWindow"',
                       'id="liveWindow"', 'data-live-window="weekly-performance"',
                       'data-live-period="mtd"', 'data-live-window-latest-date=',
                       '라이브 성과 상세탭', '디폴트 금월 누적', '주차별 보기',
                       '거래액 기준 분리', '데이터 사용 룰',
                       '카드 거래액=1D 브랜드 일거래액', '월/주간 효율=방송별 데이터 GMV',
                       '방송별 카드 거래액=`일 전체 GMV (라이브 브랜드 전체)`',
                       '금월 누적 성과가 디폴트', '미집계/0원 편성은 누적 성과에서 제외',
                       '월 누적과 주간을 분리', 'data-live-week-group=',
                       'data-live-week-summary=', 'data-live-weekly-view=',
                       'function setLiveWindow(open)', '주차별 보기 · 방송별 성과',
                       'RAW 수치 readback 전용', '가로 풀폭',
                       '.live-window{position:fixed;inset:16px;',
                       '@media (max-width:1180px){.live-window{inset:14px'):
            self.assertIn(marker, self.html)
        self.assertGreaterEqual(self.html.count('data-live-broadcast-card='), 1)
        self.assertNotIn('data-live-weekly-analysis=', self.html)
        self.assertNotIn('원본 rows 302–308', self.html)
        self.assertNotIn('공식 회고 전문', self.html)
        self.assertNotIn('aria-label="8월 1주차 라이브 성과 요약"', self.html)
        self.assertNotIn('총액은 회복됐지만,<br>방송당 효율 회복으로 보긴 어려움', self.html)
        self.assertNotIn('data-live-broadcast-card="frosch"', self.html)
        self.assertNotIn('data-live-broadcast-card="cuchen"', self.html)
        self.assertNotIn('inset:22px 28px 22px 278px', self.html)
        self.assertNotIn('inset:16px 18px 16px 238px', self.html)
        self.assertNotIn('<div class="live-pulse-row"><b>시그니처 GMV</b>', self.html)
        self.assertNotIn('<div class="live-pulse-row"><b>신제품 gate</b>', self.html)

    def test_left_youtube_nav_opens_dedicated_weekly_window_contract(self):
        contract = self.owned_youtube_contract
        source = contract["source"]
        hero = {item["label"]: item for item in contract["hero_kpis"]}
        latest = source["monthly_period"].split("~", 1)[1]
        for marker in ('data-yt-launch', 'aria-controls="youtubeWindow"',
                       'id="youtubeWindow"', 'data-yt-window="weekly-detail"',
                       'data-yt-period="mtd"', 'data-owned-media-window="youtube"',
                       f'data-yt-window-latest-date="{latest}"', '온드미디어 상세탭',
                       '디폴트 금월 누적', '주차별 보기', 'YouTube Analytics 기준',
                       '8월 금월 누적', hero['조회수']['em'],
                       f"YouTube 조회수 {hero['조회수']['value']}",
                       f"발행</small><b>{hero['발행']['value']}</b><em>{hero['발행']['em']}",
                       '당월/당주 발행 기여', '기발행 기여',
                       'data-yt-week-summary="8"', 'data-yt-weekly-view="8"',
                       '콘텐츠 D+N 참고', 'public D+N snapshot',
                       'function setYoutubeWindow(open)', 'data-yt-close',
                       '.yt-window{position:fixed;inset:16px;',
                       '@media (max-width:1180px){.yt-window{inset:14px'):
            self.assertIn(marker, self.html)
        for marker in contract["required_copy"]:
            self.assertIn(marker, self.html)
        self.assertGreaterEqual(self.html.count('data-yt-weekly-card='), 1)
        for stale in ('유튜브 주간 리포팅 창', '좌측 유튜브 탭 전용 UI',
                      'LF 비포애프터가 주간 성장 대부분', '동일 D+N × LF/SF × IP',
                      'public snapshot 기준 2026-08-11 23:57 KST',
                      'data-yt-weekly-card="beforeafter-lf-ep91"',
                      'data-yt-weekly-card="nationhome-lf-ep9"',
                      '보조 기여는 있으나 LF 히트로 보긴 어려움',
                      'SF 전국내집자랑 -123.0K', '혼수의기술 기발행 쇼츠 재상승'):
            self.assertNotIn(stale, self.html)
        self.assertNotIn('data-yt-weekly-analysis-raw=', self.html)
        self.assertNotIn('fact_public_dplusn_', self.html)
        self.assertNotIn('v_public_dplusn_', self.html)

    def test_youtube_window_marker_removal_fails(self):
        bad = self.html.replace('data-yt-window="weekly-detail"', 'data-yt-window="removed"', 1)
        errors = vd.verify(bad, self.now, require_fresh=False)
        self.assertTrue(any("missing youtube window marker" in error for error in errors), errors)

    def test_inline_youtube_weekly_analysis_reappearance_fails(self):
        bad = self.html.replace('<div class="content-ledger" data-content-ledger="youtube">',
                                '<div class="content-ledger" data-content-ledger="youtube"><div data-yt-weekly-analysis-raw="8-1">youtube_views.duckdb fact_public_dplusn_video</div>', 1)
        errors = vd.verify(bad, self.now, require_fresh=False)
        self.assertTrue(any("obsolete inline/raw youtube analysis marker" in error for error in errors), errors)

    def test_live_window_marker_removal_fails(self):
        bad = self.html.replace('data-live-window="weekly-performance"', 'data-live-window="removed"', 1)
        errors = vd.verify(bad, self.now, require_fresh=False)
        self.assertTrue(any("missing live window marker" in error for error in errors), errors)

    def test_inline_live_weekly_analysis_reappearance_fails(self):
        bad = self.html.replace('<div class="content-ledger" data-content-ledger="live">',
                                '<div class="content-ledger" data-content-ledger="live"><div data-live-weekly-analysis="8-1">원본 rows 302–308</div>', 1)
        errors = vd.verify(bad, self.now, require_fresh=False)
        self.assertTrue(any("obsolete inline/raw live analysis marker" in error for error in errors), errors)

    def test_monthly_flow_tooltips_include_team_mom_with_red_up_blue_down(self):
        gauge_tips = {
            int(month): html_mod.unescape(tip)
            for month, tip in re.findall(r'<div class="g [^"]*" data-m="(\d+)" data-tip="([^"]+)"', self.html)
        }
        self.assertEqual(len(gauge_tips), 12)
        august = gauge_tips[8]
        self.assertIn('<span>일반광고</span><b><span class="tv"><span>8.68억</span><small class="up">MoM +7.9%</small>', august)
        self.assertIn('<span>통광마</span><b><span class="tv"><span>2,909만</span><small class="dn">MoM -46.1%</small>', august)
        self.assertIn('<span>라이브</span><b><span class="tv"><span>2.18억</span><small class="up">MoM +17.8%</small>', august)
        september = gauge_tips[9]
        self.assertIn('<small class="dn">MoM -32.5%</small>', september)
        self.assertIn('<small class="up">MoM +49.0%</small>', september)
        self.assertIn('<small class="dn">MoM -20.2%</small>', september)

    def test_public_guard_rejects_non_allowlisted_content_link(self):
        bad = self.html.replace("https://www.youtube.com/watch?v=", "https://evil.example/watch?v=", 1)
        errors = vd.verify(bad, self.now, require_fresh=False)
        self.assertTrue(any("invalid youtube content URL" in error for error in errors), errors)

    def test_public_guard_rejects_obsolete_per_row_status(self):
        bad = self.html.replace('class="activity-row"', 'class="activity-row" data-content-status="완료"', 1)
        errors = vd.verify(bad, self.now, require_fresh=False)
        self.assertTrue(any("obsolete weekly marker" in error for error in errors), errors)

    def test_youtube_main_average_marker_removal_fails(self):
        bad = self.html.replace('data-yt-main-average=', 'data-yt-main-removed=')
        errors = vd.verify(bad, self.now, require_fresh=False)
        self.assertTrue(any("missing youtube quality-card marker" in error for error in errors), errors)

    def test_quality_card_mom_marker_removal_fails(self):
        bad = self.html.replace('data-live-quality-mom-main="8"', 'data-live-quality-mom-main-removed="8"', 1)
        errors = vd.verify(bad, self.now, require_fresh=False)
        self.assertTrue(any("missing quality-card MoM marker" in error for error in errors), errors)

    def test_sales_structure_marker_removal_fails(self):
        bad = self.html.replace('data-adgen-booking-rate=', 'data-booking-rate-removed=')
        errors = vd.verify(bad, self.now, require_fresh=False)
        self.assertTrue(any("missing sales-structure marker" in error for error in errors), errors)

    def test_live_package_mom_marker_removal_fails(self):
        bad = self.html.replace('data-live-package-mom=', 'data-live-package-mom-removed=')
        errors = vd.verify(bad, self.now, require_fresh=False)
        self.assertTrue(any("missing sales-structure marker" in error for error in errors), errors)

    def test_live_package_count_marker_removal_fails(self):
        bad = self.html.replace('data-live-progress-count=', 'data-live-progress-count-removed=')
        errors = vd.verify(bad, self.now, require_fresh=False)
        self.assertTrue(any("missing sales-structure marker" in error for error in errors), errors)

    def test_live_sub_promotion_marker_removal_fails(self):
        bad = self.html.replace('시그니처 하위', '시그니처_하위_제거', 1)
        errors = vd.verify(bad, self.now, require_fresh=False)
        self.assertTrue(any("live sub-promotion marker" in error for error in errors), errors)

    def test_redundant_monthly_report_flow_reappearance_fails(self):
        bad = self.html.replace('<main class="main">', '<main class="main"><section data-report-flow>월간 보고 흐름</section>', 1)
        errors = vd.verify(bad, self.now, require_fresh=False)
        self.assertTrue(any("redundant monthly report flow" in error for error in errors), errors)

    def test_redundant_dashboard_copy_reappearance_fails(self):
        bad = self.html.replace('<main class="main">', '<main class="main"><div class="cause"><b>유입 구성</b> · 중복</div>', 1)
        errors = vd.verify(bad, self.now, require_fresh=False)
        self.assertTrue(any("redundant dashboard copy" in error for error in errors), errors)

    def test_manifest_live_average_target_is_fixed_to_one_eok(self):
        _, manifest = vd.extract_manifest(self.html)
        self.assertEqual(manifest.get("live_avg_gmv_target_won"), 100_000_000)

    def test_live_average_uses_1d_brand_daily_gmv(self):
        _, manifest = vd.extract_manifest(self.html)
        self.assertEqual(manifest.get("live_gmv_basis"), "1D")
        self.assertIn("라이브 1D 평균거래액 · 품질", self.html)
        self.assertIn("1D 평균 거래액", self.html)
        self.assertRegex(self.html, r"8월 목표 1\.00억 대비 [0-9.]+%")
        self.assertNotIn("방송 평균 거래액", self.html)

    # [2026-08-07] 일반광고 hover는 보이는 KPI 반복이 아니라 3유형 금액+MoM이어야 한다.
    def test_all_months_have_three_way_adgen_mix_tooltips(self):
        attrs = re.findall(r'<div class="team" data-tip="([^"]+)"', self.html)
        adgen_tips = [html_mod.unescape(value) for value in attrs
                      if "일반광고 ·" in html_mod.unescape(value)]
        self.assertEqual(len(adgen_tips), 12)
        for tip_html in adgen_tips:
            self.assertEqual(tip_html.count('class="tr"'), 3)
            for bucket in ("유상", "무상", "정부지원"):
                self.assertIn(bucket, tip_html)
            self.assertEqual(tip_html.count("MoM "), 3)
            for duplicate in ("월 목표", "GAP", "달성률"):
                self.assertNotIn(duplicate, tip_html)

    def test_government_tooltips_use_canonical_project_subrows(self):
        attrs = re.findall(r'<div class="team" data-tip="([^"]+)"', self.html)
        tips = [html_mod.unescape(value) for value in attrs
                if "일반광고 ·" in html_mod.unescape(value)]
        july = next(tip for tip in tips if "일반광고 · 7월" in tip)
        september = next(tip for tip in tips if "일반광고 · 9월" in tip)
        self.assertIn('class="gsubs"', july)
        self.assertIn("TOPS", july)
        self.assertIn("기타 정부지원", july)
        self.assertIn('class="gsubs"', september)
        self.assertIn("경기도주식회사", september)
        self.assertIn("기타 정부지원", september)
        for raw_comment in ("정부지원사업 TOPS", "경기도 주식회사"):
            self.assertNotIn(raw_comment, "".join(tips))

    def test_adint_tooltips_have_three_buckets_and_nested_items(self):
        attrs = re.findall(r'<div class="team" data-tip="([^"]+)"', self.html)
        tips = [html_mod.unescape(value) for value in attrs
                if "통광마 ·" in html_mod.unescape(value)]
        self.assertEqual(len(tips), 12)
        for tip_html in tips:
            self.assertEqual(tip_html.count('class="tr"'), 3)
            self.assertEqual(tip_html.count("MoM "), 3)
            for bucket in ("유상", "무상", "정부지원"):
                self.assertIn(bucket, tip_html)
            for duplicate in ("월 목표", "GAP", "달성률"):
                self.assertNotIn(duplicate, tip_html)
        july = next(tip for tip in tips if "통광마 · 7월" in tip)
        august = next(tip for tip in tips if "통광마 · 8월" in tip)
        september = next(tip for tip in tips if "통광마 · 9월" in tip)
        self.assertIn("마틸라 · 컴팩트PKG", july)
        self.assertIn("오늘의집 layer", july)
        self.assertIn("경기도주식회사", july)
        self.assertIn("샤크닌자 · 미디어PKG", august)
        self.assertIn("익산원예농협", august)
        self.assertIn("헬로우슬립 · 컴팩트PKG", september)
        self.assertIn("마틸라 · 컴팩트PKG", september)

    def test_pages_workflow_uploads_index_only(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" /
                    "dashboard-guard.yml").read_text(encoding="utf-8")
        self.assertIn("path: _site", workflow)
        self.assertIn("cp index.html _site/index.html", workflow)
        self.assertNotIn("path: .\n", workflow)

    # ── 구 payload 재출현 (negative control) ────────────────────────
    def test_legacy_top_summary_payload_reappearance_fails(self):
        bad = self.html.replace(
            "</body>", "<script>window.__TOP_SUMMARY_DATA__ = {}</script></body>", 1)
        self.assertTrue(any("legacy embedded payload" in e for e in vd.verify(bad, self.now)))

    def test_legacy_sot_payload_reappearance_fails(self):
        bad = self.html.replace(
            "</body>", "<script>window.__MBD_SOT_DATA__ = {}</script></body>", 1)
        self.assertTrue(any("legacy embedded payload" in e for e in vd.verify(bad, self.now)))

    # ── LIVE 마커 missing / duplicate / tampered ────────────────────
    def test_missing_live_marker_fails(self):
        bad = self.html.replace("LIVE 빌드", "", 1)
        self.assertTrue(any("LIVE marker" in e for e in vd.verify(bad, self.now)))

    def test_duplicate_live_marker_fails(self):
        bad = self.html.replace("LIVE 빌드", "LIVE 빌드 LIVE 빌드", 1)
        self.assertTrue(any("LIVE marker" in e and "2x" in e for e in vd.verify(bad, self.now)))

    def test_tampered_live_marker_reintroduces_pre_approval_fails(self):
        bad = self.html.replace("고정 URL 운영본", "승인 전 비공개", 1)
        errors = vd.verify(bad, self.now)
        self.assertTrue(any("pre-approval marker" in e for e in errors))
        self.assertTrue(any("LIVE marker" in e for e in errors))

    def test_staging_marker_reappearance_fails(self):
        bad = self.html.replace("LIVE 빌드", "STAGING 빌드", 1)
        self.assertTrue(any("pre-approval marker" in e for e in vd.verify(bad, self.now)))

    # ── 안전하지 않은 링크 (negative control) ───────────────────────
    def test_javascript_uri_fails(self):
        bad = self.html.replace("</body>", '<a href="javascript:alert(1)">x</a></body>', 1)
        self.assertTrue(any("javascript:" in e for e in vd.verify(bad, self.now)))

    def test_blank_link_without_noopener_fails(self):
        bad = self.html.replace(
            "</body>", '<a href="https://x.example" target="_blank">x</a></body>', 1)
        self.assertTrue(any("rel=noopener" in e for e in vd.verify(bad, self.now)))

    # ── 월 셀렉터 (negative control) ────────────────────────────────
    def test_missing_month_option_fails(self):
        bad = self.html.replace('<option value="8"', '<option value="99"', 1)
        self.assertTrue(any("1..12" in e for e in vd.verify(bad, self.now)))

    # ── manifest 변조 / raw key (negative control) ──────────────────
    def test_manifest_raw_key_injection_fails(self):
        raw, manifest = vd.extract_manifest(self.html)
        manifest["rows"] = [{"secret": "x"}]
        bad = self.html.replace(raw, json.dumps(manifest, ensure_ascii=False), 1)
        errors = vd.verify(bad, self.now)
        self.assertTrue(errors)
        self.assertTrue(any("unexpected keys" in e or "forbidden raw/credential" in e for e in errors))

    def test_manifest_schema_tamper_fails(self):
        raw, manifest = vd.extract_manifest(self.html)
        manifest["schema"] = "evil-v9"
        bad = self.html.replace(
            raw, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), 1)
        self.assertTrue(any("schema" in e for e in vd.verify(bad, self.now)))

    def test_manifest_non_current_source_status_fails(self):
        raw, manifest = vd.extract_manifest(self.html)
        manifest["source_status"]["live_quality"] = "unavailable"
        bad = self.html.replace(
            raw, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), 1)
        self.assertTrue(any("source_status" in e for e in vd.verify(bad, self.now)))

    def test_manifest_missing_source_timestamp_key_fails(self):
        raw, manifest = vd.extract_manifest(self.html)
        del manifest["source_snapshot_as_of"]["owned_media"]
        bad = self.html.replace(
            raw, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), 1)
        self.assertTrue(any("source_snapshot_as_of keys" in e for e in vd.verify(bad, self.now)))

    def test_manifest_default_month_out_of_range_fails(self):
        raw, manifest = vd.extract_manifest(self.html)
        manifest["default_month"] = 99
        bad = self.html.replace(
            raw, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), 1)
        self.assertTrue(any("default_month" in e for e in vd.verify(bad, self.now)))

    def test_manifest_payload_hash_format_fails(self):
        raw, manifest = vd.extract_manifest(self.html)
        manifest["source_payload_sha256"] = "not-a-sha"
        bad = self.html.replace(
            raw, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), 1)
        self.assertTrue(any("source_payload_sha256" in e for e in vd.verify(bad, self.now)))

    def test_manifest_naive_timestamp_fails(self):
        raw, manifest = vd.extract_manifest(self.html)
        manifest["built_at_kst"] = "2026-08-07T02:27:29"  # tz 제거
        bad = self.html.replace(
            raw, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), 1)
        self.assertTrue(any("timezone-aware" in e for e in vd.verify(bad, self.now)))

    def test_manifest_future_timestamp_beyond_clock_skew_fails(self):
        raw, manifest = vd.extract_manifest(self.html)
        manifest["built_at_kst"] = (self.now + dt.timedelta(hours=1)).isoformat()
        bad = self.html.replace(
            raw, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), 1)
        self.assertTrue(any("future" in e for e in vd.verify(bad, self.now, require_fresh=True)))

    def test_missing_manifest_fails(self):
        bad = vd.MANIFEST_RE.sub("", self.html, count=1)
        self.assertTrue(any("manifest" in e and "missing" in e for e in vd.verify(bad, self.now)))

    # ── freshness SLA (negative control) ────────────────────────────
    def test_stale_snapshot_only_fails_under_require_fresh(self):
        # require_fresh=False → 노후여도 통과(다른 위반 없음), True → stale RED
        self.assertEqual(vd.verify(self.html, self.stale_now, require_fresh=False), [])
        stale_errors = vd.verify(self.html, self.stale_now, require_fresh=True)
        self.assertTrue(any("stale snapshot" in e for e in stale_errors))


class SmokeViewportPolicyTest(unittest.TestCase):
    def _result(self, width):
        return {
            "doc": {"sw": width, "cw": width, "bsw": width, "iw": width},
            "errors": [],
            "lower": {
                "visibleRest": 1,
                "teamCards": 3,
                "qualityCards": 2,
                "rawRowTables": 0,
                "futureRootCount": 9,
                "futureForbiddenCount": 0,
            },
            "liveWindow": {
                "hasLaunch": True,
                "hasWindow": True,
                "beforeHidden": "true",
                "afterOpen": True,
                "ariaOpen": "false",
                "expanded": "true",
                "title": True,
                "basis": True,
                "rect": {"left": 10, "rightGap": 10, "width": width - 20, "viewport": width},
                "afterClose": False,
                "ariaClose": "true",
            },
            "youtubeWindow": {
                "hasLaunch": True,
                "hasWindow": True,
                "beforeHidden": "true",
                "afterOpen": True,
                "ariaOpen": "false",
                "expanded": "true",
                "title": True,
                "basis": True,
                "cardCount": 5,
                "liveClosed": True,
                "rect": {"left": 10, "rightGap": 10, "width": width - 20, "viewport": width},
                "afterClose": False,
                "ariaClose": "true",
            },
        }

    def test_mobile_css_viewport_must_match_requested_390(self):
        errors = sd._check_viewport(
            self._result(500), 390, 844, "mobile", switch_expected=None)
        self.assertTrue(any("requested width" in e for e in errors))

    def test_exact_mobile_width_with_lower_contract_is_accepted(self):
        errors = sd._check_viewport(
            self._result(390), 390, 844, "mobile", switch_expected=None)
        self.assertEqual(errors, [])

    def test_missing_lower_card_evidence_fails(self):
        result = self._result(390)
        result.pop("lower")
        errors = sd._check_viewport(result, 390, 844, "mobile", switch_expected=None)
        self.assertTrue(any("lower-card" in e for e in errors))

    def test_live_window_not_full_width_fails(self):
        result = self._result(1440)
        result["liveWindow"]["rect"] = {"left": 278, "rightGap": 28, "width": 1134, "viewport": 1440}
        errors = sd._check_viewport(result, 1440, 900, "desktop", switch_expected=None)
        self.assertTrue(any("not horizontally full width" in e for e in errors), errors)

    def test_future_forbidden_labels_fail(self):
        result = self._result(390)
        result["lower"]["futureForbiddenCount"] = 1
        errors = sd._check_viewport(result, 390, 844, "mobile", switch_expected=None)
        self.assertTrue(any("future" in e and "forbidden" in e for e in errors))

    def test_mobile_width_outside_responsive_breakpoint_fails(self):
        errors = sd._check_viewport(
            self._result(1000), 390, 844, "mobile", switch_expected=None)
        self.assertTrue(any("requested width" in e for e in errors))

    def test_desktop_css_viewport_must_match_requested_width(self):
        errors = sd._check_viewport(
            self._result(1200), 1440, 900, "desktop", switch_expected=None)
        self.assertTrue(any("requested width" in e for e in errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
