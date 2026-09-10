"""Keep the next-month preview and its booking surfaces on one canonical snapshot."""
import datetime as dt
import html
import math
import re

from dashboard_kpi_cards import card, pair, replace_top
from dashboard_forecast_state import TEAMS, element_end, replace_div
from refresh_live_daily_from_duckdb import _month_bounds, fmt_won, fmt_pct


def attach_next_booking(db_path, as_of, forecast):
    if as_of.month == 12:
        return forecast  # This annual dashboard has no next-year surface.
    import duckdb
    from dashboard_forecast_state import fetch_forecast
    next_date = (as_of.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    booked = fetch_forecast(db_path, next_date)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute("select team, value_num from meta.targets where ym=? and metric='매출' "
                           "and kind='target' and team in ('ad_gen','ad_int','live')",
                           [next_date.strftime('%Y-%m')]).fetchall()
    finally:
        con.close()
    if (len(rows) != 3 or {k for k, _ in rows} != {k for _, k in TEAMS}
            or any(v is None or not math.isfinite(v) or v <= 0 or v != int(v) for _, v in rows)):
        raise ValueError('next booking targets missing or invalid')
    booked.update(month=next_date.month, target_won=sum(int(v) for _, v in rows),
                  team_targets_won={k:int(v) for k,v in rows}, snapshot_as_of=as_of.isoformat())
    return dict(forecast, next_booking=booked)


def update_next_booking(text, as_of, booked):
    month = booked['month']
    if month != as_of.month + 1 or month > 12 or booked['snapshot_as_of'] != as_of.isoformat():
        raise ValueError('next booking period mismatch')
    start, end = _month_bounds(text, 'mvk', month)
    if 'data-phase="future"' not in text[start:end].partition('>')[0]:
        raise ValueError('next booking must not overwrite current or closed revenue')
    total, target = booked['total_won'], booked['target_won']
    if sum(booked[k] for _, k in TEAMS) != total:
        raise ValueError('next booking total mismatch')
    pct = fmt_pct(total / target * 100)
    tip = f'<div class="th">차월 부킹 · {month}월</div>' + ''.join(
        f'<div class="tr"><span>{label}</span><b>{fmt_won(booked[k])}</b></div>' for label,k in TEAMS)
    tip += '<div class="tn">기존 예측 원천 · 월전체 부킹·계약 · 실적 아님</div>'
    escaped = html.escape(tip, quote=True)
    source_attrs = f' data-next-booking-target="{target}"' + ''.join(
        f' data-next-booking-{k.replace("_", "-")}="{booked[k]}"' for _,k in TEAMS)
    feature = (f'<div class="feature" data-next-booking-month="{month}" data-next-booking-total="{total}" '
               f'data-next-booking-as-of="{as_of.isoformat()}"{source_attrs} data-tip="{escaped}">'
               f'<div class="fk">차월 부킹 · {month}월</div><div class="fv num">{fmt_won(total)}</div>'
               '<div class="fs num">' + ''.join(f'<div>{label}<b>{fmt_won(booked[k])}</b></div>' for label,k in TEAMS)
               + f'</div><div class="fn num">목표 {fmt_won(target)} 대비 채움 {pct}</div></div>')
    ss, ee = _month_bounds(text, 'mvs', as_of.month)
    text = text[:ss] + replace_div(text[ss:ee], '<div class="feature"', feature) + text[ee:]
    # Derive the shared annual scale before replacing the retained target card.
    old_target = re.search(r'data-kpi-role="target".*?<div class="v num">([0-9.]+)억</div>', text[start:end], re.S)
    bar = re.search(rf'<div class="g future" data-m="{month}"', text)
    if not old_target or not bar:
        raise ValueError('next booking chart anchors missing')
    stop = element_end(text, bar.start())
    old_bar = text[bar.start():stop]
    stored_scale = re.search(r'data-booking-scale="([0-9.e+-]+)"', old_bar)
    marker = re.search(r'<div class="tk" style="bottom:([0-9.]+)%"></div>', old_bar)
    scale = float(stored_scale[1]) if stored_scale else float(marker[1]) / (float(old_target[1]) * 100000000)
    if max(total, target) * scale > 100:
        raise ValueError('next booking exceeds annual chart scale')
    segments, bottom = [], 0
    for (_, k), color in zip(TEAMS, ('#2563EB','#14B8A6','#A78BFA')):
        height = booked[k] * scale
        segments.append(f'<div class="seg" style="bottom:{bottom:.2f}%;height:{height:.2f}%;background:{color}"></div>')
        bottom += height
    chart = (f'<div class="g future" data-m="{month}" data-booking-scale="{scale}" data-tip="{escaped}">'
             f'<div class="lab num">{total / 100000000:.1f}</div><div class="glab mut2">부킹</div>'
             '<div class="trk"><div class="clip">' + ''.join(segments) + '</div>'
             f'<div class="tk" style="bottom:{target * scale:.2f}%"></div></div></div>')
    text = text[:bar.start()] + chart + text[stop:]
    text = replace_top(text, month, pair(
        card('booking', f'{month}월 부킹 총액<span class="phase">부킹 진행</span>', fmt_won(total), '월전체 부킹·계약 · 실적 아님', tip)
        + card('target', '월 목표', fmt_won(target), f'목표 채움 {pct}')))
    teams = []
    bases = {'ad_gen':'월전체 일반광고 비취소 부킹','ad_int':'계약 시작월 · 계약 금액','live':'확정 편성 · 패키지 비용'}
    for label, k in TEAMS:
        value, team_target = booked[k], booked['team_targets_won'][k]
        progress = value / team_target * 100
        team_tip = html.escape(f'<div class="th">{label} · {month}월 부킹 · 마감예상 원천</div>'
                              f'<div class="tr"><span>부킹</span><b>{fmt_won(value)}</b></div>'
                              f'<div class="tn">{bases[k]} · 실적 아님</div>', quote=True)
        teams.append(f'<div class="team" data-tip="{team_tip}"><div class="hd2"><div class="team-main">'
                     f'<span class="nm">{label}</span><div class="bigv num">{fmt_won(value)}</div></div>'
                     f'<span class="achv flat num" data-achievement-ring="채움" style="--p:{min(progress,100):.1f}" '
                     f'role="img" aria-label="채움률 {fmt_pct(progress)}"><span class="achv-in"><b>{fmt_pct(progress)}</b><small>채움률</small></span></span></div>'
                     f'<div class="rows num"><div class="r"><span>월 목표</span><b>{fmt_won(team_target)}</b></div>'
                     f'<div class="r"><span>기준</span><b>{bases[k]}</b></div></div></div>')
    ss, ee = _month_bounds(text, 'mvr', month)
    return text[:ss] + replace_div(text[ss:ee], '<div class="teams">', '<div class="teams">' + ''.join(teams) + '</div>') + text[ee:]
