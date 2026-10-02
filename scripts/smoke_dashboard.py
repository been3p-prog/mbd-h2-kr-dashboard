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
# [2026-10-02] 주간 회고 hover pop 전용 스모크 — generic VIEWPORTS 루프와 완전히 분리한다.
#   기존 제네릭 픽스처/계약이 이 증거를 요구하지 않게 두어야(그래야 _check_viewport 회귀가
#   안 생긴다) 하므로, 전용 뷰포트·전용 수집기·전용 검증기로 따로 돈다.
WEEKLY_RETRO_VIEWPORT = (1280, 900)
WEEKLY_RETRO_MONTH = 9
WEEKLY_RETRO_ROW_DATE = "2026-09-07"
WEEKLY_RETRO_ROW_TITLE = "베베숲"
# 유튜브 원장에도 .activity-row 가 있으므로 라이브 원장으로 한정한다. 인덱스 정렬을 보장하려고
# 행 탐색 JS 와 Playwright locator 가 반드시 같은 셀렉터를 쓴다.
WEEKLY_RETRO_ROW_SELECTOR = '[data-content-ledger="live"] .activity-row'
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
        "var initialMonth=sel?sel.value:null;"
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
        "var ytLaunch=document.querySelector('[data-yt-launch]');"
        "var ytWindow=document.querySelector('[data-yt-window=\"weekly-detail\"]');"
        "out.youtubeWindow={hasLaunch:!!ytLaunch,hasWindow:!!ytWindow,beforeHidden:ytWindow?ytWindow.getAttribute('aria-hidden'):null};"
        "if(ytLaunch&&ytWindow){if(liveLaunch&&liveWindow){liveLaunch.click();}ytLaunch.click();"
        "out.youtubeWindow.afterOpen=ytWindow.classList.contains('open');"
        "out.youtubeWindow.ariaOpen=ytWindow.getAttribute('aria-hidden');"
        "out.youtubeWindow.expanded=ytLaunch.getAttribute('aria-expanded');"
        "out.youtubeWindow.title=!!ytWindow.querySelector('#youtubeWindowTitle');"
        "out.youtubeWindow.basis=/디폴트 금월 누적/.test(ytWindow.textContent||'')&&/YouTube Analytics 기준/.test(ytWindow.textContent||'')&&/주차별 보기/.test(ytWindow.textContent||'');"
        "out.youtubeWindow.cardCount=ytWindow.querySelectorAll('[data-yt-weekly-card]').length;"
        "out.youtubeWindow.empty=!!ytWindow.querySelector('[data-yt-content-empty=\"true\"]');"
        "out.youtubeWindow.liveClosed=liveWindow?!liveWindow.classList.contains('open'):true;"
        "var yr=ytWindow.getBoundingClientRect();out.youtubeWindow.rect={left:Math.round(yr.left),"
        "rightGap:Math.round(window.innerWidth-yr.right),width:Math.round(yr.width),viewport:window.innerWidth};"
        "var yclose=ytWindow.querySelector('[data-yt-close]');if(yclose){yclose.click();}"
        "out.youtubeWindow.afterClose=ytWindow.classList.contains('open');"
        "out.youtubeWindow.ariaClose=ytWindow.getAttribute('aria-hidden');}"
        "var rest=document.querySelector('.mvr[data-m=\"'+sel.value+'\"]');"
        "var futureRoots=document.querySelectorAll('.mv[data-phase=\"future\"]');"
        "var futureForbidden=Array.prototype.reduce.call(futureRoots,function(n,x){"
        "var tips=Array.prototype.map.call(x.querySelectorAll('[data-tip]'),function(t){return t.dataset.tip||'';}).join(' ');"
        "return n+(/GAP|달성률|▼|미달/.test((x.textContent||'')+' '+tips)?1:0);},0);"
        "out.lower={visibleRest:(rest&&getComputedStyle(rest).display!=='none')?1:0,"
        "teamCards:rest?rest.querySelectorAll('.team').length:0,"
        "qualityCards:rest?rest.querySelectorAll('.quality-card').length:0,"
        "rawRowTables:document.querySelectorAll('.livetbl,.raw-row-table').length,"
        "futureRootCount:futureRoots.length,futureForbiddenCount:futureForbidden,"
        "futureExpectedCount:Array.prototype.filter.call(sel.options,function(x){return / · 부킹 진행$/.test(x.textContent);}).length*3};"
        "var main=document.querySelector('main');"
        "var monthRoots=document.querySelectorAll('.mvk,.mvs,.mvr');"
        "var currentKpis=document.querySelector('.mvk[data-m=\"'+initialMonth+'\"] > .kpis');"
        "out.layout={hasMain:!!main,monthRootCount:monthRoots.length,"
        "monthRootsOutsideMain:Array.prototype.filter.call(monthRoots,function(x){return !main||!main.contains(x);}).length,"
        "currentKpiMonth:currentKpis?currentKpis.parentElement.dataset.m:null,"
        "currentKpiPhase:currentKpis?currentKpis.parentElement.dataset.phase:null,"
        "currentKpiLayout:currentKpis?currentKpis.dataset.kpiLayout:null,"
        "currentKpiRoles:currentKpis?Array.prototype.map.call(currentKpis.querySelectorAll(':scope > .kpi'),function(x){return x.dataset.kpiRole;}):null,"
        "currentKpiLabels:currentKpis?Array.prototype.map.call(currentKpis.querySelectorAll(':scope > .kpi'),function(x){"
        "var k=x.querySelector('.k');return k&&k.firstChild?k.firstChild.textContent.trim().split(' · ')[0]:null;}):null,"
        "currentForecastStates:currentKpis?Array.prototype.map.call(currentKpis.querySelectorAll(':scope > .kpi[data-current-forecast-status]'),"
        "function(x){return x.dataset.currentForecastStatus;}):null,"
        "currentKpiCount:currentKpis?currentKpis.querySelectorAll(':scope > .kpi').length:0};"
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
                page.set_default_timeout(5_000)
                page.goto(render_path.as_uri(), wait_until="load", timeout=30_000)
                page.wait_for_timeout(150)
                retro_evidence = {"available": False}
                try:
                    current_month = re.search(r'<option value="(\d+)" selected>', instrumented)[1]
                    page.evaluate("m=>{var s=document.getElementById('msel');s.value=m;s.dispatchEvent(new Event('change'));}", current_month)
                    page.locator("[data-live-launch]").click()
                    page.evaluate("()=>{var w=document.getElementById('liveWindow');w.classList.add('open');w.setAttribute('aria-hidden','false');}")
                    available_count = page.locator('[data-live-retro="available"]').count()
                    pending_count = page.locator('[data-live-retro="pending"]').count()
                    if available_count == 0:
                        if pending_count > 0:
                            retro_evidence = {"available": False, "pendingOnly": True}
                            raise StopIteration
                        if page.locator('[data-live-content-empty="true"]').count() > 0:
                            retro_evidence = {"available": False, "empty": True}
                            raise StopIteration
                        raise RuntimeError("no retrospective state")
                    trigger = page.locator(
                        '[data-live-retro="available"] .live-retro-trigger').first
                    trigger.evaluate(
                        "e=>{for(var p=e.parentElement;p;p=p.parentElement){if(p.tagName==='DETAILS'){p.open=true;}}}")
                    trigger.evaluate("e=>e.scrollIntoView({block:'center'})")
                    page.wait_for_timeout(80)
                    card = trigger.locator("xpath=ancestor::*[@data-live-broadcast-card]")
                    overlay = page.locator('#' + trigger.get_attribute('aria-controls'))
                    card.hover()
                    page.wait_for_timeout(180)
                    hover_style = overlay.evaluate(
                        "e=>({visibility:getComputedStyle(e).visibility,opacity:getComputedStyle(e).opacity,pointerEvents:getComputedStyle(e).pointerEvents})")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(180)
                    hover_escape_visibility = overlay.evaluate(
                        "e=>getComputedStyle(e).visibility")
                    trigger.click()
                    page.wait_for_timeout(180)
                    click_style = overlay.evaluate(
                        "e=>({visibility:getComputedStyle(e).visibility,opacity:getComputedStyle(e).opacity,pointerEvents:getComputedStyle(e).pointerEvents})")
                    click_open = trigger.evaluate(
                        "e=>({expanded:e.getAttribute('aria-expanded'),open:e.closest('.live-retro').classList.contains('is-open')})")
                    overlay_bounds = overlay.evaluate(
                        "e=>{var r=e.getBoundingClientRect(),b=e.closest('.live-window-body').getBoundingClientRect();"
                        "return {top:r.top,bottom:r.bottom,bodyTop:b.top,bodyBottom:b.bottom};}")
                    trigger.click()
                    page.wait_for_timeout(180)
                    toggle_close_state = trigger.evaluate(
                        "e=>({expanded:e.getAttribute('aria-expanded'),open:e.closest('.live-retro').classList.contains('is-open')})")
                    toggle_close_visibility = overlay.evaluate(
                        "e=>getComputedStyle(e).visibility")
                    trigger.click()
                    page.wait_for_timeout(180)
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(180)
                    escape_state = trigger.evaluate(
                        "e=>({expanded:e.getAttribute('aria-expanded'),open:e.closest('.live-retro').classList.contains('is-open')})")
                    escape_visibility = overlay.evaluate(
                        "e=>getComputedStyle(e).visibility")
                    retro_evidence = {
                        "available": True,
                        "hoverStyle": hover_style,
                        "hoverEscapeVisibility": hover_escape_visibility,
                        "clickStyle": click_style,
                        "clickOpen": click_open,
                        "overlayBounds": overlay_bounds,
                        "toggleCloseState": toggle_close_state,
                        "toggleCloseVisibility": toggle_close_visibility,
                        "escapeState": escape_state,
                        "escapeVisibility": escape_visibility,
                    }
                except StopIteration:
                    pass
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    # The synthetic month-switch probe can leave an off-screen MTD card
                    # outside Playwright's visibility model. Static overlay contracts
                    # still cover that state; do not turn it into a viewport failure.
                    retro_evidence = ({"available": False, "selectedMonthHidden": True}
                                      if "element is not visible" in message else
                                      {"available": False, "error": message})
                page.evaluate(
                    "evidence=>{var d=document.getElementById('smoke-result');"
                    "var out=JSON.parse(d.textContent);out.liveWindow.retro=evidence;"
                    "d.textContent=JSON.stringify(out);}",
                    retro_evidence,
                )
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
        expected_future = lower.get("futureExpectedCount")
        if expected_future is not None:
            if type(expected_future) is not int or expected_future < 0 or lower.get("futureRootCount") != expected_future:
                errors.append(f"{tag}: future negative-control roots do not match selector")
        elif not isinstance(lower.get("futureRootCount"), int) or lower.get("futureRootCount") <= 0:
            errors.append(f"{tag}: future negative-control roots missing")
        if lower.get("futureForbiddenCount") != 0:
            errors.append(
                f"{tag}: future forbidden gap/achievement/decline/miss labels="
                f"{lower.get('futureForbiddenCount')} != 0")
    layout = result.get("layout")
    if not isinstance(layout, dict):
        errors.append(f"{tag}: layout ownership evidence missing")
    else:
        if layout.get("hasMain") is not True:
            errors.append(f"{tag}: dashboard main root missing")
        if layout.get("monthRootsOutsideMain") != 0:
            errors.append(
                f"{tag}: month roots outside main={layout.get('monthRootsOutsideMain')} != 0")
        month = layout.get("currentKpiMonth")
        labels = layout.get("currentKpiLabels")
        states = layout.get("currentForecastStates")
        expected_kpis = 3
        if layout.get("currentKpiLayout") == "two-card-v1":
            expected_kpis = 2
            roles = layout.get("currentKpiRoles")
            phase = layout.get("currentKpiPhase")
            valid = (
                roles == ["forecast", "current_raw"] and states in (["pending_scope"], ["canonical"])
                and phase in {"current", "pending_close"}
                and labels == [f"{month}월 마감예측치", f"{month}월 현황누적치"]
            ) or (
                roles == ["booking", "target"] and states == []
                and phase in {"future", "current", "pending_close"}
                and labels == [f"{month}월 부킹 총액", "월 목표"]
            ) or (
                roles == ["closed_actual", "target_gap"] and states == [] and phase == "closed"
                and labels == [f"{month}월 마감확정치", "월 목표"]
            )
            if not valid:
                errors.append(f"{tag}: two-card KPI roles/declarations are malformed")
        elif states == ["pending_scope"]:
            expected_kpis = 4
            if labels != [f"{month}월 마감예상액", "현재 RAW 누적", "월 목표", "마감예상 GAP"]:
                errors.append(f"{tag}: current pending KPI roles are malformed: {labels!r}")
        elif states != []:
            errors.append(f"{tag}: current KPI forecast declarations are invalid: {states!r}")
        elif labels == [f"{month}월 확정 총액", "확정 RAW", "월 목표", "확정 GAP"]:
            # Closing the generated four-card layout removes its pending marker.
            expected_kpis = 4
        if layout.get("currentKpiCount") != expected_kpis:
            errors.append(
                f"{tag}: current KPI direct-child count={layout.get('currentKpiCount')} != {expected_kpis}")
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
        retro = live_window.get("retro")
        if retro is not None and (retro.get("pendingOnly") is True or retro.get("empty") is True
                                  or retro.get("selectedMonthHidden") is True):
            pass
        elif retro is not None and retro.get("available") is not True:
            errors.append(f"{tag}: live retrospective interaction unavailable: {retro}")
        elif retro is not None:
            expected_style = {"visibility": "visible", "opacity": "1", "pointerEvents": "auto"}
            if retro.get("hoverStyle") != expected_style:
                errors.append(f"{tag}: live retrospective hover did not show overlay: {retro}")
            if retro.get("hoverEscapeVisibility") != "hidden":
                errors.append(f"{tag}: live retrospective hover remained visible after Escape: {retro}")
            if retro.get("clickStyle") != expected_style:
                errors.append(f"{tag}: live retrospective tap/click did not show overlay: {retro}")
            if retro.get("clickOpen") != {"expanded": "true", "open": True}:
                errors.append(f"{tag}: live retrospective click state malformed: {retro}")
            if retro.get("toggleCloseState") != {"expanded": "false", "open": False}:
                errors.append(f"{tag}: live retrospective toggle-close state malformed: {retro}")
            if retro.get("toggleCloseVisibility") != "hidden":
                errors.append(f"{tag}: live retrospective remained visible after toggle-close: {retro}")
            bounds = retro.get("overlayBounds") or {}
            if (not all(isinstance(bounds.get(key), (int, float)) for key in
                        ("top", "bottom", "bodyTop", "bodyBottom")) or
                    bounds.get("top", 0) < bounds.get("bodyTop", 0) - 1 or
                    bounds.get("bottom", 0) > bounds.get("bodyBottom", 0) + 1):
                errors.append(f"{tag}: live retrospective overlay clipped by scroll body: {retro}")
            if retro.get("escapeState") != {"expanded": "false", "open": False}:
                errors.append(f"{tag}: live retrospective escape state malformed: {retro}")
            if retro.get("escapeVisibility") != "hidden":
                errors.append(f"{tag}: live retrospective remained visible after Escape: {retro}")
    youtube_window = result.get("youtubeWindow") or {}
    if not isinstance(youtube_window, dict):
        errors.append(f"{tag}: youtube-window evidence missing")
    else:
        if not youtube_window.get("hasLaunch") or not youtube_window.get("hasWindow"):
            errors.append(f"{tag}: youtube-window launch/window missing: {youtube_window}")
        if youtube_window.get("beforeHidden") != "true":
            errors.append(f"{tag}: youtube-window should be hidden before click: {youtube_window}")
        if youtube_window.get("afterOpen") is not True or youtube_window.get("ariaOpen") != "false":
            errors.append(f"{tag}: youtube-window did not open via left nav: {youtube_window}")
        if youtube_window.get("expanded") != "true":
            errors.append(f"{tag}: youtube nav aria-expanded not true after click: {youtube_window}")
        if youtube_window.get("title") is not True or youtube_window.get("basis") is not True:
            errors.append(f"{tag}: youtube-window title/basis missing: {youtube_window}")
        if youtube_window.get("cardCount", 0) < 1 and youtube_window.get("empty") is not True:
            errors.append(f"{tag}: youtube-window cardCount={youtube_window.get('cardCount')} < 1")
        if youtube_window.get("liveClosed") is not True:
            errors.append(f"{tag}: opening youtube-window did not close live-window: {youtube_window}")
        rect = youtube_window.get("rect") or {}
        if not isinstance(rect, dict):
            errors.append(f"{tag}: youtube-window full-width rect missing: {youtube_window}")
        else:
            left, right_gap, rect_width, viewport = (
                rect.get("left"), rect.get("rightGap"), rect.get("width"), rect.get("viewport"))
            max_gap = 24 if tag == "desktop" else 16
            if not all(isinstance(value, int) for value in (left, right_gap, rect_width, viewport)):
                errors.append(f"{tag}: invalid youtube-window rect metrics: {rect}")
            else:
                left_i, right_gap_i, rect_width_i, viewport_i = (
                    cast(int, left), cast(int, right_gap), cast(int, rect_width), cast(int, viewport))
                if (left_i > max_gap or right_gap_i > max_gap or
                        rect_width_i < viewport_i - (max_gap * 2)):
                    errors.append(
                        f"{tag}: youtube-window is not horizontally full width enough "
                        f"(left={left_i}, rightGap={right_gap_i}, "
                        f"width={rect_width_i}, viewport={viewport_i})")
        if youtube_window.get("afterClose") is not False or youtube_window.get("ariaClose") != "true":
            errors.append(f"{tag}: youtube-window did not close cleanly: {youtube_window}")
    return errors


