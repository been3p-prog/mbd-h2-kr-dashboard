#!/usr/bin/env python3
"""Deterministic release guard for the static MBD dashboard artifact."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))
ALLOWED_REVENUE_TEAMS = {"ad_gen", "ad_int", "live"}


def extract_json_assignment(html: str, variable: str) -> dict:
    marker = f"window.{variable} ="
    start = html.find(marker)
    if start < 0:
        raise AssertionError(f"missing {marker}")
    start += len(marker)
    payload, _ = json.JSONDecoder().raw_decode(html[start:].lstrip())
    return payload


def verify(html: str, now: dt.datetime, *, require_fresh: bool = False) -> list[str]:
    errors: list[str] = []
    top = extract_json_assignment(html, "__TOP_SUMMARY_DATA__")
    sot = extract_json_assignment(html, "__MBD_SOT_DATA__")
    periods = top.get("month", {}).get("periods", [])

    runtime_checks = {
        "selected-period diagnosis renderer": r"function\s+renderGapDiagnosis\(period,p\)\s*\{",
        "diagnosis renderer wiring": r"function\s+renderSummaryPeriod\(period,idx\)[\s\S]*?renderGapDiagnosis\(period,p\);",
        "raw semantic guard definition": r"function\s+rawActualPresentation\(period,p\)\s*\{",
        "raw semantic guard wiring": r"const\s+raw=rawActualPresentation\(period,p\);[\s\S]*?raw\.label[\s\S]*?raw\.value[\s\S]*?raw\.small",
        "current-month auto selection wiring": r"const\s+summarySelection=\{month:defaultMonthSelection\(\),week:topSummary\.week\?\.current_index\|\|0\};",
        "revenue-scope render filter": r"function\s+scopedSummarySegments\(p\)\s*\{\s*return\s+\(p\?\.segments\|\|\[\]\)\.filter\(seg=>REVENUE_SCOPE_SET\.has\(seg\.team\)\);\s*\}",
        "scoped month-chart renderer": r"function\s+renderScopedMonthChart\(idx\)\s*\{[\s\S]*?const\s+segments=scopedSummarySegments\(p\);",
        "scoped month-chart renderer wiring": r"function\s+renderSummaryPeriod\(period,idx\)[\s\S]*?if\(period==='month'\)renderScopedMonthChart\(idx\);",
        "scoped week-chart renderer": r"function\s+renderScopedWeekChart\(idx\)\s*\{[\s\S]*?const\s+segments=scopedSummarySegments\(p\);",
        "scoped week-chart renderer wiring": r"function\s+renderSummaryPeriod\(period,idx\)[\s\S]*?if\(period==='week'\)renderScopedWeekChart\(idx\);",
        "snapshot freshness warning": r"function\s+renderFreshnessWarning\(\)\s*\{",
    }
    for label, pattern in runtime_checks.items():
        if not re.search(pattern, html):
            errors.append(f"missing or unwired {label}")

    scope_match = re.search(r"const\s+REVENUE_SCOPE=Object\.freeze\(\[([^\]]*)\]\);", html)
    if not scope_match:
        errors.append("missing explicit revenue scope")
    else:
        runtime_scope = re.findall(r"['\"]([^'\"]+)['\"]", scope_match.group(1))
        if set(runtime_scope) != ALLOWED_REVENUE_TEAMS or len(runtime_scope) != len(ALLOWED_REVENUE_TEAMS):
            errors.append(f"runtime revenue scope mismatch: {runtime_scope}")

    freshness_pos = html.find('data-dashboard-freshness')
    first_period_view_pos = html.find('class="summary-period-view')
    if freshness_pos < 0 or first_period_view_pos < 0 or freshness_pos > first_period_view_pos:
        errors.append("freshness warning must remain visible outside month/week period views")

    if re.search(r">\s*\d{1,2}월 미달 원인\s*<", html):
        errors.append("month diagnosis is hard-coded in the initial HTML instead of selected-period state")

    current_ym = now.astimezone(KST).strftime("%Y-%m")
    selected_period = next((p for p in periods if p.get("ym") == current_ym), None)
    if selected_period:
        basis = str(selected_period.get("current_actual_basis") or "")
        if basis == "월전체 집계" and "월전체 집계값은 RAW 누적으로 표시하지 않음" not in html:
            errors.append(f"{current_ym}: full-month aggregate can still be mislabeled as current RAW")

    built_raw = sot.get("meta", {}).get("built_at_kst") or sot.get("meta", {}).get("today")
    if not built_raw:
        errors.append("missing dashboard build timestamp")
    else:
        try:
            built_at = dt.datetime.fromisoformat(str(built_raw))
            if built_at.tzinfo is None:
                built_at = built_at.replace(tzinfo=KST)
            age_hours = (now.astimezone(KST) - built_at.astimezone(KST)).total_seconds() / 3600
            if age_hours > 48:
                if require_fresh:
                    errors.append(f"stale snapshot ({age_hours:.1f}h) exceeds the 48h freshness SLA")
                elif "data-dashboard-freshness" not in html:
                    errors.append(f"stale snapshot ({age_hours:.1f}h) has no visible freshness warning")
        except ValueError:
            errors.append(f"invalid dashboard build timestamp: {built_raw}")

    if "ogam" in ALLOWED_REVENUE_TEAMS:
        errors.append("ogam must never be part of the B22N revenue scope")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("html", nargs="?", default="index.html")
    parser.add_argument("--now", help="ISO timestamp for deterministic tests")
    parser.add_argument("--require-fresh", action="store_true", help="Fail when the snapshot is older than 48 hours")
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
    print("scope=ad_gen,ad_int,live; ogam=excluded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
