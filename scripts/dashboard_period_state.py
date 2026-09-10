"""Shared calendar transition: moving the cursor is never a revenue close."""
from __future__ import annotations

import re


def update_default_month_state(html: str, month: int, *, year: int | None = None) -> str:
    if not 1 <= month <= 12:
        raise ValueError(f'default month out of range: {month}')
    select = re.search(r'(<select id="msel">)(.*?)(</select>)', html, re.S)
    if not select:
        raise RuntimeError('month selector not found')
    options = list(re.finditer(r'<option value="(\d+)"(?: selected)?>([^<]+)</option>', select[2]))
    values = [int(item[1]) for item in options]
    if len(set(values)) != len(values) or values.count(month) != 1:
        raise RuntimeError('month selector target count mismatch')
    # This artifact contains one year. Never relabel last year's values as new data.
    years = set(re.findall(r'>(\d{4})년 ', select[2]))
    if year is not None and years != {str(year)}:
        raise ValueError(f'dashboard year mismatch: artifact={sorted(years)}, requested={year}')
    phases = {}
    for option in options:
        value, label = int(option[1]), option[2]
        if ' · ' not in label:
            raise RuntimeError(f'month selector label malformed: {label}')
        prior_phases = re.findall(
            rf'class="(?:mvk|mvs|mvr) mv" data-m="{value}" data-phase="([^"]+)"', html)
        # Preserve accepted historical closes, but never invent one from month order.
        sealed = label.endswith(' · 확정') and bool(prior_phases) and set(prior_phases) == {'closed'}
        phases[value] = ('closed' if sealed else 'pending_close') if value < month else (
            'current' if value == month else 'future')
    labels = {'closed': '확정', 'pending_close': '마감 확인 필요', 'current': '진행 중', 'future': '부킹 진행'}

    def option_label(match):
        value = int(match[1])
        prefix = match[2].rsplit(' · ', 1)[0]
        selected = ' selected' if value == month else ''
        return f'<option value="{value}"{selected}>{prefix} · {labels[phases[value]]}</option>'

    body = re.sub(r'<option value="(\d+)"(?: selected)?>([^<]+)</option>', option_label, select[2])
    updated = html[:select.start()] + select[1] + body + select[3] + html[select.end():]
    updated, cursor_count = re.subn(r'\bvar CUR = \d+;', f'var CUR = {month};', updated)
    if cursor_count != 1:
        raise RuntimeError(f'month JS cursor count mismatch: {cursor_count}')

    def phase_value(match):
        value = int(match[2])
        if value not in phases:
            raise RuntimeError(f'month phase missing option: {value}')
        return match[1] + phases[value] + match[3]

    updated, count = re.subn(
        r'(class="(?:mvk|mvs|mvr) mv" data-m="(\d+)" data-phase=")(?:closed|cur|current|future|pending_close)(")',
        phase_value, updated)
    if not count:
        raise RuntimeError('month phase surfaces not found')
    return updated
