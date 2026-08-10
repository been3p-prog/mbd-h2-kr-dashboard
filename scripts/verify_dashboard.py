#!/usr/bin/env python3
"""Deterministic release guard for the DOM-first MBD dashboard (LIVE artifact).

승인 골격(2026-08-07): compact public manifest(<script id="mbd-public-guard">) +
월 드롭다운(#msel 1..12) + 12개월 세로 기둥 차트. 구(舊) 임베드 payload
(window.__TOP_SUMMARY_DATA__ / window.__MBD_SOT_DATA__) 는 재출현 금지.
verify() 는 예외를 던지지 않고 정렬된 위반 리스트를 반환한다(fail-closed).
--require-fresh 는 스케줄 프로브에서 manifest 타임스탬프 48h SLA 를 강제한다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))
FRESHNESS_SLA_HOURS = 48
MAX_FUTURE_SKEW_HOURS = 5 / 60  # tolerate at most five minutes of clock skew
# B22N 매출 스코프 — ogam 은 영구 제외 (원전 §4)
ALLOWED_REVENUE_TEAMS = ("ad_gen", "ad_int", "live")

# ── 승인 골격 HTML 계약 상수 ──────────────────────────────────────────
TITLE = "MBD H2 KR 대시보드 — 2026"
REQUIRED_LABELS = ("마감예상", "목표", "달성률", "RAW", "평균",
                   "일반광고", "통광마", "라이브", "유튜브")
LIVE_MARKERS = ("LIVE · 실데이터", "LIVE 빌드", "고정 URL 운영본")
PRE_APPROVAL_MARKERS = ("STAGING", "승인 전 비공개")
# 구 payload-heavy 대시보드 흔적 — 절대 재출현 금지
LEGACY_PAYLOAD_MARKERS = ("window.__TOP_SUMMARY_DATA__", "window.__MBD_SOT_DATA__",
                          "__QUALITY_DETAIL_DATA__")
# 세로 기둥 차트(존재 필수) — 승인 골격 (세로 기둥만)
VERTICAL_MARKERS = ("gauge12", 'class="seg"', "align-items:end",
                    "grid-template-columns:repeat(12,1fr)")
# 가로 막대 semantics(존재 금지) — 가로 그래프/다이버징/불릿 전면 금지
FORBIDDEN_HBAR = ("horizontal-bar", "bar-horizontal", "hbar",
                  "diverging", 'aria-orientation="horizontal"')

SHEET_LIVE = "https://docs.google.com/spreadsheets/d/1Kw-IMgnP_kj0qY3q8thqsrPQ_KQvypTAX3hT5J-Gp4Q/edit?gid=1837542220#gid=1837542220"
SHEET_YT = "https://docs.google.com/spreadsheets/d/1mMkGwBuWr_L0YXvmDlGtPGzpm9kAgk8VQubjC_w52vg/edit?gid=34722178#gid=34722178"
SHEET_OKR = "https://docs.google.com/spreadsheets/d/1DgciUq9HLVs5Q-vt0GmuDrX8-Yd6T8SxEoRPudPWpPA/edit?gid=43885048#gid=43885048"
REQUIRED_SOURCE_LINKS = (SHEET_LIVE, SHEET_YT, SHEET_OKR)

# ── compact manifest 계약 (mbd-public-guard-v3) ───────────────────────
MANIFEST_RE = re.compile(
    r'<script type="application/json" id="mbd-public-guard">(.*?)</script>', re.S)
MANIFEST_SCHEMA = "mbd-public-guard-v3"
MANIFEST_GENERATOR = "mbd-dash-v5/render_venus.py"
MANIFEST_SCOPE = ["ad_gen", "ad_int", "live"]
LIVE_AVG_GMV_TARGET = 100_000_000
LIVE_GMV_BASIS = "1D"
PUBLIC_DETAIL_FIELDS = {
    "live": ["date", "status", "brand", "program", "package", "replay_url",
             "viewer_count", "gmv_1d", "gmv_3h"],
    "youtube": ["date", "status", "form", "title", "url", "views_total", "views_d7", "pis"],
}
CONTENT_LINK_RE = re.compile(
    r'<a class="content-link" data-content-link="(live|youtube)" href="([^"]+)" target="_blank" rel="noopener">')
YT_CONTENT_URL_RE = re.compile(r"^https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}$")
LIVE_CONTENT_URL_RE = re.compile(
    r"^https://www\.shoplive\.show/v1/player\.html\?ak=[A-Za-z0-9_-]+&amp;ck=[0-9a-f]{12}&amp;replay=true$")
MANIFEST_ALLOWED_KEYS = frozenset({
    "schema", "built_at_kst", "source_snapshot_as_of", "source_status",
    "default_month", "public_scope", "raw_rows_included", "generator",
    "sanitized_rows_included", "public_detail_fields",
    "source_payload_sha256", "live_avg_gmv_target_won", "live_gmv_basis"})
MANIFEST_MAX_BYTES = 4096
MANIFEST_SOURCE_KEYS = (
    "revenue_mirror", "live_quality", "yt_quality", "okr_targets", "owned_media")
MANIFEST_STATUS_KEYS = ("live_quality", "yt_quality", "okr_targets", "owned_media")
MANIFEST_FORBIDDEN_TOKENS = (
    '"packages"', '"forms"', '"rows"', '"review_full"', '"teams"',
    '"series_12m"', '"months"', '"by_month"', '"token"',
    "service_account", "private_key", "client_email",
    "iam.gserviceaccount", "-----begin", "authorization")

MONTH_OPTION_RE = re.compile(r'<option value="(\d+)"')
ANCHOR_RE = re.compile(r'<a\b[^>]*>', re.I)
# 자격증명 흔적 (공개본 어디에도 노출 금지)
CRED_TOKENS = ("service_account", "private_key", "client_email",
               "iam.gserviceaccount", "-----begin")
# 내부 절대경로 누출 (generator 의 상대경로 'mbd-dash-v5/...' 는 provenance 이므로 제외)
INTERNAL_PATH_TOKENS = ("/Users/automation", "/Users/sb.lee", "/home/", ".hermes/")
# [2026-08-08] 상세 공개는 allowlist 필드와 검증된 public player 링크만 허용한다.
PRIVATE_DETAIL_MARKERS = ('"review_full"', '"live_id"', "data-owner=")


def extract_manifest(html: str):
    """LIVE 아티팩트의 compact manifest (raw, parsed) 반환.
       정확히 1개가 아니면 AssertionError (테스트/도구용 헬퍼)."""
    blocks = MANIFEST_RE.findall(html)
    if len(blocks) != 1:
        raise AssertionError(f"expected exactly one public manifest, found {len(blocks)}")
    raw = blocks[0]
    return raw, json.loads(raw)


def _parse_iso(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _check_manifest(html: str, now: dt.datetime, require_fresh: bool, errors: list) -> None:
    """정확히 1개 · allowlist · 상수 계약 · raw/자격증명 부재 · 타임스탬프 tz-aware(+SLA)."""
    blocks = MANIFEST_RE.findall(html)
    if not blocks:
        errors.append('compact public manifest <script id="mbd-public-guard"> is missing')
        return
    if len(blocks) > 1:
        errors.append(f"expected exactly one public manifest, found {len(blocks)}")
    raw = blocks[0]
    if len(raw.encode("utf-8")) >= MANIFEST_MAX_BYTES:
        errors.append(f"manifest raw exceeds {MANIFEST_MAX_BYTES}B budget (raw rows may have leaked)")
    try:
        manifest = json.loads(raw)
    except (ValueError, TypeError):
        errors.append("manifest is not valid JSON")
        return
    if not isinstance(manifest, dict):
        errors.append("manifest is not a JSON object")
        return

    keys = set(manifest)
    if keys - MANIFEST_ALLOWED_KEYS:
        errors.append(f"manifest has unexpected keys {sorted(keys - MANIFEST_ALLOWED_KEYS)}")
    if MANIFEST_ALLOWED_KEYS - keys:
        errors.append(f"manifest missing keys {sorted(MANIFEST_ALLOWED_KEYS - keys)}")

    low = raw.lower()
    for tok in MANIFEST_FORBIDDEN_TOKENS:
        if tok in low:
            errors.append(f"manifest contains forbidden raw/credential token {tok!r}")

    if manifest.get("schema") != MANIFEST_SCHEMA:
        errors.append(f"manifest schema {manifest.get('schema')!r} != {MANIFEST_SCHEMA!r}")
    if manifest.get("raw_rows_included") is not False:
        errors.append("manifest raw_rows_included must be false")
    if manifest.get("sanitized_rows_included") is not True:
        errors.append("manifest sanitized_rows_included must be true")
    if manifest.get("public_detail_fields") != PUBLIC_DETAIL_FIELDS:
        errors.append("manifest public_detail_fields does not match approved allowlist")
    if manifest.get("public_scope") != MANIFEST_SCOPE:
        errors.append(f"manifest public_scope {manifest.get('public_scope')!r} != {MANIFEST_SCOPE}")
    if manifest.get("live_avg_gmv_target_won") != LIVE_AVG_GMV_TARGET:
        errors.append(
            f"manifest live_avg_gmv_target_won {manifest.get('live_avg_gmv_target_won')!r} "
            f"!= {LIVE_AVG_GMV_TARGET}")
    if manifest.get("live_gmv_basis") != LIVE_GMV_BASIS:
        errors.append(
            f"manifest live_gmv_basis {manifest.get('live_gmv_basis')!r} != {LIVE_GMV_BASIS!r}")
    if manifest.get("generator") != MANIFEST_GENERATOR:
        errors.append(f"manifest generator {manifest.get('generator')!r} != {MANIFEST_GENERATOR!r}")
    default_month = manifest.get("default_month")
    if not (isinstance(default_month, int) and 1 <= default_month <= 12):
        errors.append(f"manifest default_month {default_month!r} is not an integer in 1..12")
    payload_sha = manifest.get("source_payload_sha256")
    if not (isinstance(payload_sha, str) and re.fullmatch(r"[0-9a-f]{64}", payload_sha)):
        errors.append("manifest source_payload_sha256 is not a lowercase 64-hex SHA-256")

    statuses = manifest.get("source_status")
    if not isinstance(statuses, dict):
        errors.append("manifest source_status missing or not an object")
    else:
        if set(statuses) != set(MANIFEST_STATUS_KEYS):
            errors.append(
                f"manifest source_status keys {sorted(statuses)} != {sorted(MANIFEST_STATUS_KEYS)}")
        for key in MANIFEST_STATUS_KEYS:
            if statuses.get(key) != "current":
                errors.append(f"manifest source_status.{key} {statuses.get(key)!r} != 'current'")

    # 타임스탬프: 항상 tz-aware · require_fresh 시 48h 이내
    stamps = {"built_at_kst": manifest.get("built_at_kst")}
    snap = manifest.get("source_snapshot_as_of")
    if isinstance(snap, dict):
        if set(snap) != set(MANIFEST_SOURCE_KEYS):
            errors.append(
                f"manifest source_snapshot_as_of keys {sorted(snap)} != "
                f"{sorted(MANIFEST_SOURCE_KEYS)}")
        for key in MANIFEST_SOURCE_KEYS:
            stamps[f"source_snapshot_as_of.{key}"] = snap.get(key)
    else:
        errors.append("manifest source_snapshot_as_of missing or not an object")
    for label, value in stamps.items():
        parsed = _parse_iso(value)
        if parsed is None:
            errors.append(f"manifest {label} missing or unparseable timestamp")
        elif parsed.tzinfo is None:
            errors.append(f"manifest {label} is not timezone-aware")
        else:
            age_hours = (now - parsed).total_seconds() / 3600
            if age_hours < -MAX_FUTURE_SKEW_HOURS:
                errors.append(f"future snapshot: {label} is {-age_hours:.1f}h ahead of verifier time")
            elif require_fresh and age_hours > FRESHNESS_SLA_HOURS:
                errors.append(f"stale snapshot: {label} {age_hours:.1f}h exceeds "
                              f"the {FRESHNESS_SLA_HOURS}h freshness SLA")


def verify(html: str, now: dt.datetime, *, require_fresh: bool = False) -> list:
    """DOM-first LIVE 아티팩트 계약 검증. 정렬된 위반 리스트 반환([] = 통과)."""
    if not isinstance(html, str) or not html.strip():
        return ["html: empty or not a string"]
    errors: list = []

    # 1) 구 임베드 payload 재출현 금지
    for marker in LEGACY_PAYLOAD_MARKERS:
        if marker in html:
            errors.append(f"legacy embedded payload reappeared: {marker}")

    # 2) compact manifest (정확히 1개 · allowlist · 상수 · raw 부재 · freshness)
    _check_manifest(html, now, require_fresh, errors)

    # 3) LIVE 마커 정확히 1회 · STAGING/승인 전 비공개 부재
    for marker in LIVE_MARKERS:
        count = html.count(marker)
        if count != 1:
            errors.append(f"LIVE marker {marker!r} appears {count}x (expected exactly 1)")
    for marker in PRE_APPROVAL_MARKERS:
        if marker in html:
            errors.append(f"pre-approval marker {marker!r} must be absent from the LIVE artifact")

    # 4) title + 필수 가시 라벨
    if TITLE not in html:
        errors.append(f"missing title {TITLE!r}")
    for label in REQUIRED_LABELS:
        if label not in html:
            errors.append(f"missing required visible label {label!r}")

    # [2026-08-08] 위 KPI·차트·아래 팀/품질 카드와 중복되는 브리핑 UI·로컬 메모 회귀를 차단.
    for marker in ("data-report-flow", "월간 보고 흐름", "MONTHLY BRIEFING",
                   'class="report-block', "data-note-edit", "monthly-brief-note",
                   "REPORT_DATA", "localStorage"):
        if marker in html:
            errors.append(f"redundant monthly report flow present: {marker!r}")
    # [2026-08-09] 팝업/표가 이미 제공하는 구성과 provenance 장문은 카드 본문에 재노출하지 않는다.
    for marker in ('<div class="cause"><b>유입 구성</b>',
                   '<div class="cause" data-live-revenue-breakdown=',
                   "direct Sheet CURRENT",
                   "목표 SoT: H2 고정 1억원 · OKR row는 참고",
                   "집계 6/6건 · 총 421,752",
                   "목표 SoT: OKR 14행 (H2)",
                   "콘텐츠 링크 · 시청자수 / 1D 거래액 / 3H 거래액",
                   "콘텐츠 링크 · 누적조회수 / D7 조회수 / PIS",
                   "단위 억원 · DuckDB 미러",
                   "호버하면 PGM/프로모션 하위 금액",
                   "(첨부)"):
        if marker in html:
            errors.append(f"redundant dashboard copy present: {marker!r}")
    for domain in ("live", "youtube"):
        count = html.count(f'data-content-ledger="{domain}"')
        if count != 12:
            errors.append(f"content ledger {domain!r} appears {count}x (expected 12)")
    for marker in ('data-week-toggle=', "시청자수", "1D 거래액", "3H 거래액",
                   "누적조회수", "D7 조회수", "PIS", 'activity-column-head',
                   'activity-metric-head metric-trio', 'activity-date',
                   'activity-main-inline', 'activity-inline-meta', 'min-height:42px'):
        if marker not in html:
            errors.append(f"missing weekly content marker {marker!r}")
    for marker in ('class="week-counts"', 'class="activity-state', 'data-content-status=',
                   '<small>시청자수</small>', '<small>1D 거래액</small>',
                   '<small>3H 거래액</small>', '<small>누적조회수</small>'):
        if marker in html:
            errors.append(f"repeated or obsolete weekly marker present: {marker!r}")

    # [2026-08-09 correction] 유튜브 메인=평균조회수, 우측=구독자·발행·지속시간 카드.
    for marker in ('data-yt-main-average=', '전체 평균 조회수', 'data-yt-subscriber-card=',
                   'data-yt-publish-card=', 'data-yt-watch-duration-card=', '평균 시청지속시간'):
        if marker not in html:
            errors.append(f"missing youtube quality-card marker {marker!r}")
    if 'data-yt-channel-overview=' in html:
        errors.append("obsolete youtube left-subscriber overview present")

    # [2026-08-09] 일반광고 부킹률과 라이브 package→PGM/프로모션 팝업 구조의 공개 DOM 회귀 방지.
    for marker in ('data-adgen-booking-rate=', '부킹률', '비취소 부킹건수 ÷ 부킹건수 목표',
                   'data-live-revenue-breakdown=', 'data-live-package-mom=', '패키지별 매출',
                   'MoM ', '패키지 총액 = AF 패키지비',
                   '하위 구분 = PGM/프로모션'):
        if marker not in html:
            errors.append(f"missing sales-structure marker {marker!r}")
    # [2026-08-09] 9월 신청 시트 30건의 패키지비 합계가 미래월 부킹 화면까지 연결됐는지 고정한다.
    for marker in ('라이브 · 9월 패키지별 부킹', '1.74억', '8.03억',
                   '목표 12.8억 대비 채움 62.9%', '채움 82.1%'):
        if marker not in html:
            errors.append(f"missing September live booking source-parity marker {marker!r}")
    for marker in PRIVATE_DETAIL_MARKERS:
        if marker.lower() in html.lower():
            errors.append(f"private detail marker must not be public: {marker!r}")
    content_links = CONTENT_LINK_RE.findall(html)
    for kind in ("live", "youtube"):
        if not any(link_kind == kind for link_kind, _ in content_links):
            errors.append(f"missing {kind} content links")
    for kind, url in content_links:
        pattern = LIVE_CONTENT_URL_RE if kind == "live" else YT_CONTENT_URL_RE
        if not pattern.fullmatch(url):
            errors.append(f"invalid {kind} content URL: {url!r}")

    # 5) 월 셀렉터 #msel 옵션 1..12
    if '<select id="msel">' not in html:
        errors.append('missing month selector <select id="msel">')
    opts = sorted(int(v) for v in MONTH_OPTION_RE.findall(html))
    if opts != list(range(1, 13)):
        errors.append(f"month <option> values {opts} != 1..12")

    # 6) 필수 HTTPS 소스 시트 링크
    for url in REQUIRED_SOURCE_LINKS:
        if url not in html:
            errors.append(f"missing source link {url}")
        elif not url.startswith("https://"):
            errors.append(f"non-https source link {url}")

    # 7) 외부 링크 안전: target=_blank => rel=noopener · javascript: URI 금지
    for tag in ANCHOR_RE.findall(html):
        if 'target="_blank"' in tag and 'rel="noopener"' not in tag:
            errors.append(f"target=_blank link without rel=noopener: {tag[:90]}")
    if "javascript:" in html.lower():
        errors.append("unsafe javascript: URI present")

    # 8) 자격증명 · 내부 절대경로 누출 금지
    low = html.lower()
    for tok in CRED_TOKENS:
        if tok in low:
            errors.append(f"credential token leaked: {tok!r}")
    for tok in INTERNAL_PATH_TOKENS:
        if tok in html:
            errors.append(f"internal filesystem path leaked: {tok!r}")

    # 공개본은 aggregate-only. manifest 선언만 믿지 않고 실제 HTML도 negative-control.
    for marker in ('class="livetbl"', "시트 인사이트 전문", "콘텐츠별 성과"):
        if marker in html:
            errors.append(f"public raw-row marker present: {marker!r}")

    # 9) 세로 기둥 마커 필수 · 가로 막대 semantics 금지
    for tok in VERTICAL_MARKERS:
        if tok not in html:
            errors.append(f"missing vertical-column chart marker {tok!r}")
    for tok in FORBIDDEN_HBAR:
        if tok.lower() in low:
            errors.append(f"forbidden horizontal-bar semantics {tok!r}")

    # 10) 스코프 불변식 — ogam 영구 제외
    if "ogam" in ALLOWED_REVENUE_TEAMS or "ogam" in MANIFEST_SCOPE:
        errors.append("ogam must never be part of the B22N revenue scope")

    return sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description="MBD DOM-first dashboard release guard")
    parser.add_argument("html", nargs="?", default="index.html")
    parser.add_argument("--now", help="ISO timestamp for deterministic checks")
    parser.add_argument("--require-fresh", action="store_true",
                        help="Fail when any manifest timestamp is older than 48 hours")
    args = parser.parse_args()
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    html = Path(args.html).read_text(encoding="utf-8")
    errors = verify(html, now, require_fresh=args.require_fresh)
    if errors:
        print("DASHBOARD_GUARD=RED")
        for error in errors:
            print(f"- {error}")
        return 1
    print("DASHBOARD_GUARD=GREEN")
    print("scope=ad_gen,ad_int,live; ogam=excluded; manifest=mbd-public-guard-v3; detail=sanitized+allowlisted-links")
    return 0


if __name__ == "__main__":
    sys.exit(main())
