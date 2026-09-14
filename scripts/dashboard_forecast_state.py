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
DETAIL_BUCKETS = ("무상지원", "정부지원", "유상")


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


def _detail_rows(con, query: str, params: list) -> list[tuple]:
    """Optional drilldown tables must never make the canonical forecast unavailable."""
    try:
        return con.execute(query, params).fetchall()
    except Exception:
        return []


def fetch_forecast_breakdowns(con, as_of: dt.date) -> dict:
    """Fetch public card drilldowns from the same month/basis as each forecast."""
    start = as_of.replace(day=1)
    next_month = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    ad_gen_rows = _detail_rows(con, r'''
        select case
                 when upper(trim(coalesce(pre_issue, ''))) = 'O' then '정부지원'
                 when upper(trim(coalesce(cast(is_support as varchar), ''))) in ('O', 'Y', 'TRUE', '1') then '무상지원'
                 else '유상'
               end as bucket,
               concat_ws(' · ', coalesce(nullif(trim(brand_name), ''), '브랜드 미입력'),
                 coalesce(nullif(trim(slot_type), ''), '구좌 유형 미입력')) as item,
               count(*) as item_count,
               coalesce(sum(try_cast(regexp_replace(coalesce(revenue, '0'), '[^0-9.-]', '', 'g') as bigint)), 0) as amount
        from ad_gen.booking_pred
        where try_cast(date as date) >= ? and try_cast(date as date) < ?
          and ad_type = '일반광고'
          and upper(coalesce(status, '')) not in ('CANCEL', 'CANCELLED')
        group by 1, 2
        order by 1, amount desc, item
    ''', [start, next_month])
    ad_int_rows = _detail_rows(con, r'''
        select case
                 when coalesce("유형", '') like '%정부지원%' then '정부지원'
                 when coalesce("유형", '') like '%무상%' then '무상지원'
                 else '유상'
               end as bucket,
               concat_ws(' · ', coalesce(nullif(trim("브랜드명"), ''), '브랜드 미입력'),
                 coalesce(nullif(trim("유형"), ''), '유형 미입력')) as item,
               count(*) as item_count,
               coalesce(sum(try_cast(regexp_replace(coalesce("계약 금액", '0'), '[^0-9.-]', '', 'g') as bigint)), 0) as amount
        from ad_int.contract
        where try_strptime("계약 시작일", '%Y. %-m. %-d') >= ?
          and try_strptime("계약 시작일", '%Y. %-m. %-d') < ?
        group by 1, 2
        order by 1, amount desc, item
    ''', [start, next_month])
    live_rows = _detail_rows(con, r'''
        select coalesce(nullif(trim("패키지"), ''), '미분류') as package,
               count(*) as item_count,
               coalesce(sum(try_cast(regexp_replace(coalesce("패키지 비용", '0'), '[^0-9.-]', '', 'g') as bigint)), 0) as amount
        from live.raw_slots
        where try_cast("온에어 일자" as date) >= ? and try_cast("온에어 일자" as date) < ?
          and not regexp_matches(lower(concat_ws(' ', "패키지", "PGM", "비고 (프로모션)")), '취소|cancel')
        group by 1
        order by amount desc, package
    ''', [start, next_month])

    def bucketed(rows: list[tuple]) -> dict:
        result = {bucket: [] for bucket in DETAIL_BUCKETS}
        for bucket, item, count, amount in rows:
            result.setdefault(str(bucket), []).append({
                "label": str(item), "count": int(count or 0), "amount": int(amount or 0),
            })
        return result

    return {
        "ad_gen": {"buckets": bucketed(ad_gen_rows)},
        "ad_int": {"buckets": bucketed(ad_int_rows)},
        "live": {"packages": [
            {"label": str(package), "count": int(count or 0), "amount": int(amount or 0)}
            for package, count, amount in live_rows
        ]},
    }


def forecast_detail_total(key: str, breakdowns: dict | None) -> int | None:
    """Return the total represented by a public drilldown, if it has rows."""
    detail = (breakdowns or {}).get(key, {})
    if key == "live":
        rows = detail.get("packages", [])
    else:
        rows = [item for bucket in DETAIL_BUCKETS
                for item in detail.get("buckets", {}).get(bucket, [])]
    return sum(int(row["amount"]) for row in rows) if rows else None


