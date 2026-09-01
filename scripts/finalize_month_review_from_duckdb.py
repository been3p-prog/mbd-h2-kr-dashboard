#!/usr/bin/env python3
"""Close one finished month in the static H2 dashboard using canonical RAW actuals."""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import html as html_lib
import json
import os
import re
import uuid
from pathlib import Path

import duckdb

from refresh_live_daily_from_duckdb import (
    DEFAULT_DUCKDB,
    DEFAULT_HTML,
    KST,
    fetch_current_revenue_snapshot,
    fetch_live_rows,
    fmt_pct,
    fmt_sum,
    fmt_won,
    summarize,
    update_current_raw_surfaces,
    update_manifest,
)

TEAMS = (
    ("일반광고", "ad_gen_won", "ad_gen"),
    ("통광마", "ad_int_won", "ad_int"),
    ("라이브", "live_won", "live"),
)

# 2026-07 predates revenue.integrated_ssot. Preserve the already-approved
# July closed values as the comparison baseline for the first canonical close.
LEGACY_PREVIOUS_BY_REVIEW_MONTH = {
    "2026-08": {
        "as_of": "2026-07-31",
        "ad_gen_won": 804_600_000,
        "ad_int_won": 54_000_000,
        "live_won": 185_000_000,
        "total_won": 1_043_600_000,
        "live_package_revenue": {
            "시그니처": 72_000_000,
            "에센셜": 64_000_000,
            "스마트": 49_000_000,
        },
    }
}


def _mom(current: int, previous: int) -> float | None:
    return None if not previous else (current / previous - 1) * 100


def _direction(value: float | None) -> tuple[str, str, str]:
    if value is None or abs(value) < 0.05:
        return "flat", "—", "—"
    if value > 0:
        return "up", "▲", f"{value:.1f}%"
    return "dn", "▼", f"{abs(value):.1f}%"


def _signed_won(value: int) -> str:
    sign = "+" if value > 0 else "-" if value < 0 else ""
    return sign + fmt_won(abs(value))


def fetch_canonical_actual_snapshot(db_path: Path, end: dt.date) -> dict:
    """Read one month-end B22N actual from revenue.integrated_ssot."""
    if end.day != calendar.monthrange(end.year, end.month)[1]:
        raise ValueError("month-end snapshot required")
    ym = end.strftime("%Y-%m")
    team_labels = {
        "일반광고": "ad_gen",
        "통합광고": "ad_int",
        "라이브커머스": "live",
    }
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        actual_rows = con.execute(
            """
            select revenue_team, round(sum(team_attributed_revenue), 0)::bigint
            from revenue.integrated_ssot
            where revenue_month = ? and include_in_mbd_revenue
            group by revenue_team
            """,
            [ym],
        ).fetchall()
        targets = {
            str(team): int(value or 0)
            for team, value in con.execute(
                "select team, value_num from meta.targets "
                "where ym = ? and metric = '매출' and kind = 'target' "
                "and team in ('ad_gen','ad_int','live')",
                [ym],
            ).fetchall()
        }
        package_rows = con.execute(
            """
            select case when package_or_slot_type = '미디어패키지 - 시그니처'
                        then '시그니처' else package_or_slot_type end package,
                   count(*)::bigint,
                   round(sum(team_attributed_revenue), 0)::bigint
            from revenue.integrated_ssot
            where revenue_month = ? and include_in_mbd_revenue
              and revenue_team = '라이브커머스'
            group by 1
            """,
            [ym],
        ).fetchall()
        has_raw_slots = con.execute(
            "select count(*) from information_schema.tables "
            "where table_schema='live' and table_name='raw_slots'"
        ).fetchone()[0]
        pgm_rows = []
        if has_raw_slots:
            pgm_rows = con.execute(
                """
                with raw as (
                    select try_cast("온에어 일자" as date) d,
                           trim("브랜드명") brand,
                           any_value("PGM") pgm
                    from live.raw_slots
                    where try_cast("온에어 일자" as date) between ? and ?
                    group by 1, 2
                ), actual as (
                    select try_cast(source_date as date) d,
                           trim(brand_name) brand,
                           case when package_or_slot_type = '미디어패키지 - 시그니처'
                                then '시그니처' else package_or_slot_type end package,
                           team_attributed_revenue revenue
                    from revenue.integrated_ssot
                    where revenue_month = ? and include_in_mbd_revenue
                      and revenue_team = '라이브커머스'
                )
                select actual.package,
                       case when coalesce(trim(raw.pgm), '') = '' then '구분 미기재'
                            when trim(raw.pgm) = '일반' then '일반'
                            else 'PGM · ' || trim(raw.pgm) end driver,
                       round(sum(actual.revenue), 0)::bigint
                from actual left join raw using(d, brand)
                group by 1, 2 order by 1, 2
                """,
                [end.replace(day=1), end, ym],
            ).fetchall()
    finally:
        con.close()

    actual_by_code = {
        team_labels[label]: int(value)
        for label, value in actual_rows
        if label in team_labels
    }
    missing_actuals = sorted(set(team_labels.values()) - set(actual_by_code))
    missing_targets = sorted(set(team_labels.values()) - set(targets))
    if missing_actuals:
        raise RuntimeError(f"canonical actual teams missing for {ym}: {','.join(missing_actuals)}")
    if missing_targets:
        raise RuntimeError(f"canonical targets missing for {ym}: {','.join(missing_targets)}")

    package_revenue = {"시그니처": 0, "에센셜": 0, "스마트": 0}
    package_count = {"시그니처": 0, "에센셜": 0, "스마트": 0}
    for package, count, revenue in package_rows:
        if package in package_revenue:
            package_revenue[package] = int(revenue)
            package_count[package] = int(count)
    pgm_revenue = {package: {} for package in package_revenue}
    for package, driver, revenue in pgm_rows:
        if package in pgm_revenue:
            pgm_revenue[package][str(driver)] = int(revenue)

    total = sum(actual_by_code.values())
    target = sum(targets.values())
    return {
        "as_of": end.isoformat(),
        "range_label": f"{end.month}/1~{end.month}/{end.day}",
        "ad_gen_won": actual_by_code["ad_gen"],
        "ad_int_won": actual_by_code["ad_int"],
        "live_won": actual_by_code["live"],
        "total_won": total,
        "target_won": target,
        "team_targets_won": targets,
        "progress_pct": total / target * 100 if target else 0,
        "live_package_revenue": package_revenue,
        "live_package_count": package_count,
        "live_pgm_revenue": pgm_revenue,
        "actual_basis": "revenue.integrated_ssot",
    }


