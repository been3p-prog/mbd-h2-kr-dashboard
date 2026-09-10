"""Current-month forecast disclosure, separate from recognized RAW revenue."""
from __future__ import annotations

import datetime as dt
import html as html_lib
import re
import math

from refresh_live_daily_from_duckdb import (
    _month_bounds, _raw_team_row, fmt_pct, fmt_won,
)

TEAMS = (("일반광고", "ad_gen"), ("통광마", "ad_int"), ("라이브", "live"))
PENDING = "라이브 예상매출 집계 기준 확인 필요"


def element_end(text: str, start: int) -> int:
    depth = 0
    for token in re.finditer(r'<div\b|</div>', text[start:]):
        depth += 1 if token.group().startswith('<div') else -1
        if depth == 0:
            return start + token.end()
    raise RuntimeError("unterminated dashboard div")


def replace_div(text: str, opening: str, replacement: str) -> str:
    start = text.find(opening)
    if start < 0:
        raise RuntimeError(f"missing dashboard surface: {opening}")
    return text[:start] + replacement + text[element_end(text, start):]


def fetch_forecast(db_path, as_of: dt.date) -> dict:
    """Consume the existing canonical forecast; never substitute RAW or costs."""
    import duckdb
    expected = {
        'ad_gen': ('ad_gen.booking_pred', 'revenue', 'forecast_ad_gen_booking_v1'),
        'ad_int': ('ad_int.contract', '계약 금액', 'forecast_ad_int_contract_v1'),
        'live': ('live.booking_confirmed', '패키지 비용', 'forecast_live_booking_confirmed_v1'),
        'MBD_TOTAL': ('team_forecast_sources', 'forecast_revenue', 'forecast_mbd_total_v1'),
    }
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute('select team_code, forecast_revenue, source_table, source_column, rule_id '
                           'from revenue.v_revenue_forecast_monthly where ym = ?',
                           [as_of.strftime('%Y-%m')]).fetchall()
        previous_month = (as_of.replace(day=1) - dt.timedelta(days=1)).strftime('%Y-%m')
        prior = con.execute("select revenue_team, sum(team_attributed_revenue) from revenue.integrated_ssot "
                            "where revenue_month=? and include_in_mbd_revenue "
                            "and revenue_team in ('일반광고','통합광고','라이브커머스') group by revenue_team",
                            [previous_month]).fetchall()
    finally:
        con.close()
    values = {}
    for team, value, table, column, rule in rows:
        if team not in expected or team in values or (table, column, rule) != expected[team]:
            raise ValueError('canonical forecast provenance or duplicate team invalid')
        if value is None or not math.isfinite(value) or value < 0 or value != int(value):
            raise ValueError('canonical forecast amount invalid')
        values[team] = int(value)
    if set(values) != set(expected) or sum(values.get(k, 0) for _, k in TEAMS) != values.get('MBD_TOTAL'):
        raise ValueError('canonical forecast missing teams or total mismatch')
    previous_total = None
    if len(prior) == 3 and all(v is not None and math.isfinite(v) and v >= 0 for _, v in prior):
        previous_total = round(sum(v for _, v in prior))
    return {**{k: values[k] for _, k in TEAMS}, 'total_won': values['MBD_TOTAL'],
            'previous_total_won': previous_total, 'as_of': as_of.isoformat(),
            'status': 'canonical', 'source': 'revenue.v_revenue_forecast_monthly'}


