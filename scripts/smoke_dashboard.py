#!/usr/bin/env python3
"""Headless-Chrome smoke test for the DOM-first MBD dashboard (LIVE artifact).

임의 HTML 경로에서 동작(기본 index.html). 월 드롭다운(#msel)으로 현재월→대체월 전환 후
location.hash 갱신·선택월 가시성 변화·소스 링크 존재·콘솔/페이지 에러 부재를 검증하고,
데스크톱 1440x900·모바일 390x844 두 뷰포트에서 가로 오버플로가 0인지 확인한다.
Playwright 제어 + 시스템 Chrome으로 요청한 CSS viewport를 정확히 강제한다.
"""
from __future__ import annotations

import html as html_lib
import importlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast

VIEWPORTS = ((1440, 900, "desktop"), (390, 844, "mobile"))
SELECTED_OPTION_RE = re.compile(r'<option value="(\d+)" selected>')
MANIFEST_RE = re.compile(
    r'<script type="application/json" id="mbd-public-guard">(.*?)</script>', re.S)
RESULT_RE = re.compile(r'<div id="smoke-result">(.*?)</div>', re.S)

# 로드 시점부터 window.onerror / console.error 를 수집 (head 에 주입 → 페이지 스크립트보다 먼저)
_CAPTURER = (
    "window.__smoke_errors=[];"
    "addEventListener('error',function(e){window.__smoke_errors.push('error: '+(e.message||String(e.error||e)));});"
    "addEventListener('unhandledrejection',function(e){window.__smoke_errors.push('rejection: '+String(e.reason));});"
    "(function(){var _e=console.error;console.error=function(){"
    "window.__smoke_errors.push('console.error: '+Array.prototype.join.call(arguments,' '));"
    "return _e.apply(console,arguments);};})();")


def find_chrome() -> str:
    candidates = [
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError("Chrome/Chromium is required for dashboard smoke verification")


def _probe_script(target_month: int) -> str:
    return (
        "(function(){var out={};"
        "function vis(){return Array.prototype.filter.call(document.querySelectorAll('.mvk'),"
        "function(x){return x.style.display!=='none';}).map(function(x){return x.dataset.m;});}"
        "try{var sel=document.getElementById('msel');"
        "out.hasSelect=!!sel;out.optionCount=sel?sel.options.length:0;"
        "out.initialHash=location.hash;out.visibleBefore=vis();"
        "sel.value='%d';sel.dispatchEvent(new Event('change'));"
        "out.afterHash=location.hash;out.selectedAfter=sel.value;out.visibleAfter=vis();"
        "out.doc={sw:document.documentElement.scrollWidth,cw:document.documentElement.clientWidth,"
        "bsw:document.body.scrollWidth,iw:window.innerWidth};"
        "out.links={live:!!document.querySelector('a[href*=\"1Kw-IMgnP\"]'),"
        "yt:!!document.querySelector('a[href*=\"1mMkGwBuWr\"]'),"
        "okr:!!document.querySelector('a[href*=\"1DgciUq9HLVs\"]')};"
        "var liveLaunch=document.querySelector('[data-live-launch]');"
        "var liveWindow=document.querySelector('[data-live-window=\"weekly-performance\"]');"
        "out.liveWindow={hasLaunch:!!liveLaunch,hasWindow:!!liveWindow,beforeHidden:liveWindow?liveWindow.getAttribute('aria-hidden'):null};"
        "if(liveLaunch&&liveWindow){liveLaunch.click();out.liveWindow.afterOpen=liveWindow.classList.contains('open');"
        "out.liveWindow.ariaOpen=liveWindow.getAttribute('aria-hidden');"
        "out.liveWindow.expanded=liveLaunch.getAttribute('aria-expanded');"
        "out.liveWindow.title=!!liveWindow.querySelector('#liveWindowTitle');"
        "out.liveWindow.basis=/데이터 사용 룰/.test(liveWindow.textContent||'')&&/카드 거래액=1D 브랜드 일거래액/.test(liveWindow.textContent||'');"
        "var lr=liveWindow.getBoundingClientRect();out.liveWindow.rect={left:Math.round(lr.left),"
        "rightGap:Math.round(window.innerWidth-lr.right),width:Math.round(lr.width),viewport:window.innerWidth};"
        "var close=liveWindow.querySelector('[data-live-close]');if(close){close.click();}"
        "out.liveWindow.afterClose=liveWindow.classList.contains('open');"
        "out.liveWindow.ariaClose=liveWindow.getAttribute('aria-hidden');}"
        "var rest=document.querySelector('.mvr[data-m=\"'+sel.value+'\"]');"
        "var futureRoots=document.querySelectorAll('.mv[data-phase=\"future\"]');"
        "var futureForbidden=Array.prototype.reduce.call(futureRoots,function(n,x){"
        "var tips=Array.prototype.map.call(x.querySelectorAll('[data-tip]'),function(t){return t.dataset.tip||'';}).join(' ');"
        "return n+(/GAP|달성률|▼|미달/.test((x.textContent||'')+' '+tips)?1:0);},0);"
        "out.lower={visibleRest:(rest&&getComputedStyle(rest).display!=='none')?1:0,"
        "teamCards:rest?rest.querySelectorAll('.team').length:0,"
        "qualityCards:rest?rest.querySelectorAll('.quality-card').length:0,"
        "rawRowTables:document.querySelectorAll('.livetbl,.raw-row-table').length,"
        "futureRootCount:futureRoots.length,futureForbiddenCount:futureForbidden};"
        "out.errors=(window.__smoke_errors||[]).slice(0,20);"
        "}catch(e){out.fatal=String(e)+' | '+((e&&e.stack)||'');}"
        "var d=document.createElement('div');d.id='smoke-result';d.textContent=JSON.stringify(out);"
        "document.body.appendChild(d);})();") % target_month


def instrument(source: str, target_month: int) -> str:
    src = source.replace('<meta charset="utf-8">',
                         '<meta charset="utf-8"><script>' + _CAPTURER + "</script>", 1)
    src = src.replace("</body>", "<script>" + _probe_script(target_month) + "</script></body>", 1)
    return src


def render_dom(instrumented: str, width: int, height: int):
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as handle:
        handle.write(instrumented)
        render_path = Path(handle.name)
    try:
        try:
            sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
        except (ImportError, ModuleNotFoundError):
            return subprocess.CompletedProcess(
                ["playwright"], 70, "",
                "playwright package missing; exact CSS viewport smoke cannot run")
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    executable_path=find_chrome(), headless=True,
                    args=["--no-sandbox", "--disable-gpu", "--hide-scrollbars"])
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(render_path.as_uri(), wait_until="load", timeout=30_000)
                page.wait_for_timeout(150)
                dumped = page.content()
                browser.close()
            return subprocess.CompletedProcess(["playwright"], 0, dumped, "")
        except Exception as exc:
            return subprocess.CompletedProcess(["playwright"], 71, "", f"{type(exc).__name__}: {exc}")
    finally:
        render_path.unlink(missing_ok=True)