def _element_end(text: str, start: int, tag: str = "div") -> int:
    token = re.compile(rf"<{tag}\b|</{tag}>")
    depth = 0
    for match in token.finditer(text, start):
        if match.group(0).startswith(f"<{tag}"):
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return match.end()
    raise RuntimeError(f"unterminated <{tag}> at {start}")


def _replace_element_containing(text: str, marker: str, class_start: str, replacement: str) -> str:
    marker_at = text.index(marker)
    start = text.rfind(class_start, 0, marker_at)
    if start < 0:
        raise RuntimeError(f"container not found for {marker}")
    end = _element_end(text, start)
    return text[:start] + replacement + text[end:]


def _month_bounds(text: str, group: str, month: int) -> tuple[int, int]:
    matches = list(re.finditer(rf'<div class="{re.escape(group)} mv" data-m="(\d+)"', text))
    for index, match in enumerate(matches):
        if int(match.group(1)) == month:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            return match.start(), end
    raise RuntimeError(f"month surface not found: {group} {month}")


def _actual_tip(month: int, current: dict, previous: dict) -> str:
    rows = [f'<div class="th">{month}월 · 확정 총액 · RAW 3팀</div>']
    for label, key, _ in TEAMS:
        value = int(current[key])
        mom = _mom(value, int(previous[key]))
        cls, arrow, pct = _direction(mom)
        rows.append(
            f'<div class="tr"><span>{label}</span><b><span class="tv"><span>{fmt_won(value)}</span>'
            f'<small class="{cls}">MoM {arrow} {pct}</small>'
            '</span></b></div>'
        )
    rows.append(f'<div class="tr"><span>확정 합계</span><b>{fmt_sum(current["total_won"])}</b></div>')
    rows.append(f'<div class="tr"><span>월 목표</span><b>{fmt_sum(current["target_won"])}</b></div>')
    rows.append('<div class="tn">실발생 RAW · 미래 일정·취소·무상 제외 · B22N 3팀</div>')
    return "".join(rows)