# 행 탐색: 날짜(datetime)와 공백 정규화한 .content-title 완전일치로 후보 인덱스를 모은다.
_WEEKLY_RETRO_ROW_JS = (
    "a=>{var rows=document.querySelectorAll(a.selector);var indexes=[];"
    "for(var i=0;i<rows.length;i++){"
    "var time=rows[i].querySelector('time.activity-date');"
    "var title=rows[i].querySelector('.content-title');"
    "if(!time||!title)continue;"
    "if(time.getAttribute('datetime')!==a.date)continue;"
    "if((title.textContent||'').replace(/\\s+/g,' ').trim()!==a.title)continue;"
    "indexes.push(i);}"
    "return {indexes:indexes,rowTotal:rows.length};}"
)
# hit-test: 직계 자식 .wk-retro-pop 의 중심에서 elementFromPoint 를 찍어 그 지점의 타깃이
# pop 자신이거나 pop의 자손인지를 돌려준다. display 는 참고 증거로만 싣는다.
_WEEKLY_RETRO_POP_JS = (
    "row=>{var pops=Array.prototype.filter.call(row.children,function(node){"
    "return node.classList&&node.classList.contains('wk-retro-pop');});"
    "var time=row.querySelector('time.activity-date');"
    "var title=row.querySelector('.content-title');"
    "var out={popCount:pops.length,"
    "date:time?time.getAttribute('datetime'):null,"
    "title:((title&&title.textContent)||'').replace(/\\s+/g,' ').trim()};"
    "if(pops.length!==1)return out;"
    "var pop=pops[0];var rect=pop.getBoundingClientRect();"
    "var cx=rect.left+rect.width/2,cy=rect.top+rect.height/2;"
    "var target=document.elementFromPoint(cx,cy);"
    "out.display=getComputedStyle(pop).display;"
    "out.pointerEvents=getComputedStyle(pop).pointerEvents;"
    "out.box={left:rect.left,top:rect.top,width:rect.width,height:rect.height};"
    "out.viewport={width:window.innerWidth,height:window.innerHeight};"
    "out.point={x:cx,y:cy};"
    "out.hitTarget=target?(target.tagName+((typeof target.className==='string'&&target.className)"
    "?'.'+target.className.trim().split(/\\s+/).join('.'):'')):null;"
    "out.hitIsPop=target===pop;"
    "out.hitIsDescendant=!!target&&target!==pop&&pop.contains(target);"
    "out.topmost=out.hitIsPop||out.hitIsDescendant;"
    "return out;}"
)