def parse_result(dumped: str):
    match = RESULT_RE.search(dumped)
    if not match:
        return None
    try:
        return json.loads(html_lib.unescape(match.group(1)))
    except json.JSONDecodeError:
        return None


def _current_month(source: str) -> int:
    selected = SELECTED_OPTION_RE.search(source)
    if selected:
        return int(selected.group(1))
    manifest = MANIFEST_RE.findall(source)
    if manifest:
        try:
            value = json.loads(manifest[0]).get("default_month")
            if isinstance(value, int) and 1 <= value <= 12:
                return value
        except json.JSONDecodeError:
            pass
    return 8


def _check_viewport(result, width, height, tag, *, switch_expected):
    """뷰포트별 위반 리스트 반환. switch_expected 시 월 전환 계약도 검증."""
    errors = []
    if result is None:
        return [f"{tag}: smoke-result probe not found in dumped DOM"]
    if result.get("fatal"):
        errors.append(f"{tag}: probe fatal: {result['fatal'][:160]}")
    if result.get("errors"):
        errors.append(f"{tag}: console/page errors: {result['errors']}")
    doc = result.get("doc") or {}
    sw, cw = doc.get("sw"), doc.get("cw")
    bsw, iw = doc.get("bsw"), doc.get("iw")
    if not all(isinstance(value, int) for value in (sw, cw, bsw, iw)):
        errors.append(f"{tag}: invalid viewport metrics sw={sw}, cw={cw}, bsw={bsw}, iw={iw}")
    else:
        sw_i, cw_i, bsw_i, iw_i = cast(int, sw), cast(int, cw), cast(int, bsw), cast(int, iw)
        if sw_i > cw_i:
            errors.append(f"{tag}: horizontal overflow docElement scrollWidth={sw_i} > clientWidth={cw_i}")
        if bsw_i > iw_i:
            errors.append(f"{tag}: horizontal overflow body scrollWidth={bsw_i} > innerWidth={iw_i}")
    if iw != width:
        errors.append(f"{tag}: CSS viewport {iw}px != requested width {width}px")
    lower = result.get("lower")
    if not isinstance(lower, dict):
        errors.append(f"{tag}: lower-card acceptance evidence missing")
    else:
        if lower.get("visibleRest") != 1:
            errors.append(f"{tag}: lower-card selected month is not visible")
        if lower.get("teamCards") != 3:
            errors.append(f"{tag}: lower-card teamCards={lower.get('teamCards')} != 3")
        if lower.get("qualityCards") != 2:
            errors.append(f"{tag}: lower-card qualityCards={lower.get('qualityCards')} != 2")
        if lower.get("rawRowTables") != 0:
            errors.append(f"{tag}: public raw-row tables={lower.get('rawRowTables')} != 0")
        if not isinstance(lower.get("futureRootCount"), int) or lower.get("futureRootCount") <= 0:
            errors.append(f"{tag}: future negative-control roots missing")
        if lower.get("futureForbiddenCount") != 0:
            errors.append(
                f"{tag}: future forbidden gap/achievement/decline/miss labels="
                f"{lower.get('futureForbiddenCount')} != 0")
    if switch_expected:
        if result.get("optionCount") != 12:
            errors.append(f"{tag}: month selector has {result.get('optionCount')} options (expected 12)")
        target = switch_expected["target"]
        current = switch_expected["current"]
        if result.get("afterHash") != f"#m{target}":
            errors.append(f"{tag}: location.hash={result.get('afterHash')!r} did not update to #m{target}")
        if str(result.get("selectedAfter")) != str(target):
            errors.append(f"{tag}: selected month {result.get('selectedAfter')!r} != {target}")
        if result.get("visibleBefore") != [str(current)]:
            errors.append(f"{tag}: initial visible month {result.get('visibleBefore')} != [{current}]")
        if result.get("visibleAfter") != [str(target)]:
            errors.append(f"{tag}: post-switch visible month {result.get('visibleAfter')} != [{target}]")
        if result.get("visibleBefore") == result.get("visibleAfter"):
            errors.append(f"{tag}: month visibility did not change on selection")
        links = result.get("links") or {}
        if not all(links.get(k) for k in ("live", "yt", "okr")):
            errors.append(f"{tag}: required source sheet links missing: {links}")
    live_window = result.get("liveWindow") or {}
    if not isinstance(live_window, dict):
        errors.append(f"{tag}: live-window evidence missing")
    else:
        if not live_window.get("hasLaunch") or not live_window.get("hasWindow"):
            errors.append(f"{tag}: live-window launch/window missing: {live_window}")
        if live_window.get("beforeHidden") != "true":
            errors.append(f"{tag}: live-window should be hidden before click: {live_window}")
        if live_window.get("afterOpen") is not True or live_window.get("ariaOpen") != "false":
            errors.append(f"{tag}: live-window did not open via left nav: {live_window}")
        if live_window.get("expanded") != "true":
            errors.append(f"{tag}: live nav aria-expanded not true after click: {live_window}")
        if live_window.get("title") is not True or live_window.get("basis") is not True:
            errors.append(f"{tag}: live-window title/basis missing: {live_window}")
        rect = live_window.get("rect") or {}
        if not isinstance(rect, dict):
            errors.append(f"{tag}: live-window full-width rect missing: {live_window}")
        else:
            left, right_gap, rect_width, viewport = (
                rect.get("left"), rect.get("rightGap"), rect.get("width"), rect.get("viewport"))
            max_gap = 24 if tag == "desktop" else 16
            if not all(isinstance(value, int) for value in (left, right_gap, rect_width, viewport)):
                errors.append(f"{tag}: invalid live-window rect metrics: {rect}")
            else:
                left_i, right_gap_i, rect_width_i, viewport_i = (
                    cast(int, left), cast(int, right_gap), cast(int, rect_width), cast(int, viewport))
                if (left_i > max_gap or right_gap_i > max_gap or
                        rect_width_i < viewport_i - (max_gap * 2)):
                    errors.append(
                        f"{tag}: live-window is not horizontally full width enough "
                        f"(left={left_i}, rightGap={right_gap_i}, "
                        f"width={rect_width_i}, viewport={viewport_i})")
        if live_window.get("afterClose") is not False or live_window.get("ariaClose") != "true":
            errors.append(f"{tag}: live-window did not close cleanly: {live_window}")
    return errors