def _kpi_card(icon: str, tip: str, label: str, value: str, sub: str) -> str:
    return (
        f'<div class="kpi" data-tip="{html_lib.escape(tip, quote=True)}">{icon}<div>\n'
        f'      <div class="k">{label}</div>\n'
        f'      <div class="v num">{value}</div>\n'
        f'      <div class="s num">{sub}</div></div></div>'
    )


def _review_selector_and_phase(html: str, review_month: int) -> str:
    selector = re.search(r'(<select id="msel">)(.*?)(</select>)', html, re.S)
    if not selector:
        raise RuntimeError("month selector missing")

    def option(match: re.Match) -> str:
        month = int(match.group(1))
        prefix = match.group(2).rsplit(" · ", 1)[0]
        phase = "확정" if month <= review_month else "진행 중" if month == review_month + 1 else "부킹 진행"
        selected = " selected" if month == review_month else ""
        return f'<option value="{month}"{selected}>{prefix} · {phase}</option>'

    options = re.sub(r'<option value="(\d+)"(?: selected)?>([^<]+)</option>', option, selector.group(2))
    html = html[:selector.start()] + selector.group(1) + options + selector.group(3) + html[selector.end():]
    html, count = re.subn(r'\bvar CUR = \d+;', f'var CUR = {review_month};', html)
    if count != 1:
        raise RuntimeError("month cursor missing")

    def phase(match: re.Match) -> str:
        month = int(match.group(2))
        value = "closed" if month <= review_month else "current" if month == review_month + 1 else "future"
        return match.group(1) + value + match.group(3)

    html = re.sub(
        r'(class="(?:mvk|mvs|mvr) mv" data-m="(\d+)" data-phase=")(?:closed|cur|current|future)(")',
        phase,
        html,
    )
    return html


def _finalize_top(html: str, current: dict, previous: dict) -> str:
    month = int(current["as_of"][5:7])
    start, end = _month_bounds(html, "mvk", month)
    block = html[start:end]
    total_mom = _mom(int(current["total_won"]), int(previous["total_won"]))
    cls, arrow, pct = _direction(total_mom)
    first_label = re.search(
        rf'<div class="k">{month}월 (?:마감예상액|확정 총액)',
        block,
    )
    if not first_label:
        raise RuntimeError("headline label missing")
    label_at = first_label.start()
    card_start = block.rfind('<div class="kpi"', 0, label_at)
    card_end = _element_end(block, card_start)
    icon_match = re.search(r'<div class="ic">.*?</div>', block[card_start:card_end], re.S)
    if not icon_match:
        raise RuntimeError("headline icon missing")
    sub = f'전월 대비 <span class="pill {cls} num">{arrow} {pct}</span>'
    headline = _kpi_card(
        icon_match.group(0),
        _actual_tip(month, current, previous),
        f'{month}월 확정 총액<span class="phase">확정</span>',
        fmt_sum(current["total_won"]),
        sub,
    )
    block = block[:card_start] + headline + block[card_end:]

    gap = int(current["total_won"]) - int(current["target_won"])
    gap_tip = [f'<div class="th">{month}월 확정 GAP · 팀 기여</div>']
    for label, key, target_key in TEAMS:
        team_gap = int(current[key]) - int(current["team_targets_won"][target_key])
        gap_tip.append(f'<div class="tr"><span>{label}</span><b>{_signed_won(team_gap)}</b></div>')
    gap_tip.append('<div class="tn">GAP = 확정 RAW − 월 목표 (signed)</div>')
    marker = re.search(r'<div class="k">(?:마감예상 GAP|확정 GAP)</div>', block)
    if not marker:
        raise RuntimeError("gap label missing")
    marker_at = marker.start()
    gap_start = block.rfind('<div class="kpi"', 0, marker_at)
    gap_end = _element_end(block, gap_start)
    icon_match = re.search(r'<div class="ic">.*?</div>', block[gap_start:gap_end], re.S)
    if not icon_match:
        raise RuntimeError("gap icon missing")
    achievement = float(current["progress_pct"])
    gap_color = "var(--red)" if gap >= 0 else "var(--blue)"
    gap_cls = "up" if achievement >= 100 else "dn"
    gap_card = _kpi_card(
        icon_match.group(0),
        "".join(gap_tip),
        "확정 GAP",
        f'<span style="color:{gap_color}">{_signed_won(gap)}</span>',
        f'<span class="pill {gap_cls} num">달성률 {achievement:.1f}%</span>',
    )
    block = block[:gap_start] + gap_card + block[gap_end:]
    block = block.replace("현재 RAW 누적", "확정 RAW")
    return html[:start] + block + html[end:]


