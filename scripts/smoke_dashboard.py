#!/usr/bin/env python3
"""Headless-browser smoke test for the rendered static dashboard."""
from __future__ import annotations

import datetime as dt
import html as html_lib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))


def extract_json_assignment(source: str, variable: str) -> dict:
    marker = f"window.{variable} ="
    start = source.index(marker) + len(marker)
    payload, _ = json.JSONDecoder().raw_decode(source[start:].lstrip())
    return payload


def plain(fragment: str) -> str:
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def find_chrome() -> str:
    candidates = [
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError("Chrome/Chromium is required for dashboard smoke verification")


def render_dom(source: str, probe: str) -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as handle:
        handle.write(source.replace("</body>", probe + "</body>", 1))
        render_path = Path(handle.name)
    try:
        return subprocess.run(
            [
                find_chrome(),
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--allow-file-access-from-files",
                "--virtual-time-budget=3000",
                "--dump-dom",
                render_path.as_uri(),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    finally:
        render_path.unlink(missing_ok=True)


def main() -> int:
    source_path = Path(sys.argv[1] if len(sys.argv) > 1 else "index.html").resolve()
    source = source_path.read_text(encoding="utf-8")
    top = extract_json_assignment(source, "__TOP_SUMMARY_DATA__")
    sot = extract_json_assignment(source, "__MBD_SOT_DATA__")
    periods = top["month"]["periods"]
    current_ym = dt.datetime.now(KST).strftime("%Y-%m")
    selected = next((p for p in periods if p.get("ym") == current_ym), None)
    if selected is None:
        selected = periods[top["month"].get("current_index", 0)]

    selected_idx = periods.index(selected)
    alternate_idx = 0 if selected_idx else min(1, len(periods) - 1)
    switch_probe = f'''<script>
(() => {{
  const click = index => document.querySelector(`.summary-period-view[data-period-view="month"] .stack-month[data-period-index="${{index}}"]`)?.click();
  click({alternate_idx});
  click({selected_idx});
}})();
</script>'''
    proc = render_dom(source, switch_probe)
    if proc.returncode != 0:
        print(proc.stderr[-2000:])
        return 1

    rendered = proc.stdout
    active = re.search(
        r'<div class="summary-period-view active" data-period-view="month">(.*?)<div class="summary-period-view" data-period-view="week">',
        rendered,
        re.S,
    )
    if not active:
        print("DASHBOARD_SMOKE=RED\n- active month view not found")
        return 1
    month_html = active.group(1)
    visible_text = plain(month_html)
    full_text = plain(rendered)
    errors: list[str] = []

    ym = str(selected["ym"])
    year, month = ym.split("-", 1)
    expected_label = f"{int(year)}년 {int(month)}월"
    expected_month = str(int(month))
    if f"선택 기준 · {expected_label}" not in visible_text:
        errors.append(f"selected-period label mismatch: expected {expected_label}")
    # [2026-08-06] 판정 카드 계약 변경 — 미달 팀만이 아니라 3팀의 마감예측·목표·GAP을 모두 렌더링해야 한다.
    if f"{expected_month}월 3팀 매출 요약" not in visible_text:
        errors.append(f"selected-period three-team summary mismatch: expected {expected_month}월")
    decision_teams = set(re.findall(r'data-gap-team="([^"]+)"', month_html))
    if decision_teams != {"ad_gen", "ad_int", "live"}:
        errors.append(f"decision summary team mismatch: {sorted(decision_teams)}")

    if "온드·YT 광고" in visible_text:
        errors.append("owned YouTube advertising leaked into the rendered B22N revenue chart")
    if "owned-youtube" in month_html:
        errors.append("owned YouTube segment leaked into the rendered B22N revenue chart")

    basis = str(selected.get("current_actual_basis") or "")
    if "RAW 누적" not in basis:
        if "현재 RAW 누적 미연결" not in visible_text:
            errors.append("unverified current_actual is not rendered as RAW disconnected")
        if "월전체 집계값은 RAW 누적으로 표시하지 않음" not in visible_text:
            errors.append("RAW-disconnection explanation is missing")

    topmeta = re.search(r'<div class="topmeta">(.*?)</div>', rendered, re.S)
    if not topmeta or selected["ym"] not in plain(topmeta.group(1)):
        errors.append(f"top metadata does not follow selected month {selected['ym']}")

    rendered_teams = set(re.findall(r'data-summary-segment="([^"]+)"', month_html))
    if "ogam" in rendered_teams:
        errors.append("ogam leaked into the rendered B22N revenue scope")

    week_periods = top["week"]["periods"]
    week_selected = week_periods[top["week"].get("current_index", 0)]
    week_selected_idx = week_periods.index(week_selected)
    week_alternate_idx = 0 if week_selected_idx else min(1, len(week_periods) - 1)
    week_probe = f'''<script>
(() => {{
  document.querySelector('[data-summary-period="week"]')?.click();
  const click = index => document.querySelector(`.summary-period-view[data-period-view="week"] .stack-month[data-period-index="${{index}}"]`)?.click();
  click({week_alternate_idx});
  click({week_selected_idx});
}})();
</script>'''
    week_proc = render_dom(source, week_probe)
    if week_proc.returncode != 0:
        errors.append("week-toggle smoke render failed")
    else:
        week_active = re.search(
            r'<div class="summary-period-view active" data-period-view="week">(.*?)</section>\s*<div class="trend-bridge-banner"',
            week_proc.stdout,
            re.S,
        )
        if not week_active:
            errors.append("active week view not found after period toggle")
        else:
            week_html = week_active.group(1)
            week_text = plain(week_html)
            week_label = str(week_selected.get("month_label") or week_selected.get("short_label") or week_selected.get("ym"))
            if f"선택 기준 · {week_label}" not in week_text:
                errors.append(f"selected-week label mismatch: expected {week_label}")
            if "온드·YT 광고" in week_text or "owned-youtube" in week_html:
                errors.append("owned YouTube advertising leaked into the rendered B22N weekly chart")
            week_teams = set(re.findall(r'data-summary-segment="([^"]+)"', week_html))
            if not week_teams.issubset({"ad_gen", "ad_int", "live"}):
                errors.append("non-B22N team leaked into the rendered weekly revenue scope")
            if "B22N 매출 범위 · 일반광고 / 통광마 / 라이브" not in week_text:
                errors.append("weekly B22N revenue scope disclosure is missing")

    built_raw = sot.get("meta", {}).get("built_at_kst") or sot.get("meta", {}).get("today")
    built_at = dt.datetime.fromisoformat(str(built_raw))
    if built_at.tzinfo is None:
        built_at = built_at.replace(tzinfo=KST)
    stale = (dt.datetime.now(KST) - built_at.astimezone(KST)).total_seconds() > 48 * 3600
    if stale and "선택월 변경은 데이터 새로고침이 아닙니다" not in full_text:
        errors.append("stale-snapshot warning is not visible in rendered DOM")

    if errors:
        print("DASHBOARD_SMOKE=RED")
        for error in errors:
            print(f"- {error}")
        return 1
    print("DASHBOARD_SMOKE=GREEN")
    print(f"selected={selected['ym']}; raw_basis={basis}; ogam=excluded; stale_warning={'on' if stale else 'off'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