def main() -> int:
    source_path = Path(sys.argv[1] if len(sys.argv) > 1 else "index.html").resolve()
    source = source_path.read_text(encoding="utf-8")
    current = _current_month(source)
    target = 3 if current != 3 else 5  # 현재월→대체월 (8→3 등)

    instrumented = instrument(source, target)
    errors: list = []
    observed_widths = {}
    for width, height, tag in VIEWPORTS:
        proc = render_dom(instrumented, width, height)
        if proc.returncode != 0:
            errors.append(f"{tag}: chrome exited {proc.returncode}: {proc.stderr[-400:]}")
            continue
        result = parse_result(proc.stdout)
        observed_widths[tag] = ((result or {}).get("doc") or {}).get("iw")
        # 월 전환·소스 링크·오버플로·에러를 데스크톱과 모바일 반응형 뷰 모두 검증
        switch = {"current": current, "target": target}
        errors.extend(_check_viewport(result, width, height, tag, switch_expected=switch))

    if errors:
        print("DASHBOARD_SMOKE=RED")
        for error in errors:
            print(f"- {error}")
        return 1
    print("DASHBOARD_SMOKE=GREEN")
    print(
        f"month_switch={current}->{target}; overflow=0; "
        f"css_widths=desktop:{observed_widths.get('desktop')},"
        f"mobile:{observed_widths.get('mobile')}; "
        "source_links=present; lower_cards=green; live_window=green; "
        "live_window_full_width=green; future_negative_control=green"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