def _update_mix_tip(card: str, team_key: str, current: dict, previous: dict) -> str:
    attr = re.search(r'data-tip="([^"]*)"', card)
    if not attr:
        raise RuntimeError(f"team tooltip missing: {team_key}")
    tip = html_lib.unescape(attr.group(1))
    for bucket in ("유상", "무상", "정부지원"):
        value = int(current["mix"][team_key][bucket])
        prev = int(previous["mix"][team_key][bucket])
        cls, arrow, pct = _direction(_mom(value, prev))
        display = "0원" if value == 0 else fmt_won(value)
        row = (
            f'<div class="tr"><span>{bucket}</span><b><span class="tv"><span>{display}</span>'
            f'<small class="{cls}">MoM {arrow} {pct}</small></span></b></div>'
        )
        tip, count = re.subn(
            rf'<div class="tr"><span>{re.escape(bucket)}</span>.*?</div>',
            row,
            tip,
            count=1,
        )
        if count != 1:
            raise RuntimeError(f"team tooltip bucket missing: {team_key}/{bucket}")
    encoded = html_lib.escape(tip, quote=True)
    return card[:attr.start(1)] + encoded + card[attr.end(1):]


def _update_live_actual_tip(card: str, current: dict, previous: dict) -> str:
    attr = re.search(r'data-tip="([^"]*)"', card)
    if not attr:
        raise RuntimeError("live team tooltip missing")
    old_tip = html_lib.unescape(attr.group(1))
    count_block = re.search(
        r'<div class="cnt" data-live-progress-count="[^"]+">.*?</div>',
        old_tip,
        re.S,
    )
    if not count_block:
        raise RuntimeError("live progress count drilldown missing")
    scheduled_match = re.search(r'진행 (\d+)건', count_block.group(0))
    if not scheduled_match:
        raise RuntimeError("live scheduled count missing")
    scheduled_count = int(scheduled_match.group(1))

    month = int(current["as_of"][5:7])
    rows = [f'<div class="th">라이브 · {month}월 확정 패키지별 매출</div>', count_block.group(0)]
    for package in ("시그니처", "에센셜", "스마트"):
        old_row = re.search(
            rf'<div class="tr" data-live-package-count="{package}"><span>{package}'
            r'(?P<count><small class="[^"]+">.*?</small>)</span>.*?</div>',
            old_tip,
            re.S,
        )
        if not old_row:
            raise RuntimeError(f"live package count row missing: {package}")
        value = int(current["live_package_revenue"][package])
        prior = int(previous["live_package_revenue"][package])
        cls, arrow, pct = _direction(_mom(value, prior))
        rows.append(
            f'<div class="tr" data-live-package-count="{package}"><span>{package}'
            f'{old_row.group("count")}</span><b><span class="tv"><span>{fmt_won(value)}</span>'
            f'<small class="{cls}">MoM {arrow} {pct}</small></span></b></div>'
        )
        drivers = current.get("live_pgm_revenue", {}).get(package, {})
        if drivers:
            rows.append(f'<div class="isubs"><div class="ititle">{package} 하위</div>')
            for driver, driver_value in drivers.items():
                rows.append(f'<div class="is"><span>{driver}</span><b>{fmt_won(driver_value)}</b></div>')
            rows.append('</div>')
    recognized_count = sum(int(v) for v in current["live_package_count"].values())
    rows.append(
        f'<div class="tn">확정 매출 {recognized_count}건 · 편성 {scheduled_count}건 '
        '· 매출 = revenue.integrated_ssot</div>'
    )
    encoded = html_lib.escape("".join(rows), quote=True)
    return card[:attr.start(1)] + encoded + card[attr.end(1):]


