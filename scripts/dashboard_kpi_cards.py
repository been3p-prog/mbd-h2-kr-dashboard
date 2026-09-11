"""Stable two-card presentation shared by daily refresh and canonical close."""
from __future__ import annotations

import datetime as dt
import html
import re

LAYOUT = 'two-card-v1'
ICON = '<div class="ic" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 20V10M10 20V4M16 20v-8M22 20H2"/></svg></div>'


def element_end(text: str, start: int) -> int:
    depth = 0
    for token in re.finditer(r'<div\b|</div>', text[start:]):
        depth += 1 if token.group().startswith('<div') else -1
        if depth == 0:
            return start + token.end()
    raise ValueError('unterminated KPI container')


def replace_top(text: str, month: int, cards: str) -> str:
    from refresh_live_daily_from_duckdb import _month_bounds
    start, end = _month_bounds(text, 'mvk', month)
    block = text[start:end]
    at = block.index('<div class="kpis"')
    block = block[:at] + cards + block[element_end(block, at):]
    return text[:start] + block + text[end:]


def pair(cards: str) -> str:
    return f'<div class="kpis" data-kpi-layout="{LAYOUT}">{cards}</div>'


def card(role: str, label: str, value: str, sub: str, tip: str = '', attrs: str = '') -> str:
    return (f'<div class="kpi" data-kpi-role="{role}" data-tip="{html.escape(tip, quote=True)}"{attrs}>'
            f'{ICON}<div><div class="k">{label}</div><div class="v num">{value}</div>'
            f'<div class="s num">{sub}</div></div></div>')


def mom(current: int | None, previous: int | None) -> str:
    if current is None or previous is None or previous <= 0:
        return 'MoM —'
    change = (current / previous - 1) * 100
    cls = 'up' if change > 0 else 'dn' if change < 0 else 'flat'
    arrow = '▲ ' if change > 0 else '▼ ' if change < 0 else ''
    return f'<span class="pill {cls} num">MoM {arrow}{abs(change):.1f}%</span>'


def previous_cutoff(as_of: dt.date) -> dt.date:
    previous_end = as_of.replace(day=1) - dt.timedelta(days=1)
    return previous_end.replace(day=min(as_of.day, previous_end.day))


def current_cards(raw: dict, forecast_tip: str = '', *, forecast: dict | None = None) -> str:
    from refresh_live_daily_from_duckdb import fmt_won, fmt_pct
    month = dt.date.fromisoformat(raw['as_of']).month
    previous = raw.get('previous_same_period')
    comparison = mom(raw['total_won'], previous['total_won'] if previous else None)
    if previous:
        cutoff = dt.date.fromisoformat(previous['as_of'])
        note = f'전월 동일기간 {cutoff.month}/1~{cutoff.month}/{cutoff.day} · {fmt_won(previous["total_won"])}'
        comparison_attrs = f' data-previous-as-of="{cutoff.isoformat()}" data-previous-total-won="{previous["total_won"]}"'
    else:
        note = '전월 동일기간 비교값 확인 필요'
        comparison_attrs = ''
    raw_tip = '<div class="th">RAW 누적 · ' + raw['range_label'] + '</div>' + ''.join(
        f'<div class="tr"><span>{name}</span><b>{fmt_won(raw[key + "_won"])}</b></div>'
        for name, key in [('일반광고', 'ad_gen'), ('통광마', 'ad_int'), ('라이브', 'live')])
    raw_tip += '<div class="tn">동일 매출 필터 · 전월은 현재 스냅샷으로 재집계한 동일기간이며 당시 스냅샷은 아님</div>'
    unknown = raw.get('live_unknown_party_count', 0)
    if unknown:
        raw_tip += f'<div class="tn">라이브 1P/3P 미기재 {unknown}건 · AF {fmt_won(raw["live_unknown_party_af_won"])} 귀속 확인 필요. RAW 제외이며 매출 없음이 아닙니다.</div>'
        note += f' · 라이브 귀속 확인 {unknown}건'
    attrs = f' data-current-as-of="{raw["as_of"]}" data-current-total-won="{raw["total_won"]}"' + comparison_attrs
    if raw['total_won'] == 0:
        attrs += ' data-current-raw-empty="true"'
    forecast_value, forecast_sub = '확인 필요', 'MoM — · 예상 달성률 —'
    forecast_attrs = ' data-current-forecast-status="pending_scope"'
    if forecast:
        total, target = forecast['total_won'], raw['target_won']
        forecast_value = fmt_won(total)
        forecast_sub = (f'{mom(total, forecast.get("previous_total_won"))} · 예상 달성률 '
                        + fmt_pct(total / target * 100 if target else None))
        forecast_attrs = (f' data-current-forecast-status="canonical" data-forecast-total-won="{total}"'
                          f' data-forecast-target-won="{target}" data-forecast-as-of="{raw["as_of"]}"'
                          f' data-forecast-source="revenue.v_revenue_forecast_monthly"')
        for key in ('ad_gen', 'ad_int', 'live'):
            forecast_attrs += f' data-forecast-{key.replace("_", "-")}-won="{forecast[key]}"'
        if forecast.get('previous_total_won') is not None:
            forecast_attrs += f' data-forecast-previous-won="{forecast["previous_total_won"]}"'
    return pair(
        card('forecast', f'{month}월 마감예측치', forecast_value,
             f'{forecast_sub}<small>전월 확정치 대비 · 월 목표 {fmt_won(raw["target_won"])}</small>',
             forecast_tip or '<div class="tn">라이브 예상매출 집계 기준 확인 필요</div>',
             forecast_attrs) +
        card('current_raw', f'{month}월 현황누적치', fmt_won(raw['total_won']),
             f'{comparison}<small>현재 RAW 누적 · {raw["range_label"]}</small><small>{note}</small>', raw_tip, attrs))


