"""Current-month forecast disclosure, separate from recognized RAW revenue."""
from __future__ import annotations

import calendar
import datetime as dt
import html as html_lib
import re

from refresh_live_daily_from_duckdb import (
    _month_bounds, _raw_team_row, fmt_pct, fmt_won,
)

TEAMS = (("일반광고", "ad_gen"), ("통광마", "ad_int"), ("라이브", "live"))
PENDING = "라이브 예상매출 집계 기준 확인 필요"
ICON = '<div class="ic" aria-hidden="true">↗</div>'


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
    # Reuse the approved general-ad and attribution-month contract filters.
    # The Live forecast fee/party policy is not approved; absence is not zero.
    end = as_of.replace(day=calendar.monthrange(as_of.year, as_of.month)[1])
    import refresh_live_daily_from_duckdb as daily
    whole = daily.fetch_current_revenue_snapshot(db_path, end)
    return {"ad_gen": whole["ad_gen_won"], "ad_int": whole["ad_int_won"],
            "live": None, "status": "pending_scope"}


def update_forecast_surfaces(text: str, raw: dict, forecast: dict) -> str:
    month = int(raw["as_of"][5:7])
    start, end = _month_bounds(text, "mvk", month)
    block = text[start:end]
    if 'data-phase="current"' not in block[:160]:
        raise RuntimeError("forecast updater requires current month")
    if forecast.get("status") != "pending_scope" or forecast.get("live") is not None:
        raise ValueError("unapproved Live forecast policy")

    def display(value):
        return "확인 필요" if value is None else fmt_won(value)

    def card(label, value, sub, tip, attr=""):
        return (f'<div class="kpi" data-tip="{html_lib.escape(tip, quote=True)}"{attr}>'
                f'{ICON}<div><div class="k">{label}</div><div class="v num">{value}</div>'
                f'<div class="s num">{sub}</div></div></div>')

    tip = f'<div class="th">{month}월 마감예상</div>' + ''.join(
        f'<div class="tr"><span>{label}</span><b>{display(forecast[key])}</b></div>'
        for label, key in TEAMS) + f'<div class="tn">{PENDING} · RAW 누적과 분리</div>'
    # Retain the RAW card verbatim: its updater and finalizer share this contract.
    raw_marker = block.find('<div class="k">현재 RAW 누적')
    raw_start = block.rfind('<div class="kpi"', 0, raw_marker)
    if raw_marker < 0 or raw_start < 0:
        raise RuntimeError("RAW card missing before forecast update")
    raw_card = block[raw_start:element_end(block, raw_start)]
    target_tip = '<div class="th">월 목표 · 3팀 승인</div>' + ''.join(
        f'<div class="tr"><span>{label}</span><b>{fmt_won(raw["team_targets_won"][key])}</b></div>'
        for label, key in TEAMS)
    cards = card(f'{month}월 마감예상액', '확인 필요', '예상 달성률 — · 집계 기준 미확정', tip,
                 ' data-current-forecast-status="pending_scope"')
    cards += raw_card
    cards += card('월 목표', fmt_won(raw['target_won']), '3팀 승인 · 매출 목표', target_tip)
    cards += card('마감예상 GAP', '확인 필요', '예상액 − 목표 · 예상액 확인 후 산출', tip)
    block = replace_div(block, '<div class="kpis">', '<div class="kpis">' + cards + '</div>')
    text = text[:start] + block + text[end:]

    start, end = _month_bounds(text, "mvr", month)
    block = text[start:end]
    team_cards = []
    for label, key in TEAMS:
        value, target = forecast[key], raw['team_targets_won'][key]
        pct = value / target * 100 if value is not None and target else None
        value_text = display(value)
        note = PENDING if value is None else '월전체 비취소 부킹·계약 · RAW와 분리'
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
            + f'<div class="r"><span>기준</span><b>{"확인 필요" if value is None else "월전체 부킹·계약"}</b></div>'
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
    text = text[:bar.start()] + new + text[old_end:]

    # Adjacent closed-month comparison is copied from that month's closed RAW,
    # not from an old forecast and not from quality-population totals.
    if month > 1:
        prior_start, prior_end = _month_bounds(text, 'mvk', month - 1)
        prior = text[prior_start:prior_end]
        if 'data-phase="closed"' in prior[:160]:
            actual = re.search(r'<div class="k">확정 RAW[^<]*</div><div class="v num">([^<]+)</div>\s*'
                               r'<div class="s num"><span class="pill flat num">목표 진척 ([^<]+)</span>', prior)
            if not actual:
                raise RuntimeError("closed prior-month RAW comparison missing")
            ss, ee = _month_bounds(text, 'mvs', month)
            comparison = (f'<div class="mini"><div class="k">{month-1}월 확정 RAW</div>'
                          f'<div class="v num">{actual[1]}</div><div class="s num">목표 대비 {actual[2]}</div></div>')
            updated = replace_div(text[ss:ee], '<div class="mini"', comparison)
            text = text[:ss] + updated + text[ee:]
    return text
