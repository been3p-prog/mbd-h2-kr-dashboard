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
import calendar
import datetime as dt
import html as html_lib
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
SHEET_YT_CONTENT = "https://docs.google.com/spreadsheets/d/1mMkGwBuWr_L0YXvmDlGtPGzpm9kAgk8VQubjC_w52vg/edit?gid=34722178#gid=34722178"
SHEET_YT_SSOT = "https://docs.google.com/spreadsheets/d/1lXIjLja-DEdBmDWDTM9LqNOG9UhVPCLS2B09InHQD90/edit?gid=673164445#gid=673164445"
SHEET_OKR = "https://docs.google.com/spreadsheets/d/1DgciUq9HLVs5Q-vt0GmuDrX8-Yd6T8SxEoRPudPWpPA/edit?gid=43885048#gid=43885048"
REQUIRED_SOURCE_LINKS = (SHEET_LIVE, SHEET_YT_CONTENT, SHEET_YT_SSOT, SHEET_OKR)

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
             "viewer_count", "gmv_1d", "gmv_3h", "gmv_1h"],
    "youtube": ["date", "status", "form", "title", "url", "views_total", "views_d7", "pis", "scheduled_date", "scheduled_time", "ip"],
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
MANIFEST_OPTIONAL_KEYS = frozenset({"stage_payload_sha256", "snapshot_captured_at"})
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


def youtube_week_state_ok(month_surface: str, youtube_window: str, month: int, elapsed: int) -> bool:
    ledger_marker = '<div class="content-ledger" data-content-ledger="youtube">'
    ledger_start = month_surface.find(ledger_marker)
    if ledger_start >= 0:
        month_surface = month_surface[ledger_start:]
    total_match = re.search(r'data-yt-main-source-total-weeks="(\d+)"', month_surface)
    total = int(total_match[1]) if total_match else elapsed
    if total_match and total != (calendar.monthrange(2026, month)[1] + 6) // 7:
        return False
    rendered = [
        int(value)
        for value in re.findall(rf'data-week-group="{month}-(\d+)"', month_surface)
    ]
    return rendered == list(range(1, total + 1)) or (
        not rendered and 'data-yt-weekly-empty="true"' in youtube_window
    )


def _month_surface(html: str, group: str, month: int) -> str | None:
    marker = re.compile(rf'class="{re.escape(group)} mv" data-m="(\d+)"')
    matches = list(marker.finditer(html))
    for index, match in enumerate(matches):
        if int(match.group(1)) == month:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(html)
            return html[match.start():end]
    return None


def _gauge_surface(html: str, month: int) -> str | None:
    marker = re.compile(r'class="g [^"]*" data-m="(\d+)"')
    matches = list(marker.finditer(html))
    for index, match in enumerate(matches):
        if int(match.group(1)) == month:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(html)
            return html[match.start():end]
    return None


def _parse_iso(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _check_manifest(
    html: str,
    now: dt.datetime,
    require_fresh: bool,
    errors: list,
    *,
    allow_stale_sources: frozenset[str],
) -> None:
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
    if keys - MANIFEST_ALLOWED_KEYS - MANIFEST_OPTIONAL_KEYS:
        errors.append(f"manifest has unexpected keys {sorted(keys - MANIFEST_ALLOWED_KEYS - MANIFEST_OPTIONAL_KEYS)}")
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
    stages = manifest.get("stage_payload_sha256", {})
    if (not isinstance(stages, dict) or set(stages) - {"live_daily", "live_window", "owned_youtube", "revenue_close"}
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in stages.values())):
        errors.append("invalid per-stage payload hash")
    captures = manifest.get("snapshot_captured_at", {})
    if not isinstance(captures, dict) or set(captures) - {"mbd", "youtube"}:
        errors.append("invalid snapshot capture metadata")
    else:
        for value in captures.values():
            try:
                if not isinstance(value, str) or dt.datetime.fromisoformat(value).tzinfo is None:
                    raise ValueError()
            except (ValueError, TypeError):
                errors.append("snapshot capture timestamp must be timezone-aware")

    statuses = manifest.get("source_status")
    if not isinstance(statuses, dict):
        errors.append("manifest source_status missing or not an object")
    else:
        if set(statuses) != set(MANIFEST_STATUS_KEYS):
            errors.append(
                f"manifest source_status keys {sorted(statuses)} != {sorted(MANIFEST_STATUS_KEYS)}")
        for key in MANIFEST_STATUS_KEYS:
            status = statuses.get(key)
            marker_count = html.count(f'data-stale-source="{key}"')
            if status == "current":
                if marker_count:
                    errors.append(f"manifest source_status.{key} is current but has stale marker")
            elif status == "stale" and key in allow_stale_sources:
                if marker_count != 1:
                    errors.append(
                        f"manifest source_status.{key} is stale but visible marker count is {marker_count}"
                    )
            else:
                errors.append(
                    f"manifest source_status.{key} {status!r} is not allowed"
                )

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
            source_key = label.split(".", 1)[1] if label.startswith("source_snapshot_as_of.") else None
            allowed_stale = (
                source_key in allow_stale_sources
                and isinstance(statuses, dict)
                and statuses.get(source_key) == "stale"
            )
            if require_fresh and age_hours > FRESHNESS_SLA_HOURS and not allowed_stale:
                errors.append(f"stale snapshot: {label} {age_hours:.1f}h exceeds "
                              f"the {FRESHNESS_SLA_HOURS}h freshness SLA")


