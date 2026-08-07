#!/usr/bin/env python3
"""Headless-Chrome smoke test for the DOM-first MBD dashboard (LIVE artifact).

임의 HTML 경로에서 동작(기본 index.html). 월 드롭다운(#msel)으로 현재월→대체월 전환 후
location.hash 갱신·선택월 가시성 변화·소스 링크 존재·콘솔/페이지 에러 부재를 검증하고,
데스크톱 1440x900·모바일 390x844 두 뷰포트에서 가로 오버플로가 0인지 확인한다.
설치/다운로드 없이 시스템 Chrome 만 사용.
"""
from __future__ import annotations

import html as html_lib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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
        return subprocess.run(
            [
                find_chrome(),
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--hide-scrollbars",
                "--allow-file-access-from-files",
                f"--window-size={width},{height}",
                "--force-device-scale-factor=1",
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
    if not (isinstance(sw, int) and isinstance(cw, int) and sw <= cw):
        errors.append(f"{tag}: horizontal overflow docElement scrollWidth={sw} > clientWidth={cw}")
    if not (isinstance(bsw, int) and isinstance(iw, int) and bsw <= iw):
        errors.append(f"{tag}: horizontal overflow body scrollWidth={bsw} > innerWidth={iw}")
    if tag == "desktop" and iw != width:
        errors.append(f"{tag}: CSS viewport {iw}px != requested width {width}px")
    if tag == "mobile" and not (isinstance(iw, int) and 0 < iw <= 920):
        errors.append(
            f"{tag}: CSS viewport {iw}px is outside the <=920px responsive breakpoint")
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
        f"mobile:{observed_widths.get('mobile')} (requested mobile 390; Chrome min may be 500); "
        "source_links=present"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