def forecast_team_tip(label: str, key: str, month: int, value: int | None, *, canonical: bool,
                      breakdowns: dict | None = None) -> str:
    """Render a compact, scrollable, source-reconciled detail tooltip for one team."""
    value_text = "확인 필요" if value is None else fmt_won(value)
    basis = {
        "ad_gen": "월전체 일반광고 비취소 부킹",
        "ad_int": "계약 시작월 · 계약 금액",
        "live": "확정 편성 · 패키지 비용",
    }
    note = basis[key] + " · RAW와 분리" if canonical else PENDING if value is None else "월전체 부킹·계약 · RAW와 분리"
    rows = [f'<div class="th">{label} · {month}월 마감예상 상세</div>',
            f'<div class="tr"><span>마감예상</span><b>{value_text}</b></div>']
    detail = (breakdowns or {}).get(key, {})
    if not canonical or value is None or not detail:
        rows.append(f'<div class="tn">{note}</div>')
        return "".join(rows)

    if key == "live":
        packages = detail.get("packages", [])
        source_total = forecast_detail_total(key, breakdowns) or 0
        if packages:
            rows.append('<div class="isubs"><div class="ititle">확정 편성 · 패키지별</div>')
            rows.extend(
                f'<div class="is"><span>{html_lib.escape(row["label"])} <small class="flat">{row["count"]}건</small></span><b>{fmt_won(row["amount"])}</b></div>'
                for row in packages
            )
            rows.append('</div>')
        else:
            rows.append('<div class="isubs"><div class="ititle">확정 편성 · 패키지별</div><div class="is"><span>상세 원천 미적재</span><b>—</b></div></div>')
    else:
        buckets = detail.get("buckets", {})
        source_total = forecast_detail_total(key, breakdowns) or 0
        for bucket in DETAIL_BUCKETS:
            items = buckets.get(bucket, [])
            amount = sum(int(item["amount"]) for item in items)
            count = sum(int(item["count"]) for item in items)
            rows.append(f'<div class="tr"><span>{bucket}<small>{count}건</small></span><b>{fmt_won(amount)}</b></div>')
            if items:
                rows.append(f'<div class="isubs"><div class="ititle">{bucket} 상세</div>')
                rows.extend(
                    f'<div class="is"><span>{html_lib.escape(item["label"])} <small class="flat">{item["count"]}건</small></span><b>{fmt_won(item["amount"])}</b></div>'
                    for item in items
                )
                rows.append('</div>')
    if source_total == value:
        rows.append(f'<div class="tn">{note} · 상세 합계 {fmt_won(source_total)} 검증</div>')
    else:
        difference = int(value) - source_total
        rows.append(
            f'<div class="tn">{note} · 상세 원천 {fmt_won(source_total)} / 마감예상 {value_text} · '
            f'차이 {fmt_won(abs(difference))} {"추가" if difference > 0 else "초과"} 확인 필요</div>'
        )
    return "".join(rows)


def fetch_forecast(db_path, as_of: dt.date, *, include_next: bool = False) -> dict:
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
        breakdowns = fetch_forecast_breakdowns(con, as_of)
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
    mapping = {'일반광고': 'ad_gen', '통합광고': 'ad_int', '라이브커머스': 'live'}
    previous_teams = {key: None for _, key in TEAMS}
    for name, value in prior:
        if value is not None and math.isfinite(value) and value >= 0:
            previous_teams[mapping[name]] = round(value)
    previous_total = (sum(previous_teams.values())
                      if all(v is not None for v in previous_teams.values()) else None)
    result = {**{k: values[k] for _, k in TEAMS}, 'total_won': values['MBD_TOTAL'],
            'previous_total_won': previous_total, 'as_of': as_of.isoformat(),
            'status': 'canonical', 'source': 'revenue.v_revenue_forecast_monthly',
            'previous_actual': {'as_of': (as_of.replace(day=1) - dt.timedelta(days=1)).isoformat(),
                                'source': 'revenue.integrated_ssot',
                                'total_won': previous_total, **{k + '_won': v for k, v in previous_teams.items()}},
            'breakdowns': breakdowns}
    if include_next:
        from dashboard_next_booking import attach_next_booking
        result = attach_next_booking(db_path, as_of, result)
    return result


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
    from dashboard_team_comparison import validate_comparisons, team_comparisons
    validate_comparisons(raw, forecast)
    text = replace_top(text, month, current_cards(raw, tip, forecast=forecast if canonical else None))

    start, end = _month_bounds(text, "mvr", month)
    block = text[start:end]
    team_cards = []
    for label, key in TEAMS:
        value, target = forecast[key], raw['team_targets_won'][key]
        pct = value / target * 100 if value is not None and target else None
        value_text = display(value)
        basis = {'ad_gen': '월전체 일반광고 비취소 부킹', 'ad_int': '계약 시작월 · 계약 금액', 'live': '확정 편성 · 패키지 비용'}
        team_tip = forecast_team_tip(label, key, month, value, canonical=canonical,
                                     breakdowns=forecast.get('breakdowns'))
        detail_total = forecast_detail_total(key, forecast.get('breakdowns'))
        detail_attrs = '' if detail_total is None else (
            f' data-forecast-detail-team="{key}" data-forecast-detail-source-total="{detail_total}"'
            f' data-forecast-detail-expected="{value}"')
        team_cards.append(
            f'<div class="team" data-tip="{html_lib.escape(team_tip, quote=True)}" '
            f'data-current-forecast-team="{key}"{detail_attrs}><div class="hd2"><div class="team-main">'
            f'<span class="nm">{label}</span><div class="bigv num">{value_text}</div></div>'
            f'<span class="achv flat num" data-achievement-ring="채움" style="--p:{min(100, pct or 0):.1f}" '
            f'role="img" aria-label="예상 달성률 {fmt_pct(pct)}"><span class="achv-in">'
            f'<b>{fmt_pct(pct)}</b><small>예상 달성</small></span></span></div>'
            f'<div class="rows num"><div class="r"><span>월 목표</span><b>{fmt_won(target)}</b></div>'
            + _raw_team_row(raw[key + '_won'], target, raw['range_label'], team_key=key)
            + (f'<div class="r" data-live-attribution-warning="true"><span>RAW 귀속 확인</span><b>1P/3P 미기재 {raw["live_unknown_party_count"]}건 · 합계 제외</b></div>' if key == 'live' and raw.get('live_unknown_party_count') else '')
            + f'<div class="r"><span>기준</span><b>{basis[key] if canonical else "확인 필요" if value is None else "월전체 부킹·계약"}</b></div>'
            + team_comparisons(raw, forecast, key)
            + '</div></div>')
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