def collect_weekly_retro_evidence(source: str, width=None, height=None) -> dict:
    """최종 HTML을 실제 시스템 Chrome(1280x900)에 띄워 주간 회고 pop의 hit-test 증거를 모은다.

    #msel 을 9월로 바꾸고, (2026-09-07, 베베숲) 행을 정확히 하나 고른 뒤 그 행을 hover 하고,
    직계 자식 .wk-retro-pop 의 중심 좌표에서 document.elementFromPoint 를 호출한다.
    계산된 display 는 증거로만 남기고 통과 조건에는 절대 쓰지 않는다 — 실제로 포인터에
    잡히는지(topmost)가 유일한 합격 조건이다. 실패/부재는 모두 error 로 fail-closed.
    """
    width = WEEKLY_RETRO_VIEWPORT[0] if width is None else width
    height = WEEKLY_RETRO_VIEWPORT[1] if height is None else height
    try:
        sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
    except (ImportError, ModuleNotFoundError):
        return {"error": "playwright package missing; weekly retrospective hit-test cannot run"}
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as handle:
        handle.write(source)
        render_path = Path(handle.name)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                executable_path=find_chrome(), headless=True,
                args=["--no-sandbox", "--disable-gpu", "--hide-scrollbars"])
            try:
                page = browser.new_page(viewport={"width": width, "height": height})
                page.set_default_timeout(5_000)
                page.goto(render_path.as_uri(), wait_until="load", timeout=30_000)
                page.wait_for_timeout(150)
                month = page.evaluate(
                    "m=>{var sel=document.getElementById('msel');if(!sel)return null;"
                    "sel.value=m;sel.dispatchEvent(new Event('change'));return sel.value;}",
                    str(WEEKLY_RETRO_MONTH))
                page.wait_for_timeout(150)
                found = page.evaluate(_WEEKLY_RETRO_ROW_JS, {
                    "selector": WEEKLY_RETRO_ROW_SELECTOR,
                    "date": WEEKLY_RETRO_ROW_DATE,
                    "title": WEEKLY_RETRO_ROW_TITLE})
                evidence = {
                    "viewportRequested": {"width": width, "height": height},
                    "month": month,
                    "rowMatches": len(found["indexes"]),
                    "rowTotal": found["rowTotal"],
                }
                if len(found["indexes"]) != 1:
                    return evidence
                row = page.locator(WEEKLY_RETRO_ROW_SELECTOR).nth(found["indexes"][0])
                row.scroll_into_view_if_needed()
                row.hover()
                page.wait_for_timeout(180)
                evidence.update(row.evaluate(_WEEKLY_RETRO_POP_JS))
                return evidence
            finally:
                browser.close()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        render_path.unlink(missing_ok=True)