def _check_youtube_main(html: str, errors: list) -> None:
    """현재월 메인 YouTube ledger/quality가 한 원천 계약으로 갱신됐는지 검증."""
    try:
        _, manifest = extract_manifest(html)
    except (AssertionError, ValueError, TypeError, json.JSONDecodeError):
        return
    month = manifest.get("default_month")
    if not isinstance(month, int) or not 1 <= month <= 12:
        return
    marker = f'<div class="mvr mv" data-m="{month}"'
    try:
        start = html.index(marker)
        if month < 12:
            end = html.index(f'<div class="mvr mv" data-m="{month + 1}"', start)
        else:
            end = html.index('<section id="youtubeWindow"', start)
        block = html[start:end]
        quality_start = block.index('<div class="card quality-card yt-quality">')
        yt_block = block[quality_start:]
    except ValueError:
        errors.append(f"youtube main source block for month {month} is missing")
        return

    attrs = {
        "publish count": "data-yt-main-source-publish-count",
        "latest publish date": "data-yt-main-source-latest-publish-date",
        "snapshot date": "data-yt-main-source-snapshot-date",
        "elapsed weeks": "data-yt-main-source-elapsed-weeks",
    }
    values: dict[str, str] = {}
    for label, attr in attrs.items():
        match = re.search(rf'{attr}="([^"]+)"', yt_block)
        if not match:
            errors.append(f"youtube main source {label} marker is missing")
        else:
            values[label] = match.group(1)
    if 'data-yt-main-quality-basis="analytics-d7"' not in yt_block:
        errors.append("youtube main source quality basis analytics-d7 is missing")
    if f'data-yt-quality-mom-main="{month}"' not in yt_block:
        errors.append(f"youtube main source current-month quality MoM marker {month} is missing")
    if f'data-quality-trend="youtube-{month}"' not in yt_block or "D+7 Analytics" not in yt_block:
        errors.append(f"youtube main source current-month D+7 trend {month} is missing")

    quality_attrs = {
        "average views": "data-yt-main-source-average-views",
        "LF average views": "data-yt-main-source-lf-average-views",
        "SF average views": "data-yt-main-source-sf-average-views",
        "subscriber count": "data-yt-main-source-subscriber-count",
        "D+7 completed": "data-yt-main-source-d7-completed",
    }
    quality_values: dict[str, str] = {}
    for label, attr in quality_attrs.items():
        match = re.search(rf'{attr}="([^"]+)"', yt_block)
        if not match:
            errors.append(f"youtube main source {label} marker is missing")
        else:
            quality_values[label] = match.group(1)

    expected_count = None
    try:
        expected_count = int(values.get("publish count", ""))
    except ValueError:
        errors.append(f"youtube main publish count is invalid: {values.get('publish count')!r}")
    rendered_count = yt_block.count('data-content-link="youtube"')
    if expected_count is not None and rendered_count != expected_count:
        errors.append(
            f"youtube main publish count mismatch: marker={expected_count}, rendered={rendered_count}"
        )

    def display_number(value: str | None) -> str:
        if value in (None, "none", "0"):
            return "—"
        try:
            return f"{int(value):,}"
        except ValueError:
            return "invalid"

    average_match = re.search(
        rf'data-yt-main-average="{month}"><div class="qk2">전체 평균 조회수</div>\s*'
        r'<div class="qv num">([^<]+)</div>',
        yt_block,
    )
    expected_average = display_number(quality_values.get("average views"))
    actual_average = average_match.group(1) if average_match else None
    if actual_average != expected_average:
        errors.append(
            f"youtube main average views mismatch: marker={expected_average!r}, rendered={actual_average!r}"
        )

    subscriber_match = re.search(
        rf'data-yt-subscriber-card="{month}".*?<div class="qn num">([^<]+)</div>',
        yt_block,
        re.S,
    )
    expected_subscriber = display_number(quality_values.get("subscriber count"))
    actual_subscriber = subscriber_match.group(1) if subscriber_match else None
    if actual_subscriber != expected_subscriber:
        errors.append(
            "youtube main subscriber count mismatch: "
            f"marker={expected_subscriber!r}, rendered={actual_subscriber!r}"
        )

    for form, label in (("lf", "LF average views"), ("sf", "SF average views")):
        match = re.search(
            rf'data-yt-{form}-average-card="{month}".*?<div class="qn num">([^<]+)</div>',
            yt_block,
            re.S,
        )
        expected_value = display_number(quality_values.get(label))
        actual_value = match.group(1) if match else None
        if actual_value != expected_value:
            errors.append(
                f"youtube main {label} mismatch: marker={expected_value!r}, rendered={actual_value!r}"
            )

    rendered_forms = re.findall(
        r'data-content-link="youtube".*?</a><small[^>]*>([^<]+)</small>',
        yt_block,
        re.S,
    )
    rendered_forms = [value.split(" · ", 1)[0].strip() for value in rendered_forms]
    expected_split = f"SF {rendered_forms.count('SF')}건 · LF {rendered_forms.count('LF')}건"
    other_count = sum(value not in {"LF", "SF"} for value in rendered_forms)
    if other_count:
        expected_split += f" · 기타 {other_count}건"
    if len(rendered_forms) != rendered_count or expected_split not in yt_block:
        errors.append(f"youtube main publish split mismatch: expected={expected_split!r}")

    completed_value = quality_values.get("D+7 completed")
    expected_d7 = (
        f"D+7 완료 {completed_value}/{expected_count}건"
        if completed_value is not None and expected_count is not None else None
    )
    if not expected_d7 or expected_d7 not in yt_block:
        errors.append(f"youtube main D+7 completion mismatch: expected={expected_d7!r}")

    latest_value = values.get("latest publish date")
    rendered_dates = [
        dt.date.fromisoformat(value)
        for value in re.findall(r'<time class="activity-date" datetime="(\d{4}-\d{2}-\d{2})">', yt_block)
    ]
    rendered_latest = max(rendered_dates).isoformat() if rendered_dates else "none"
    if latest_value is not None and latest_value != rendered_latest:
        errors.append(
            f"youtube main latest publish date mismatch: marker={latest_value!r}, rendered={rendered_latest!r}"
        )

    snapshot_value = values.get("snapshot date")
    try:
        snapshot_date = dt.date.fromisoformat(snapshot_value) if snapshot_value and snapshot_value != "none" else None
    except ValueError:
        snapshot_date = None
        errors.append(f"youtube main snapshot date is invalid: {snapshot_value!r}")
    try:
        elapsed_weeks = int(values.get("elapsed weeks", ""))
    except (TypeError, ValueError):
        elapsed_weeks = None
        errors.append(f"youtube main elapsed weeks is invalid: {values.get('elapsed weeks')!r}")
    if elapsed_weeks is not None:
        if not 1 <= elapsed_weeks <= 5:
            errors.append(f"youtube main elapsed weeks {elapsed_weeks} is outside 1..5")
        total_match = re.search(r'data-yt-main-source-total-weeks="(\d+)"', yt_block)
        total_weeks = int(total_match[1]) if total_match else elapsed_weeks
        if total_match and total_weeks != (calendar.monthrange(2026, month)[1] + 6) // 7:
            errors.append("youtube main total weeks does not match calendar month")
        expected_weeks = list(range(1, total_weeks + 1))
        rendered_weeks = [
            int(value)
            for value in re.findall(rf'data-week-group="{month}-(\d+)"', yt_block)
        ]
        if rendered_weeks != expected_weeks:
            errors.append(
                f"youtube main elapsed weeks mismatch: rendered={sorted(rendered_weeks)}, expected={sorted(expected_weeks)}"
            )
        if total_match and len(re.findall(rf'<details class="week-group" data-week-group="{month}-\d+" open>', yt_block)) != total_weeks:
            errors.append("youtube full-month weeks must be open by default")