def _finalize_teams(html: str, current: dict, previous: dict) -> str:
    month = int(current["as_of"][5:7])
    start, end = _month_bounds(html, "mvr", month)
    block = html[start:end]
    for label, key, target_key in TEAMS:
        marker = f'<span class="nm">{label}</span>'
        marker_at = block.index(marker)
        card_start = block.rfind('<div class="team"', 0, marker_at)
        card_end = _element_end(block, card_start)
        card = block[card_start:card_end]
        value = int(current[key])
        prev = int(previous[key])
        target = int(current["team_targets_won"][target_key])
        achievement = value / target * 100 if target else 0
        ring_cls = "up" if achievement >= 100 else "flat" if achievement >= 80 else "dn"
        # Preserve approved ad-team mix drilldowns. Live is rebuilt from the
        # same integrated actual rows as the headline and package totals.
        if label == "라이브":
            card = _update_live_actual_tip(card, current, previous)
        elif current.get("mix") and previous.get("mix"):
            card = _update_mix_tip(card, target_key, current, previous)
        card = re.sub(
            rf'(<span class="nm">{re.escape(label)}</span><div class="bigv num">)[^<]*(</div>)',
            rf'\g<1>{fmt_won(value)}\g<2>',
            card,
            count=1,
        )
        ring = (
            f'<span class="achv {ring_cls} num" data-achievement-ring="달성" '
            f'style="--p:{min(100, achievement):.1f}" role="img" aria-label="달성률 {achievement:.1f}%">'
            f'<span class="achv-in"><b>{achievement:.1f}%</b><small>달성률</small></span></span>'
        )
        card, ring_count = re.subn(
            r'<span class="achv [^"]+" data-achievement-ring="달성".*?</span></span>',
            ring,
            card,
            count=1,
            flags=re.S,
        )
        if ring_count != 1:
            raise RuntimeError(f"achievement ring missing: {label}")
        gap = value - target
        card = re.sub(
            r'(<div class="r"><span>GAP</span><b style="color:var\(--(?:red|blue)\)">)[^<]*(</b></div>)',
            rf'\g<1>{_signed_won(gap)}\g<2>',
            card,
            count=1,
        )
        mom = _mom(value, prev)
        cls, arrow, pct = _direction(mom)
        card = re.sub(
            r'<div class="r"><span>전월 대비</span><span class="pill [^"]+">.*?</span></div>',
            f'<div class="r"><span>전월 대비</span><span class="pill {cls} num">{arrow} {pct}</span></div>',
            card,
            count=1,
        )
        card = card.replace("RAW 누적", "확정 RAW")
        block = block[:card_start] + card + block[card_end:]
    return html[:start] + block + html[end:]


def _finalize_chart(html: str, current: dict, previous: dict) -> str:
    month = int(current["as_of"][5:7])
    match = re.search(rf'<div class="g (?:cur|current|closed)" data-m="{month}"', html)
    if not match:
        raise RuntimeError("review month chart bar missing")
    start = match.start()
    end = _element_end(html, start)
    old = html[start:end]
    target_bottom_match = re.search(r'<div class="tk" style="bottom:([0-9.]+)%"></div>', old)
    if not target_bottom_match:
        raise RuntimeError("chart target marker missing")
    target_bottom = float(target_bottom_match.group(1))
    bottoms = 0.0
    segments = []
    colors = ("#2563EB", "#14B8A6", "#A78BFA")
    for (_, key, _), color in zip(TEAMS, colors):
        height = int(current[key]) / int(current["target_won"]) * target_bottom
        segments.append(
            f'<div class="seg" style="bottom:{bottoms:.2f}%;height:{height:.2f}%;background:{color}"></div>'
        )
        bottoms += height
    gap_eok = (int(current["total_won"]) - int(current["target_won"])) / 100_000_000
    gap_label = ("+" if gap_eok > 0 else "−" if gap_eok < 0 else "") + f"{abs(gap_eok):.1f}"
    gap_class = "pos" if gap_eok > 0 else "neg" if gap_eok < 0 else "flat"
    tip = html_lib.escape(_actual_tip(month, current, previous), quote=True)
    bar = (
        f'<div class="g closed" data-m="{month}" data-tip="{tip}">'
        f'<div class="lab num">{int(current["total_won"]) / 100_000_000:.1f}</div>'
        f'<div class="glab num {gap_class}">{gap_label}</div>'
        f'<div class="trk"><div class="clip">{"".join(segments)}</div>'
        f'<div class="tk" style="bottom:{target_bottom:g}%"></div></div></div>'
    )
    return html[:start] + bar + html[end:]