def closed_cards(current: dict, previous: dict, tip: str) -> str:
    from refresh_live_daily_from_duckdb import fmt_won, fmt_pct
    month = dt.date.fromisoformat(current['as_of']).month
    total, target = current['total_won'], current['target_won']
    gap = total - target
    achievement = fmt_pct(total / target * 100 if target else None)
    return pair(
        card('closed_actual', f'{month}월 마감확정치', fmt_won(total),
             f'{mom(total, previous["total_won"])} · 달성률 {achievement}<small>확정 RAW · {current["range_label"]}</small>',
             tip, f' data-closed-raw-value="{fmt_won(total)}" data-closed-achievement="{achievement}"') +
        card('target_gap', '월 목표', fmt_won(target),
             f'확정 GAP {"+" if gap > 0 else "-" if gap < 0 else ""}{fmt_won(abs(gap))}<small>확정 매출 − 월 목표</small>'))


def normalize_legacy_tops(text: str) -> str:
    """Preserve accepted historical values/tooltips; migrate layout only, once."""
    from refresh_live_daily_from_duckdb import _month_bounds
    for month in range(1, 13):
        start, end = _month_bounds(text, 'mvk', month)
        block = text[start:end]
        if f'data-kpi-layout="{LAYOUT}"' in block:
            if 'data-kpi-role="closed_actual"' in block:
                block = block.replace('전월 대비', 'MoM')
                text = text[:start] + block + text[end:]
            continue
        phase = re.search(r'data-phase="([^"]+)"', block)
        if not phase or phase[1] not in {'closed', 'future'}:
            continue
        at = block.index('<div class="kpis"')
        container = block[at:element_end(block, at)]
        cards, pos = [], container.index('>') + 1
        while True:
            pos = container.find('<div class="kpi"', pos)
            if pos < 0:
                break
            stop = element_end(container, pos)
            cards.append(container[pos:stop])
            pos = stop
        def matching(label):
            return next((c for c in cards if re.search(r'<div class="k">' + label, c)), None)
        def field(c, cls):
            match = re.search(rf'<div class="{cls}"[^>]*>(.*?)</div>', c or '', re.S)
            if not match:
                raise ValueError(f'missing legacy KPI {month}: {cls}')
            return match[1]
        first, target = cards[0], matching('월 목표')
        if not target:
            raise ValueError(f'missing legacy target: {month}')
        first_tip = html.unescape(re.search(r'data-tip="([^"]*)"', first)[1])
        if phase[1] == 'closed':
            raw = matching('확정 RAW')
            gap = matching('확정 GAP')
            if not gap:
                raise ValueError(f'missing legacy close GAP: {month}')
            value = field(raw or first, 'v num')
            achievement = re.search(r'달성률 ([0-9.]+%)', gap)
            pct = achievement[1] if achievement else '—'
            left = card('closed_actual', f'{month}월 마감확정치', value,
                        field(first, 's num').replace('전월 대비', 'MoM') + f' · 달성률 {pct}', first_tip,
                        f' data-closed-raw-value="{value}" data-closed-achievement="{pct}"')
            right = card('target_gap', '월 목표', field(target, 'v num'),
                         '확정 GAP ' + field(gap, 'v num') + '<small>확정 매출 − 월 목표</small>')
        else:
            left = card('booking', field(first, 'k'), field(first, 'v num'), '미래월 부킹 스냅샷 · 실적 아님', first_tip)
            right = card('target', '월 목표', field(target, 'v num'), '월 시작 후 현황 집계')
        text = replace_top(text, month, pair(left + right))
    return text