def _check_live_schedule(html: str, errors: list) -> None:
    """Validate source count, full weeks and status counts on reconciled ledgers."""
    for month in range(1, 13):
        block = _month_surface(html, 'mvr', month)
        if not block or 'data-live-main-source-count=' not in block:
            continue
        ledger = block.split('data-content-ledger="live">', 1)[-1].split('<div class="card quality-card yt-quality">', 1)[0]
        try:
            value = lambda name: re.search(rf'data-live-main-source-{name}="([^"]+)"', ledger)[1]
            count, measured, pending, future = [int(value(key)) for key in ('count', 'measured', 'pending', 'future')]
            as_of = dt.date.fromisoformat(value('as-of'))
            if as_of.month != month or min(count, measured, pending, future) < 0:
                raise ValueError()
        except (TypeError, ValueError):
            errors.append('live schedule source metadata is invalid')
            continue
        row_dates = re.findall(r'<time class="activity-date" datetime="([^"]+)"', ledger)
        if count != len(row_dates) or count != measured + pending + future:
            errors.append('live schedule source count mismatch')
        states = re.findall(r'<small class="activity-inline-meta">(.*?)</small>', ledger)
        if [sum(label in state for state in states) for label in ('실적 반영', '실적 집계 대기', '예정')] != [measured, pending, future]:
            errors.append('live schedule status count mismatch')
        weeks = re.findall(rf'<details class="week-group" data-week-group="{month}-(\d+)" open>', ledger)
        total_weeks = (calendar.monthrange(as_of.year, month)[1] + 6) // 7
        if weeks != [str(week) for week in range(1, total_weeks + 1)]:
            errors.append('live schedule full-month open weeks mismatch')
        try:
            dates = [dt.date.fromisoformat(value) for value in row_dates]
            if dates != sorted(dates) or any((d.year, d.month) != (as_of.year, month) for d in dates) or sum(d > as_of for d in dates) != future:
                errors.append('live schedule dates mismatch')
        except ValueError:
            errors.append('live schedule dates are invalid')