def update_forecast_surfaces(text: str, raw: dict, forecast: dict) -> str:
    month = int(raw["as_of"][5:7])
    start, end = _month_bounds(text, "mvk", month)
    block = text[start:end]
    if 'data-phase="current"' not in block[:160]:
        raise RuntimeError("forecast updater requires current month")
    canonical = forecast.get('status') == 'canonical'
    if canonical:
        if (forecast.get('as_of') != raw['as_of'] or forecast.get('source') != 'revenue.v_revenue_forecast_monthly'
                or any(type(forecast.get(k)) is not int or forecast[k] < 0 for _, k in TEAMS)
                or sum(forecast[k] for _, k in TEAMS) != forecast.get('total_won')):
            raise ValueError('canonical forecast payload invalid')
    elif forecast.get("status") != "pending_scope" or forecast.get("live") is not None:
        raise ValueError("unapproved Live forecast policy")

    def display(value):
        return "확인 필요" if value is None else fmt_won(value)

    tip = f'<div class="th">{month}월 마감예상</div>' + ''.join(
        f'<div class="tr"><span>{label}</span><b>{display(forecast[key])}</b></div>'
        for label, key in TEAMS) + f'<div class="tn">{("기존 예측 원천 · 월전체 부킹·계약" if canonical else PENDING)} · RAW 누적과 분리</div>'
    from dashboard_kpi_cards import current_cards, replace_top
    text = replace_top(text, month, current_cards(raw, tip, forecast=forecast if canonical else None))

    start, end = _month_bounds(text, "mvr", month)
    block = text[start:end]
    team_cards = []
    for label, key in TEAMS:
        value, target = forecast[key], raw['team_targets_won'][key]
        pct = value / target * 100 if value is not None and target else None
        value_text = display(value)
        basis = {'ad_gen': '월전체 일반광고 비취소 부킹', 'ad_int': '계약 시작월 · 계약 금액', 'live': '확정 편성 · 패키지 비용'}
        note = basis[key] + ' · RAW와 분리' if canonical else PENDING if value is None else '월전체 비취소 부킹·계약 · RAW와 분리'
        team_tip = (f'<div class="th">{label} · {month}월 마감예상</div>'
                    f'<div class="tr"><span>마감예상</span><b>{value_text}</b></div>'
                    f'<div class="tn">{note}</div>')
        team_cards.append(
            f'<div class="team" data-tip="{html_lib.escape(team_tip, quote=True)}" '
            f'data-current-forecast-team="{key}"><div class="hd2"><div class="team-main">'
            f'<span class="nm">{label}</span><div class="bigv num">{value_text}</div></div>'
            f'<span class="achv flat num" data-achievement-ring="채움" style="--p:{min(100, pct or 0):.1f}" '
            f'role="img" aria-label="예상 달성률 {fmt_pct(pct)}"><span class="achv-in">'
            f'<b>{fmt_pct(pct)}</b><small>예상 달성</small></span></span></div>'
            f'<div class="rows num"><div class="r"><span>월 목표</span><b>{fmt_won(target)}</b></div>'
            + _raw_team_row(raw[key + '_won'], target, raw['range_label'], team_key=key)
            + f'<div class="r"><span>기준</span><b>{basis[key] if canonical else "확인 필요" if value is None else "월전체 부킹·계약"}</b></div>'
            '<div class="r"><span>전월 대비</span><span class="pill flat num">비교 기준 확인 필요</span></div>'
            '</div></div>')
    block = replace_div(block, '<div class="teams">', '<div class="teams">' + ''.join(team_cards) + '</div>')
    text = text[:start] + block + text[end:]

    # The shared annual revenue chart must not retain a stale complete forecast.
    bar = re.search(rf'<div class="g (?:cur|current|future)" data-m="{month}"', text)
    if not bar:
        raise RuntimeError("current revenue chart bar missing")
    old_end = element_end(text, bar.start())
    old = text[bar.start():old_end]
    target_mark = re.search(r'<div class="tk" style="bottom:[0-9.]+%"></div>', old)
    if not target_mark:
        raise RuntimeError("current revenue target line missing")
    new = (f'<div class="g cur" data-m="{month}" data-forecast-status="pending_scope" '
           f'data-tip="{html_lib.escape(tip, quote=True)}"><div class="lab num">—</div>'
           '<div class="glab num flat">확인 필요</div><div class="trk"><div class="clip"></div>'
           + target_mark.group() + '</div></div>')
    if canonical:
        total, target = forecast['total_won'], raw['target_won']
        target_pct = float(re.search(r'bottom:([0-9.]+)%', target_mark[0])[1])
        scale = target_pct / target if target else 0
        if total * scale > 100:
            raise ValueError('forecast exceeds current chart scale; rescale annual chart before publishing')
        bottom, segments = 0, []
        for (_, key), color in zip(TEAMS, ('#2563EB', '#14B8A6', '#A78BFA')):
            height = forecast[key] * scale
            segments.append(f'<div class="seg" data-forecast-team="{key}" data-forecast-won="{forecast[key]}" style="bottom:{bottom:.2f}%;height:{height:.2f}%;background:{color}"></div>')
            bottom += height
        gap = total - target
        new = (f'<div class="g cur" data-m="{month}" data-forecast-status="canonical" '
               f'data-forecast-total-won="{total}" data-tip="{html_lib.escape(tip, quote=True)}">'
               f'<div class="lab num">{total / 100000000:.1f}</div>'
               f'<div class="glab num {"pos" if gap >= 0 else "neg"}">{gap / 100000000:+.1f}</div>'
               '<div class="trk"><div class="clip">' + ''.join(segments) + '</div>' + target_mark[0] + '</div></div>')
    text = text[:bar.start()] + new + text[old_end:]

    # Adjacent closed-month comparison is copied from that month's closed RAW,
    # not from an old forecast and not from quality-population totals.
    if month > 1:
        prior_start, prior_end = _month_bounds(text, 'mvk', month - 1)
        prior = text[prior_start:prior_end]
        if 'data-phase="closed"' in prior[:160]:
            actual = re.search(r'data-closed-raw-value="([^"]+)" data-closed-achievement="([^"]+)"', prior)
            actual = actual or re.search(r'<div class="k">확정 RAW[^<]*</div><div class="v num">([^<]+)</div>\s*'
                               r'<div class="s num"><span class="pill flat num">목표 진척 ([^<]+)</span>', prior)
            if not actual:
                raise RuntimeError("closed prior-month RAW comparison missing")
            ss, ee = _month_bounds(text, 'mvs', month)
            comparison = (f'<div class="mini"><div class="k">{month-1}월 확정 RAW</div>'
                          f'<div class="v num">{actual[1]}</div><div class="s num">목표 대비 {actual[2]}</div></div>')
            updated = replace_div(text[ss:ee], '<div class="mini"', comparison)
            text = text[:ss] + updated + text[ee:]
    return text


def update_current_revenue_state(text: str, raw: dict, forecast: dict) -> str:
    """Daily refresh never reopens an explicitly accepted revenue close."""
    month = int(raw['as_of'][5:7])
    start, end = _month_bounds(text, 'mvk', month)
    if 'data-phase="closed"' in text[start:end].partition('>')[0]:
        return text
    from refresh_live_daily_from_duckdb import update_current_raw_surfaces
    return update_forecast_surfaces(update_current_raw_surfaces(text, raw), raw, forecast)