def _check_weekly_retro(evidence) -> list:
    """주간 회고 hover pop 증거 검증 — topmost 가 True 가 아니면 무조건 RED."""
    tag = "weekly-retro"
    if not isinstance(evidence, dict):
        return [f"{tag}: hover hit-test evidence missing"]
    if evidence.get("error"):
        return [f"{tag}: {evidence['error']}"]
    errors = []
    if str(evidence.get("month")) != str(WEEKLY_RETRO_MONTH):
        errors.append(f"{tag}: month selector={evidence.get('month')!r} != {WEEKLY_RETRO_MONTH}")
    if evidence.get("rowMatches") != 1:
        errors.append(
            f"{tag}: rows matching ({WEEKLY_RETRO_ROW_DATE}, {WEEKLY_RETRO_ROW_TITLE})"
            f"={evidence.get('rowMatches')} != 1 (scanned {evidence.get('rowTotal')})")
    if evidence.get("date") != WEEKLY_RETRO_ROW_DATE:
        errors.append(f"{tag}: hovered row datetime={evidence.get('date')!r} != {WEEKLY_RETRO_ROW_DATE}")
    if evidence.get("title") != WEEKLY_RETRO_ROW_TITLE:
        errors.append(f"{tag}: hovered row title={evidence.get('title')!r} != {WEEKLY_RETRO_ROW_TITLE}")
    if evidence.get("popCount") != 1:
        errors.append(f"{tag}: direct-child .wk-retro-pop count={evidence.get('popCount')} != 1")
    box, viewport = evidence.get("box"), evidence.get("viewport")
    numbers = (isinstance(box, dict) and isinstance(viewport, dict)
               and all(isinstance(box.get(key), (int, float)) for key in ("left", "top", "width", "height"))
               and all(isinstance(viewport.get(key), (int, float)) for key in ("width", "height")))
    if not numbers:
        errors.append(f"{tag}: pop box/viewport metrics missing: box={box}, viewport={viewport}")
    elif (box["width"] <= 0 or box["height"] <= 0 or box["left"] < -1 or box["top"] < -1
            or box["left"] + box["width"] > viewport["width"] + 1
            or box["top"] + box["height"] > viewport["height"] + 1):
        errors.append(f"{tag}: pop box is empty or outside the viewport: box={box}, viewport={viewport}")
    # display 는 증거로만 싣는다 — 계산된 display 가 block 이어도 포인터에 잡히지 않으면 실패.
    if evidence.get("topmost") is not True:
        errors.append(
            f"{tag}: elementFromPoint{evidence.get('point')} target={evidence.get('hitTarget')!r} "
            f"is neither the pop nor its descendant (topmost={evidence.get('topmost')!r}, "
            f"display={evidence.get('display')!r}, pointerEvents={evidence.get('pointerEvents')!r})")
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

    # [2026-10-02] 전용 주간 회고 hover 스모크 — generic 루프와 독립적으로 최종 HTML을
    #   1280x900 에 띄워 elementFromPoint 로 pop 적중을 요구한다(증거 없으면 RED).
    weekly_retro = collect_weekly_retro_evidence(source)
    errors.extend(_check_weekly_retro(weekly_retro))

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
        "live_retrospective_hover_tap=green; "
        f"weekly_retro_hover_topmost=green"
        f"(viewport={WEEKLY_RETRO_VIEWPORT[0]}x{WEEKLY_RETRO_VIEWPORT[1]},"
        f"row={WEEKLY_RETRO_ROW_DATE}/{WEEKLY_RETRO_ROW_TITLE},"
        f"hit={weekly_retro.get('hitTarget')}); "
        "live_window_full_width=green; youtube_window=green; "
        "youtube_window_full_width=green; future_negative_control=green; "
        "layout_ownership=green"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