def verify(
    html: str,
    now: dt.datetime,
    *,
    require_fresh: bool = False,
    require_schedule: bool = False,
    allow_stale_sources: set[str] | frozenset[str] = frozenset(),
) -> list:
    """DOM-first LIVE 아티팩트 계약 검증. 정렬된 위반 리스트 반환([] = 통과)."""
    if not isinstance(html, str) or not html.strip():
        return ["html: empty or not a string"]
    errors: list = []
    decoded_html = html_lib.unescape(html)

    # 1) 구 임베드 payload 재출현 금지
    for marker in LEGACY_PAYLOAD_MARKERS:
        if marker in html:
            errors.append(f"legacy embedded payload reappeared: {marker}")

    # 2) compact manifest (정확히 1개 · allowlist · 상수 · raw 부재 · freshness)
    allowed_stale = frozenset(allow_stale_sources)
    unknown_stale = sorted(allowed_stale - set(MANIFEST_STATUS_KEYS))
    if unknown_stale:
        errors.append(f"unknown allowed stale sources: {unknown_stale}")
    _check_manifest(
        html,
        now,
        require_fresh,
        errors,
        allow_stale_sources=allowed_stale,
    )
    _check_youtube_main(html, errors)
    try:
        _, schedule_manifest = extract_manifest(html)
        schedule_month = schedule_manifest.get('default_month')
        block = _month_surface(html, 'mvr', schedule_month) if isinstance(schedule_month, int) else None
        if block and ('data-yt-schedule-source-count=' in block or require_schedule):
            from youtube_schedule import verify_coverage, source_contract
            schedule_stale_allowed = ('yt_quality' in allowed_stale and
                                      schedule_manifest.get('source_status', {}).get('yt_quality') == 'stale')
            verify_coverage(block, now=now, require_fresh=require_fresh and not schedule_stale_allowed)
            if source_contract()[1]['url'] not in block:
                errors.append('YouTube schedule source link missing')
    except (AssertionError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        errors.append('YouTube schedule coverage invalid: ' + str(exc))
    _check_live_schedule(html, errors)

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
    for marker in ("온드미디어 · 참고", "3팀 스코프 밖 · 스택 최상단"):
        if marker in html:
            errors.append(f"legacy top owned-media reference present: {marker!r}")
    # [2026-08-09] 팝업/표가 이미 제공하는 구성과 provenance 장문은 카드 본문에 재노출하지 않는다.
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
    # [2026-08-25 correction] 좌측 라이브 상세탭은 8/6 주간 고정이 아니라
    # 디폴트 금월 누적 + 주차별 보기 계약을 지킨다.
    for marker in ('data-live-launch', 'aria-controls="liveWindow"',
                   'id="liveWindow"', 'data-live-window="weekly-performance"',
                   'data-live-period="mtd"', 'data-live-window-latest-date=',
                   '라이브 성과 상세탭', '디폴트 금월 누적', '주차별 보기',
                   '거래액 기준 분리', '데이터 사용 룰',
                   '카드 거래액=1D 브랜드 일거래액', '월/주간 효율=방송별 데이터 GMV',
                   '방송별 카드 거래액=`일 전체 GMV (라이브 브랜드 전체)`', '금월 누적 성과가 디폴트',
                   '미집계/0원 편성은 누적 성과에서 제외', '월 누적과 주간을 분리',
                   'data-live-week-summary=', 'data-live-weekly-view=',
                   'function setLiveWindow(open)', '주차별 보기 · 방송별 성과',
                   '가로 풀폭',
                   '.live-window{position:fixed;inset:16px;',
                   '@media (max-width:1180px){.live-window{inset:14px'):
        if marker not in html:
            errors.append(f"missing live window marker {marker!r}")
    live_card_count = html.count('data-live-broadcast-card=')
    live_empty = 'data-live-content-empty="true"' in html
    if live_empty:
        if live_card_count:
            errors.append(f"live empty state conflicts with {live_card_count} broadcast cards")
        if 'data-live-week-group=' in html:
            errors.append("live empty state conflicts with populated week groups")
    else:
        for marker in ('data-live-week-group=', 'RAW 수치 readback 전용'):
            if marker not in html:
                errors.append(f"missing live window marker {marker!r}")
        if live_card_count < 1:
            errors.append(f"live broadcast cards {live_card_count} < 1")
    for stale in ('aria-label="8월 1주차 라이브 성과 요약"',
                  '총액은 회복됐지만,<br>방송당 효율 회복으로 보긴 어려움',
                  'data-live-broadcast-card="frosch"',
                  'data-live-broadcast-card="cuchen"'):
        if stale in html:
            errors.append(f"stale live detail marker present: {stale!r}")

    # [2026-08-25 correction] 좌측 온드/유튜브 상세탭은 특정 8/11 주간
    # D+N 창이 아니라 디폴트 금월 누적 + 주차별 보기 계약을 지킨다.
    for marker in ('data-yt-launch', 'aria-controls="youtubeWindow"',
                   'id="youtubeWindow"', 'data-yt-window="weekly-detail"',
                   'data-yt-period="mtd"', 'data-owned-media-window="youtube"',
                   'data-yt-window-latest-date="', '온드미디어 상세탭',
                   '디폴트 금월 누적', '주차별 보기', 'YouTube Analytics 기준',
                   'data-yt-close', 'data-yt-weekly-view="',
                   'data-yt-week-summary="', '콘텐츠 D+N 참고',
                   'function setYoutubeWindow(open)', '.yt-window{position:fixed;inset:16px;',
                   '@media (max-width:1180px){.yt-window{inset:14px'):
        if marker not in html:
            errors.append(f"missing youtube window marker {marker!r}")
    yt_card_count = html.count('data-yt-weekly-card=')
    if yt_card_count < 1 and 'data-yt-content-empty="true"' not in html:
        errors.append(f"youtube content cards {yt_card_count} < 1")
    for stale in ('유튜브 주간 리포팅 창', '좌측 유튜브 탭 전용 UI',
                  'LF 비포애프터가 주간 성장 대부분', '동일 D+N × LF/SF × IP',
                  'public snapshot 기준 2026-08-11 23:57 KST',
                  'data-yt-weekly-card="beforeafter-lf-ep91"',
                  'data-yt-weekly-card="nationhome-lf-ep9"',
                  '보조 기여는 있으나 LF 히트로 보긴 어려움',
                  'SF 전국내집자랑 -123.0K', '혼수의기술 기발행 쇼츠 재상승'):
        if stale in html:
            errors.append(f"stale youtube detail marker present: {stale!r}")
    for marker in ('data-yt-weekly-analysis-raw=',
                   'fact_public_dplusn_', 'v_public_dplusn_',
                   'client_secret_', 'secrets/youtube', 'NOT_AUTHENTICATED',
                   'inline 유튜브 주간 분석'):
        if marker in html:
            errors.append(f"obsolete inline/raw youtube analysis marker present: {marker!r}")

    for marker in ('data-live-weekly-analysis=', '원본 rows 302–308', '공식 회고 전문',
                   '방송GMV 6,045만', '<small>방송GMV</small><b>6,045만</b>',
                   '방송별 카드/방당 효율=`방송별 데이터 GMV`',
                   'inset:22px 28px 22px 278px', 'inset:16px 18px 16px 238px'):
        if marker in html:
            errors.append(f"obsolete inline/raw live analysis marker present: {marker!r}")
    for marker in ('.team{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);padding:20px 22px}',
                   '.team .hd2{display:grid;grid-template-columns:minmax(0,1fr) 108px;gap:14px;align-items:center;min-height:108px}',
                   '.team .team-main{min-width:0;display:flex;flex-direction:column;justify-content:center;gap:14px}',
                   '.team .achv{--p:0;--achv-ring:var(--violet);--achv-track:#E8EDF3;position:relative;justify-self:end;width:108px;height:108px',
                   'data-achievement-ring="달성"',
                   'data-achievement-ring="채움"',
                   '.team .rows .r:last-child{order:-1'):
        if marker not in html:
            errors.append(f"missing team-card emphasis marker {marker!r}")
    if html.count('data-achievement-ring=') != 34:
        errors.append(f"team achievement rings {html.count('data-achievement-ring=')} != 34")
    for marker in ('<span class="pill up num">달성 ',
                   '<span class="pill dn num">달성 ',
                   '<span class="pill flat num">달성 ',
                   '<span class="pill flat num">채움 ',
                   'padding:20px 110px 20px 22px',
                   '.team .hd2{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;min-height:58px}',
                   '.team .hd2{position:relative;display:block;min-height:84px;padding-right:98px}',
                   '</div>\n          <div class="bigv num">',
                   'position:relative;flex:0 0 58px;width:58px;height:58px',
                   'width:84px;height:84px', 'width:76px;height:76px',
                   '.team .achv{--p:0;--achv-ring:#F59E0B',
                   '<small>월간 기준</small>'):
        if marker in html:
            errors.append(f"obsolete achievement pill present: {marker!r}")
    for marker in ('.pill.up{background:var(--red-soft);color:var(--red)',
                   '.pill.dn{background:var(--blue-soft);color:var(--blue)',
                   '.g .glab.neg{color:var(--blue)} .g .glab.pos{color:var(--red)',
                   '.tip .tv small.up{color:#FCA5A5}.tip .tv small.dn{color:#93C5FD'):
        if marker not in html:
            errors.append(f"missing red-up-blue-down marker {marker!r}")
    for marker in ('.pill.up{background:var(--green-soft)',
                   '.pill.dn{background:var(--red-soft)',
                   '.g .glab.neg{color:var(--red)} .g .glab.pos{color:var(--green)}',
                   '&lt;small&gt;MoM +', '&lt;small&gt;MoM -',
                   '<small>MoM +', '<small>MoM -',
                   '아래 빨강/초록 = 목표 대비 억원'):
        if marker in html:
            errors.append(f"obsolete green-up/red-down marker present: {marker!r}")
    # [2026-09-01] 8월 마감 리뷰는 canonical RAW 확정치로 고정한다.
    # Daily refresh may select the current month and move the global chip to
    # FORECAST YYYY-MM; August actuals are guarded on month-8 closed surfaces.
    august_top = _month_surface(html, "mvk", 8)
    august_detail = _month_surface(html, "mvr", 8)
    august_chart = _gauge_surface(html, 8)
    if not re.search(r'<option value="8"(?: selected)?>2026년 8월 · 확정</option>', html):
        errors.append('missing August review actual marker \'<option value="8">2026년 8월 · 확정</option>\'')
    if august_top is None:
        errors.append("missing August review actual surface 'mvk data-m=8'")
    else:
        decoded_august_top = html_lib.unescape(august_top)
        for marker in ('<span>일반광고</span><b><span class="tv"><span>8.68억</span><small class="up">MoM ▲ 7.9%</small>',
                       '<span>통광마</span><b><span class="tv"><span>2,909만</span><small class="dn">MoM ▼ 46.1%</small>',
                       '<span>라이브</span><b><span class="tv"><span>2.15억</span><small class="up">MoM ▲ 16.2%</small>'):
            if marker not in decoded_august_top:
                errors.append(f"missing monthly-flow tooltip MoM marker {marker!r}")
        top_markers = ('8월 마감확정치', 'data-closed-raw-value="11.12억"',
                       'data-closed-achievement="87.1%"', '확정 GAP', '-1.65억') if 'data-kpi-layout="two-card-v1"' in august_top else ('class="mvk mv" data-m="8" data-phase="closed"',
                       '<div class="k">8월 확정 총액<span class="phase">확정</span></div>',
                       '<div class="v num">11.1억</div>',
                       '확정 RAW · 8/1~8/31',
                       '<div class="v num">11.12억</div>',
                       '목표 진척 87.1%',
                       '<div class="k">월 목표</div><div class="v num">12.8억</div>',
                       '<div class="k">확정 GAP</div>',
                       '-1.65억')
        for marker in top_markers:
            if marker not in august_top:
                errors.append(f"missing August review actual marker {marker!r}")
    if august_detail is None:
        errors.append("missing August review actual surface 'mvr data-m=8'")
    else:
        for marker in ('class="mvr mv" data-m="8" data-phase="closed"',
                       '<span class="nm">일반광고</span><div class="bigv num">8.68억</div>',
                       '<span class="nm">통광마</span><div class="bigv num">2,909만</div>',
                       '<span class="nm">라이브</span><div class="bigv num">2.15억</div>',
                       '확정 RAW · 8/1~8/31</span><b>8.68억 <span class="mutpct">진척 100.3%</span>',
                       '확정 RAW · 8/1~8/31</span><b>2,909만 <span class="mutpct">진척 14.5%</span>',
                       '확정 RAW · 8/1~8/31</span><b>2.15억 <span class="mutpct">진척 101.4%</span>'):
            if marker not in august_detail:
                errors.append(f"missing August review actual marker {marker!r}")
    if august_chart is None:
        errors.append("missing August review actual surface 'g data-m=8'")
    else:
        decoded_august_chart = html_lib.unescape(august_chart)
        for marker in ('class="g closed" data-m="8"', '<div class="lab num">11.1</div>',
                       '<div class="glab num neg">−1.7</div>', '8월 · 확정', '확정 합계', '12.8억'):
            if marker not in decoded_august_chart:
                errors.append(f"missing August review actual marker {marker!r}")
    if "FORECAST 2026-08" in html:
        errors.append("closed August badge must never be FORECAST 2026-08")
    for marker in ('8월 마감예상액', '8월 · 진행 중'):
        if marker in html:
            errors.append(f"stale August forecast marker present: {marker!r}")
    for marker in ('class="week-counts"', 'class="activity-state', 'data-content-status=',
                   '<small>누적조회수</small>'):
        if marker in html:
            errors.append(f"repeated or obsolete weekly marker present: {marker!r}")

    # [2026-08-09 correction] 유튜브 메인=평균조회수, 우측=구독자·발행·지속시간 카드.
    for marker in ('data-yt-main-average=', '전체 평균 조회수', 'data-yt-subscriber-card=',
                   'data-yt-publish-card=', 'data-yt-watch-duration-card=', '평균 시청지속시간'):
        if marker not in html:
            errors.append(f"missing youtube quality-card marker {marker!r}")
    if 'data-yt-channel-overview=' in html:
        errors.append("obsolete youtube left-subscriber overview present")

    # [2026-08-31] 라이브 품질은 8월 기존 운영 기준을 유지하고, YouTube 현재월은
    # _check_youtube_main에서 manifest.default_month 기반으로 동적 검증한다.
    for marker in ('.qcell .qmom{font-size:11px;font-weight:850',
                   'data-live-quality-mom-main="8"', 'data-live-quality-mom="8-overall"',
                   'data-live-quality-mom="8-signature"', 'data-live-quality-mom="8-smart"',
                   'data-live-quality-mom="8-essential"', '1D 평균 거래액',
                   '8월 목표 1.00억 대비',
                   'data-yt-quality-mom-main=', 'Analytics 지속시간 미적재'):
        if marker not in html:
            errors.append(f"missing quality-card MoM marker {marker!r}")

    # [2026-08-12] 품질 카드 하위 월별 추이는 평균을 누적하지 않고 square bar + 전체평균 선으로 고정한다.
    for marker in ('.quality-trend{margin-top:16px', '.qt-bar{rx:0;shape-rendering:crispEdges}',
                   'data-quality-trend="live-8"',
                   'data-quality-trend-kind="live-package-average"',
                   'data-quality-trend-kind="yt-format-average"',
                   '평균치는 누적하지 않음', 'square bar=패키지별 평균',
                   'square bar=LF/SF 평균', '검은선=전체 평균',
                   '시그니처 평균', '스마트 평균', '에센셜 평균',
                   'LF 평균', 'SF 평균', 'D+7 완료'):
        if marker not in html:
            errors.append(f"missing quality trend marker {marker!r}")
    live_trend_count = html.count('data-quality-trend="live-')
    youtube_trend_count = html.count('data-quality-trend="youtube-')
    if live_trend_count != 12:
        errors.append(f"live quality trend charts {live_trend_count} != 12")
    if youtube_trend_count != 12:
        errors.append(f"youtube quality trend charts {youtube_trend_count} != 12")
    for marker in ('누적 높이=전체 평균', '평균 기여분(패키지 총', '포맷 총조회수 ÷ 전체 발행수'):
        if marker in html:
            errors.append(f"obsolete stacked-average quality trend marker present: {marker!r}")

    # [2026-08-12] 일반광고 부킹률은 목표가 아니라 Supabase 수용가능 구좌수 대비로 고정한다.
    for marker in ('data-adgen-booking-rate=', '비취소 구좌 부킹률', '비취소 부킹구좌수 ÷ 수용가능 구좌수',
                   'data-live-revenue-breakdown=', 'data-live-package-mom=', '패키지별 매출',
                   'MoM ', '패키지 총액 = AF 패키지비', '진행건수 = 확정 편성건',
                   'data-live-progress-count=', 'data-live-package-count=',
                   '진행 36건', '전월 +6건', '8건 · +2건', '13건 · +4건',
                   '시그니처 하위', '에센셜 하위', '스마트 하위'):
        if marker not in html:
            errors.append(f"missing sales-structure marker {marker!r}")
    for marker in ('부킹건수 목표', '비취소 부킹건수 ÷ 부킹건수 목표'):
        if marker in html:
            errors.append(f"obsolete booking-rate target denominator present: {marker!r}")
    retained_breakdowns = 0
    subpromotion_counts = {package: 0 for package in ('시그니처', '에센셜', '스마트')}
    for month in range(1, 13):
        surface = _month_surface(html, "mvr", month) or ""
        legacy_breakdown = 'data-live-revenue-breakdown=' in surface
        closed = 'data-phase="closed"' in surface.partition('>')[0]
        expected = int(legacy_breakdown or closed)
        retained_breakdowns += expected
        if surface.count('data-live-progress-count=') != expected:
            errors.append(f"live progress count markers do not match retained breakdowns: month {month}")
        if surface.count('data-live-package-count=') != expected * 3:
            errors.append(f"live package count markers do not match retained breakdowns: month {month}")
        decoded = html_lib.unescape(surface)
        package_rows = list(re.finditer(r'data-live-package-count="([^"]+)"', decoded))
        if expected and sorted(row.group(1) for row in package_rows) != sorted(subpromotion_counts):
            errors.append(f"live package count markers have missing or duplicate packages: month {month}")
        for index, row in enumerate(package_rows):
            package = row.group(1)
            if package not in subpromotion_counts:
                continue
            end = package_rows[index + 1].start() if index + 1 < len(package_rows) else decoded.find('<div class="card quality-card', row.end())
            section = decoded[row.end():end if end >= 0 else len(decoded)]
            # Retained closed cards have one drilldown per package. Canonical closes
            # may omit a drilldown when no promotion drivers were supplied.
            expected_sub = int((legacy_breakdown and closed) or '<div class="isubs">' in section)
            subpromotion_counts[package] += expected_sub
            if section.count(f'{package} 하위') != expected_sub:
                errors.append(f"live sub-promotion marker {package + ' 하위'!r} is missing or duplicated: month {month}")
    if html.count('data-live-progress-count=') != retained_breakdowns:
        errors.append("live progress count markers do not match retained breakdowns")
    if html.count('data-live-package-count=') != retained_breakdowns * 3:
        errors.append("live package count markers do not match retained breakdowns")
    for package, expected in subpromotion_counts.items():
        marker = f'{package} 하위'
        if html.count(marker) != expected:
            errors.append(f"live sub-promotion marker {marker!r} appears {html.count(marker)}x (expected {expected})")
    # New layout is a two-card contract, independent of calendar phase.
    for month in range(1, 13):
        top = _month_surface(html, "mvk", month) or ""
        if 'data-kpi-layout="two-card-v1"' not in top:
            continue
        roles = re.findall(r'<div class="kpi" data-kpi-role="([^"]+)"', top)
        phase = re.search(r'data-phase="([^"]+)"', top)
        allowed = {
            'closed': [('closed_actual', 'target_gap')],
            'current': [('forecast', 'current_raw'), ('booking', 'target')],
            'pending_close': [('forecast', 'current_raw'), ('booking', 'target')],
            'future': [('booking', 'target')],
        }
        if top.count('class="kpi"') != 2 or not phase or tuple(roles) not in allowed.get(phase[1], []):
            errors.append(f"two-card KPI roles/count invalid: month {month}")
        closed_card = re.search(r'<div class="kpi" data-kpi-role="closed_actual"[^>]*>', top)
        if closed_card:
            from dashboard_kpi_cards import element_end
            attrs = dict(re.findall(r'(data-[\w-]+)="([^"]*)"', closed_card[0]))
            body = top[closed_card.end():element_end(top, closed_card.start())]
            value = re.search(r'<div class="v num">([^<]+)</div>', body)
            achievement = re.search(r' · 달성률 ([0-9.]+%|—)(?=<|\s|$)', body)
            if (not value or value[1] != attrs.get('data-closed-raw-value') or
                    not achievement or achievement[1] != attrs.get('data-closed-achievement')):
                errors.append(f'two-card closed visible values invalid: month {month}')
        raw_card = re.search(r'<div class="kpi" data-kpi-role="current_raw"[^>]*>', top)
        forecast_card = re.search(r'<div class="kpi" data-kpi-role="forecast"[^>]*>', top)
        if forecast_card and 'data-current-forecast-status="canonical"' in forecast_card[0]:
            from dashboard_kpi_cards import element_end, mom
            attrs = dict(re.findall(r'(data-[\w-]+)="([^"]*)"', forecast_card[0]))
            body = top[forecast_card.end():element_end(top, forecast_card.start())]
            def forecast_amount(value):
                return (f'{value / 100_000_000:.2f}'.rstrip('0').rstrip('.') + '억' if abs(value) >= 100_000_000
                        else f'{round(value / 10_000):,}만' if abs(value) >= 10_000 else f'{value:,}')
            try:
                total, target = int(attrs['data-forecast-total-won']), int(attrs['data-forecast-target-won'])
                teams = {k: int(attrs[f'data-forecast-{k.replace("_", "-")}-won']) for k in ('ad_gen', 'ad_int', 'live')}
                previous = int(attrs['data-forecast-previous-won']) if 'data-forecast-previous-won' in attrs else None
                cutoff = dt.date.fromisoformat(attrs['data-forecast-as-of'])
                if (min(total, target, *teams.values()) < 0 or total != sum(teams.values()) or cutoff.month != month
                        or attrs['data-forecast-source'] != 'revenue.v_revenue_forecast_monthly'):
                    raise ValueError('invalid canonical metadata')
                if not raw_card or f'data-current-as-of="{cutoff.isoformat()}"' not in raw_card[0]:
                    raise ValueError('canonical forecast and RAW cutoff mismatch')
                achievement = f'{total / target * 100:.1f}%' if target else '—'
                if (f'<div class="v num">{forecast_amount(total)}</div>' not in body
                        or mom(total, previous) not in body or f'예상 달성률 {achievement}' not in body):
                    raise ValueError('canonical headline mismatch')
                gauge = _gauge_surface(html, month) or ''
                if f'data-forecast-total-won="{total}"' not in gauge or 'data-forecast-status="canonical"' not in gauge:
                    raise ValueError('canonical chart mismatch')
                if (f'<div class="lab num">{total / 100000000:.1f}</div>' not in gauge
                        or f'>{(total-target) / 100000000:+.1f}</div>' not in gauge):
                    raise ValueError('canonical chart visible value mismatch')
                detail = _month_surface(html, 'mvr', month) or ''
                for key, value in teams.items():
                    team = re.search(rf'data-current-forecast-team="{key}"(.*?)(?=<div class="team"|$)', detail, re.S)
                    if not team or f'<div class="bigv num">{forecast_amount(value)}</div>' not in team[1]:
                        raise ValueError('canonical team mismatch')
                    if f'data-forecast-team="{key}" data-forecast-won="{value}"' not in gauge:
                        raise ValueError('canonical segment mismatch')
            except (KeyError, ValueError, OverflowError):
                errors.append(f'two-card canonical forecast invalid: month {month}')
        if raw_card:
            from dashboard_kpi_cards import previous_cutoff, mom
            attrs = dict(re.findall(r'(data-[\w-]+)="([^"]*)"', raw_card[0]))
            try:
                as_of = dt.date.fromisoformat(attrs['data-current-as-of'])
                total = int(attrs['data-current-total-won'])
                prior_total = None
                if 'data-previous-as-of' in attrs or 'data-previous-total-won' in attrs:
                    if dt.date.fromisoformat(attrs['data-previous-as-of']) != previous_cutoff(as_of):
                        raise ValueError('not previous same-period cutoff')
                    prior_total = int(attrs['data-previous-total-won'])
                if as_of.month != month:
                    raise ValueError('current cutoff month mismatch')
                tail = top[raw_card.end():]
                amount = (f'{total / 100_000_000:.2f}'.rstrip('0').rstrip('.') + '억' if abs(total) >= 100_000_000
                          else f'{round(total / 10_000):,}만' if abs(total) >= 10_000 else f'{total:,}')
                if f'<div class="v num">{amount}</div>' not in tail or mom(total, prior_total) not in tail:
                    raise ValueError('amount or MoM does not match source metadata')
            except (KeyError, ValueError, OverflowError):
                errors.append(f'two-card same-period comparison invalid: month {month}')
    # The current next-month preview must agree with its linked booking surfaces.
    for current in range(1, 12):
        side = _month_surface(html, 'mvs', current) or ''
        feature = re.search(r'<div class="feature"[^>]*data-next-booking-month="[^>]*>', side)
        if not feature or 'data-phase="current"' not in side.partition('>')[0]:
            continue
        attrs = dict(re.findall(r'(data-[\w-]+)="([^"]*)"', feature[0]))
        try:
            month, total, target = (int(attrs[f'data-next-booking-{key}']) for key in ('month','total','target'))
            values = {key:int(attrs[f'data-next-booking-{key}']) for key in ('ad-gen','ad-int','live')}
            if month != current + 1 or total != sum(values.values()) or min(total,*values.values()) < 0 or target <= 0:
                raise ValueError('invalid next booking metadata')
            def booking_amount(value):
                return (f'{value / 100_000_000:.2f}'.rstrip('0').rstrip('.') + '억' if abs(value) >= 100_000_000
                        else f'{round(value / 10_000):,}만' if abs(value) >= 10_000 else f'{value:,}')
            if (f'차월 부킹 · {month}월' not in side or f'<div class="fv num">{booking_amount(total)}</div>' not in side
                    or f'목표 {booking_amount(target)} 대비 채움 {total / target * 100:.1f}%' not in side):
                raise ValueError('next booking preview mismatch')
            top, rest = _month_surface(html,'mvk',month) or '', _month_surface(html,'mvr',month) or ''
            if f'<div class="v num">{booking_amount(total)}</div>' not in top or f'<div class="lab num">{total / 100000000:.1f}</div>' not in (_gauge_surface(html,month) or ''):
                raise ValueError('next booking total mismatch')
            for label,key in (('일반광고','ad-gen'),('통광마','ad-int'),('라이브','live')):
                value = booking_amount(values[key])
                if f'<div>{label}<b>{value}</b></div>' not in side or f'<span class="nm">{label}</span><div class="bigv num">{value}</div>' not in rest:
                    raise ValueError('next booking component mismatch')
        except (KeyError,ValueError,OverflowError):
            errors.append(f'next booking surfaces invalid: month {current}')
    # A current pending forecast must not imply a complete total in another surface.
    if 'data-current-forecast-status="pending_scope"' in html:
        for current in range(1, 13):
            top = _month_surface(html, "mvk", current) or ""
            if 'data-current-forecast-status="pending_scope"' not in top:
                continue
            for group in ("mvk", "mvr"):
                surface = _month_surface(html, group, current)
                if surface is None or "확인 필요" not in surface:
                    errors.append(f"missing current pending forecast disclosure: {group}")
            if f'data-m="{current}" data-forecast-status="pending_scope"' not in html:
                errors.append("current forecast chart must disclose pending scope")
            labels = (f"{current}월 마감예측치",) if 'data-kpi-layout="two-card-v1"' in top else (f"{current}월 마감예상액", "마감예상 GAP")
            for label in labels:
                if not re.search(rf'<div class="k">{label}</div><div class="v num">확인 필요</div>', top):
                    errors.append("unavailable forecast must not be rendered as an amount")
            gauge = _gauge_surface(html, current) or ""
            if '<div class="seg"' in gauge or '<div class="lab num">—</div>' not in gauge:
                errors.append("unavailable forecast chart must not retain a revenue stack")
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
    parser.add_argument(
        "--allow-stale-source",
        action="append",
        choices=MANIFEST_STATUS_KEYS,
        default=[],
        help="Allow one visibly marked stale source while keeping other freshness checks strict",
    )
    args = parser.parse_args()
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    html = Path(args.html).read_text(encoding="utf-8")
    errors = verify(
        html,
        now,
        require_fresh=args.require_fresh,
        require_schedule=True,
        allow_stale_sources=set(args.allow_stale_source),
    )
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