def finalize_month_review(html: str, current: dict, previous: dict, *, built_at: dt.datetime | None = None) -> str:
    as_of = dt.date.fromisoformat(current["as_of"])
    if as_of.day != calendar.monthrange(as_of.year, as_of.month)[1]:
        raise ValueError("month-end snapshot required")
    previous_as_of = dt.date.fromisoformat(previous["as_of"])
    if previous_as_of != as_of.replace(day=1) - dt.timedelta(days=1):
        raise ValueError("previous month-end snapshot required")
    html = update_current_raw_surfaces(html, current)
    html = _review_selector_and_phase(html, as_of.month)
    html = html.replace(
        f"FORECAST {as_of:%Y-%m}",
        f"ACTUAL {as_of:%Y-%m}",
        1,
    )
    html = _finalize_top(html, current, previous)
    html = _finalize_teams(html, current, previous)
    html = _finalize_chart(html, current, previous)
    built = (built_at or dt.datetime.now(KST)).isoformat(timespec="seconds")
    payload = {"review_month": as_of.strftime("%Y-%m"), "actual": current, "previous": previous}
    html = update_manifest(html, built, payload, touched_sources={"revenue_mirror"}, default_month=as_of.month)
    html = html.replace("현재 RAW 매출 = DuckDB 3팀 MTD", f"{as_of.month}월 확정 RAW 매출 = DuckDB 3팀 월전체")
    return html


def _snapshot(db: Path, end: dt.date) -> dict:
    current = fetch_canonical_actual_snapshot(db, end)
    previous_end = end.replace(day=1) - dt.timedelta(days=1)
    rows, _ = fetch_live_rows(db, end.year, end.month, end_date=end)
    previous_rows, _ = fetch_live_rows(
        db, previous_end.year, previous_end.month, end_date=previous_end
    )
    current["live_quality"] = summarize(rows, previous_rows)
    return current


def _fetch_mix(db: Path, end: dt.date) -> dict:
    start = end.replace(day=1)
    con = duckdb.connect(str(db), read_only=True)
    try:
        ad_gen_rows = con.execute(
            r'''
            select case when upper(trim(coalesce(pre_issue, ''))) = 'O'
                        then '정부지원' else '유상' end bucket,
                   coalesce(sum(try_cast(regexp_replace(coalesce(revenue, '0'), '[^0-9.-]', '', 'g') as bigint)), 0)
            from ad_gen.booking_pred
            where try_cast(date as date) between ? and ?
              and ad_type = '일반광고'
              and upper(coalesce(status, '')) not in ('CANCEL', 'CANCELLED')
              and trim(coalesce(party_type, '')) in ('3P', '제3자', '판촉결합')
            group by 1
            ''',
            [start, end],
        ).fetchall()
        ad_int_rows = con.execute(
            r'''
            select case when coalesce("유형", '') like '%정부지원%' then '정부지원'
                        when coalesce("유형", '') like '%무상%' then '무상'
                        else '유상' end bucket,
                   coalesce(sum(try_cast(regexp_replace(coalesce("미셀 매출액", '0'), '[^0-9.-]', '', 'g') as bigint)), 0)
            from ad_int.contract
            where "매출 귀속월" = ?
              and try_strptime("계약 시작일", '%Y. %-m. %-d')::date <= ?
            group by 1
            ''',
            [end.strftime("%Y%m"), end],
        ).fetchall()
    finally:
        con.close()

    def buckets(rows) -> dict:
        result = {"유상": 0, "무상": 0, "정부지원": 0}
        result.update({str(bucket): int(value or 0) for bucket, value in rows})
        return result

    return {"ad_gen": buckets(ad_gen_rows), "ad_int": buckets(ad_int_rows)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duckdb", default=str(DEFAULT_DUCKDB))
    parser.add_argument("--html", default=str(DEFAULT_HTML))
    parser.add_argument("--output")
    parser.add_argument("--review-month", required=True, help="YYYY-MM")
    args = parser.parse_args()
    year, month = map(int, args.review_month.split("-"))
    end = dt.date(year, month, calendar.monthrange(year, month)[1])
    previous_end = end.replace(day=1) - dt.timedelta(days=1)
    db = Path(args.duckdb)
    source = Path(args.html)
    output = Path(args.output) if args.output else source
    current = _snapshot(db, end)
    previous = LEGACY_PREVIOUS_BY_REVIEW_MONTH.get(args.review_month)
    if previous is None:
        previous = fetch_canonical_actual_snapshot(db, previous_end)
    result = finalize_month_review(source.read_text(), current, previous)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f"{output.name}.{uuid.uuid4().hex}.partial")
    partial.write_text(result)
    os.replace(partial, output)
    print(json.dumps({
        "review_month": args.review_month,
        "total_won": current["total_won"],
        "target_won": current["target_won"],
        "achievement_pct": current["progress_pct"],
        "output": str(output),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
