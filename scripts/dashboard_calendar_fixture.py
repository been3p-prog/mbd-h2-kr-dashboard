# [2026-09-30] Test-only helper (not test_*.py — must not be picked up by the
#   `test_dashboard_*.py` discovery glob). Several fixtures pin real
#   September dates/arithmetic against the checked-in index.html's *actual*
#   current month. That coupling breaks the instant the real artifact rolls
#   to October, because dashboard_forecast_state.update_forecast_surfaces and
#   dashboard_next_booking.update_next_booking both require specific
#   data-phase="current"/"future" states that no longer hold. This module
#   forces a deterministic, self-contained calendar state onto any index.html
#   string so those fixtures stop depending on when the suite happens to run.
"""Build a deterministic month-calendar fixture from arbitrary index.html.

Unlike production `dashboard_period_state.update_default_month_state` (which
only ever advances forward and refuses to fabricate a close for a month that
isn't already accepted-closed), `synthetic_month_state` always forces every
month before the target to closed/확정 and every month after it to
future/부킹 진행 — deterministic regardless of the checked-in artifact's real
momentary phase.
"""
from __future__ import annotations

import json
import re

_LABELS = {"closed": "확정", "current": "진행 중", "future": "부킹 진행"}
_GAUGE_CLASS = {"closed": "closed", "current": "cur", "future": "future"}


def _force_month_option_and_phase(html: str, month: int, phase: str) -> str:
    label = _LABELS[phase]
    selected = " selected" if phase == "current" else ""
    html, option_count = re.subn(
        rf'<option value="{month}"(?: selected)?>([^<]+) · [^<]+</option>',
        lambda m: f'<option value="{month}"{selected}>{m.group(1)} · {label}</option>',
        html, count=1,
    )
    if option_count != 1:
        raise RuntimeError(f"month {month} selector option not found to force")
    html, phase_count = re.subn(
        rf'(class="(?:mvk|mvs|mvr) mv" data-m="{month}" data-phase=")[^"]+(")',
        rf"\g<1>{phase}\g<2>", html,
    )
    if phase_count != 3:
        raise RuntimeError(f"month {month} mv phase surfaces mismatch: {phase_count}")
    html, gauge_count = re.subn(
        rf'(class="g )[a-z]+(" data-m="{month}")',
        rf"\g<1>{_GAUGE_CLASS[phase]}\g<2>", html,
    )
    if gauge_count != 1:
        raise RuntimeError(f"month {month} annual gauge bar not found to force")
    return html


def _force_manifest_default_month(html: str, month: int) -> str:
    manifest_re = re.compile(r'(<script type="application/json" id="mbd-public-guard">)(.*?)(</script>)', re.S)
    match = manifest_re.search(html)
    if not match:
        raise RuntimeError("mbd-public-guard manifest not found")
    manifest = json.loads(match.group(2))
    manifest["default_month"] = month
    new_raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    return manifest_re.sub(lambda m: m.group(1) + new_raw + m.group(3), html, count=1)


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


def _has_future_layout(html: str, month: int) -> bool:
    top = _month_surface(html, "mvk", month) or ""
    gauge = _gauge_surface(html, month) or ""
    return (
        'data-kpi-role="booking"' in top
        and 'data-kpi-role="target"' in top
        and 'data-booking-scale=' in gauge
    )


def _copy_future_layout(html: str, target: int, template: int) -> str:
    # [2026-10-01] Copy a valid later future layout when the live artifact's next month is current.
    for group in ("mvk", "mvr"):
        current_surface = _month_surface(html, group, target)
        future_template = _month_surface(html, group, template)
        if current_surface is None or future_template is None:
            raise RuntimeError(f"month {target} future {group} layout missing")
        replacement = future_template.replace(f'data-m="{template}"', f'data-m="{target}"').replace(
            f"{template}월", f"{target}월"
        )
        html = html.replace(current_surface, replacement, 1)
    current_gauge = _gauge_surface(html, target)
    future_gauge = _gauge_surface(html, template)
    if current_gauge is None or future_gauge is None:
        raise RuntimeError(f"month {target} future gauge layout missing")
    replacement = future_gauge.replace(f'data-m="{template}"', f'data-m="{target}"').replace(
        f"{template}월", f"{target}월"
    )
    html = html.replace(current_gauge, replacement, 1)
    current_preview = _month_surface(html, "mvs", target - 1)
    template_preview = _month_surface(html, "mvs", template - 1)
    current_start = (current_preview or "").find('<div class="feature"')
    current_end = (current_preview or "").find('<div class="mini"', current_start)
    template_start = (template_preview or "").find('<div class="feature"')
    template_end = (template_preview or "").find('<div class="mini"', template_start)
    if min(current_start, current_end, template_start, template_end) < 0:
        raise RuntimeError(f"month {target - 1} next-booking feature missing")
    feature = template_preview[template_start:template_end].replace(
        f'data-next-booking-month="{template}"', f'data-next-booking-month="{target}"'
    ).replace(f"{template}월", f"{target}월")
    updated_preview = (
        current_preview[:current_start]
        + feature
        + current_preview[current_end:]
    )
    return html.replace(current_preview, updated_preview, 1)


def synthetic_month_state(html: str, month: int, *, set_default_month: bool = False) -> str:
    """Force the selector, `var CUR`, every mvk/mvs/mvr phase, and every
    annual-gauge bar class into a deterministic calendar centered on `month`:
    months before it become closed/확정, `month` becomes current/진행 중, and
    months after it become future/부킹 진행.

    `set_default_month` also forces the compact manifest's `default_month` to
    `month` — needed by any assertion (e.g. verify_dashboard.verify's
    YouTube-main cross-check) that reads the manifest to find the "current"
    month surface, rather than trusting the selector/phase state alone.
    """
    if not 1 <= month <= 12:
        raise ValueError(f"target month out of range: {month}")
    options = sorted({int(value) for value in re.findall(r'<option value="(\d+)"', html)})
    if not options:
        raise RuntimeError("month selector not found")
    if month not in options:
        raise RuntimeError(f"target month {month} is not a selector option")
    for value in options:
        phase = "closed" if value < month else "current" if value == month else "future"
        html = _force_month_option_and_phase(html, value, phase)
    html, cursor_count = re.subn(r"\bvar CUR = \d+;", f"var CUR = {month};", html)
    if cursor_count != 1:
        raise RuntimeError(f"month JS cursor count mismatch: {cursor_count}")
    next_month = month + 1
    if next_month <= 12 and not _has_future_layout(html, next_month):
        template = next(
            (value for value in options if value > next_month and _has_future_layout(html, value)),
            None,
        )
        if template is not None:
            html = _copy_future_layout(html, next_month, template)
    if set_default_month:
        html = _force_manifest_default_month(html, month)
    return html
