#!/usr/bin/env python3
"""Refresh the dedicated Live detail window from local MBD DuckDB.

The public dashboard is static. This generator keeps the Live overlay honest:
- default hero = current-month cumulative completed rows
- weekly details = completed rows grouped by month-week
- contract JSON = exact values rendered in the overlay
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as html_lib
import json
import re
from pathlib import Path

import duckdb

KST = dt.timezone(dt.timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HTML = ROOT / "index.html"
DEFAULT_CONTRACT = ROOT / "data" / "live_window_contract.json"
DEFAULT_DUCKDB = Path("/tmp/mbd_h2_target_snapshot.duckdb")
PACKAGE_KEYS = ("시그니처", "스마트", "에센셜")


def clean_int(value) -> int:
    if value is None:
        return 0
    text = str(value).replace(",", "").replace("원", "").strip()
    if not text or text in {"-", "—", "nan", "None"}:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def ensure_date(value) -> dt.date:
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def esc(value) -> str:
    return html_lib.escape(str(value or ""), quote=True)


def fmt_m_d(day: dt.date | None) -> str:
    if not day:
        return "확인중"
    return f"{day.month}/{day.day}"


def fmt_range(rows: list[dict]) -> str:
    if not rows:
        return "확인중"
    return f"{fmt_m_d(rows[0]['date'])}–{fmt_m_d(rows[-1]['date'])}"


def fmt_won(value: float | int, *, decimals_for_eok: int = 2) -> str:
    value = float(value or 0)
    if abs(value) >= 100_000_000:
        text = f"{value / 100_000_000:.{decimals_for_eok}f}".rstrip("0").rstrip(".")
        return f"{text}억"
    if abs(value) >= 10_000:
        return f"{round(value / 10_000):,}만"
    return f"{round(value):,}"


def fmt_short_count(value: float | int) -> str:
    value = float(value or 0)
    if abs(value) >= 100_000:
        return f"{round(value / 10_000):,}만"
    if abs(value) >= 10_000:
        text = f"{value / 10_000:.1f}".rstrip("0").rstrip(".")
        return f"{text}만"
    return f"{round(value):,}"


def fmt_pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}%"


def fmt_num(value: int) -> str:
    return f"{int(value or 0):,}"


def package_key(value: str | None) -> str:
    text = str(value or "")
    for key in PACKAGE_KEYS:
        if key in text:
            return key
    return "기타"


def display_pd(value: str | None) -> str:
    mapping = {"minnie": "Minnie", "jeff": "Jeff", "jensen": "Jensen", "외주": "외주"}
    parts = [p.strip() for p in str(value or "").replace("，", ",").split(",") if p.strip()]
    if not parts:
        return "담당 확인중"
    return "/".join(mapping.get(p.lower(), mapping.get(p, p)) for p in parts)


def week_num(day: dt.date) -> int:
    return (day.day - 1) // 7 + 1


def row_is_completed(row: dict) -> bool:
    # Performance rows are considered complete only when a GMV metric exists.
    # Rows with viewer-only/zero GMV are future or not fully ingested and must not
    # drive the default cumulative performance surface.
    return row["gmv_1d"] > 0 or row["broadcast_gmv"] > 0


def fetch_rows(db_path: Path, year: int, month: int) -> tuple[list[dict], str | None]:
    start = dt.date(year, month, 1)
    next_month = dt.date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            r'''
            select
              TRY_CAST("온에어 일자" as date) as d,
              "브랜드명" as brand,
              "패키지" as package,
              "PGM" as pgm,
              "PD" as pd,
              "라이브 시청자 (비로그인 포함)" as viewers,
              "상품 클릭수" as clicks,
              "라이브 구매자수" as buyers,
              "일 전체 GMV (라이브 브랜드 전체)" as gmv_1d,
              "라이브 1H GMV" as gmv_1h,
              "방송별 데이터 GMV" as broadcast_gmv,
              "AF수취액" as af,
              "비용" as cost,
              "마진액" as margin
            from live.raw_slots
            where TRY_CAST("온에어 일자" as date) >= ?
              and TRY_CAST("온에어 일자" as date) < ?
            order by d, brand
            ''',
            [start, next_month],
        ).fetchall()
        ingest = con.execute(
            "select max(last_ingest_at) from meta.ingest_log "
            "where lower(trim(status)) in ('ok','success')"
        ).fetchone()[0]
    finally:
        con.close()

    out: list[dict] = []
    for idx, row in enumerate(rows, start=1):
        d, brand, package, pgm, pd, viewers, clicks, buyers, gmv_1d, gmv_1h, broadcast_gmv, af, cost, margin = row
        out.append({
            "source_index": idx,
            "date": ensure_date(d),
            "brand": brand or "",
            "package": package or "",
            "package_key": package_key(package),
            "pgm": pgm or "일반",
            "pd": display_pd(pd),
            "viewers": clean_int(viewers),
            "clicks": clean_int(clicks),
            "buyers": clean_int(buyers),
            "gmv_1d": clean_int(gmv_1d),
            "gmv_1h": clean_int(gmv_1h),
            "broadcast_gmv": clean_int(broadcast_gmv),
            "af": clean_int(af),
            "cost": clean_int(cost),
            "margin": clean_int(margin),
        })
    return out, str(ingest) if ingest else None


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    viewers = sum(r["viewers"] for r in rows)
    clicks = sum(r["clicks"] for r in rows)
    buyers = sum(r["buyers"] for r in rows)
    gmv_1d = sum(r["gmv_1d"] for r in rows)
    gmv_1h = sum(r["gmv_1h"] for r in rows)
    broadcast_gmv = sum(r["broadcast_gmv"] for r in rows)
    af = sum(r["af"] for r in rows)
    cost = sum(r["cost"] for r in rows)
    margin = sum(r["margin"] for r in rows)
    return {
        "n": n,
        "start": rows[0]["date"] if rows else None,
        "end": rows[-1]["date"] if rows else None,
        "viewers": viewers,
        "clicks": clicks,
        "buyers": buyers,
        "gmv_1d": gmv_1d,
        "gmv_1h": gmv_1h,
        "broadcast_gmv": broadcast_gmv,
        "af": af,
        "cost": cost,
        "margin": margin,
        "avg_viewers": viewers / n if n else 0,
        "avg_broadcast_gmv": broadcast_gmv / n if n else 0,
        "avg_gmv_1d": gmv_1d / n if n else 0,
        "click_rate": clicks / viewers * 100 if viewers else None,
        "buy_rate": buyers / viewers * 100 if viewers else None,
    }


def metric_span(label: str, value: str, em: str) -> str:
    return f'<div class="live-kpi"><small>{esc(label)}</small><b>{esc(value)}</b><em>{esc(em)}</em></div>'


def hero_kpis(summary: dict) -> list[dict]:
    return [
        {"label": "진행", "value": f"{summary['n']}건", "em": f"{fmt_range_value(summary)} 완료"},
        {"label": "1D 거래액", "value": fmt_won(summary["gmv_1d"]), "em": "브랜드 일거래액 누적"},
        {"label": "방송별 GMV", "value": fmt_won(summary["broadcast_gmv"]), "em": "방송 성과 누적"},
        {"label": "방당 GMV", "value": fmt_won(summary["avg_broadcast_gmv"]), "em": "방송별 GMV 평균"},
        {"label": "시청", "value": fmt_short_count(summary["viewers"]), "em": f"평균 {fmt_short_count(summary['avg_viewers'])}"},
        {"label": "클릭률", "value": fmt_pct(summary["click_rate"]), "em": f"클릭 {fmt_num(summary['clicks'])}"},
        {"label": "구매", "value": fmt_num(summary["buyers"]), "em": f"구매율 {fmt_pct(summary['buy_rate'], 2)}"},
        {"label": "AF/마진", "value": fmt_won(summary["af"]), "em": f"마진 {fmt_won(summary['margin'])}"},
    ]


def fmt_range_value(summary: dict) -> str:
    if not summary.get("start") or not summary.get("end"):
        return "확인중"
    return f"{fmt_m_d(summary['start'])}–{fmt_m_d(summary['end'])}"


def render_kpis(summary: dict) -> str:
    return "".join(metric_span(k["label"], k["value"], k["em"]) for k in hero_kpis(summary))


def package_lens(rows: list[dict]) -> list[dict]:
    total_viewers = sum(r["viewers"] for r in rows)
    total_bgmv = sum(r["broadcast_gmv"] for r in rows)
    lens = []
    for key in PACKAGE_KEYS:
        subset = [r for r in rows if r["package_key"] == key]
        if not subset:
            continue
        viewer_share = sum(r["viewers"] for r in subset) / total_viewers * 100 if total_viewers else 0
        bgmv_share = sum(r["broadcast_gmv"] for r in subset) / total_bgmv * 100 if total_bgmv else 0
        value = f"시청 {viewer_share:.1f}% / 방송별 GMV {bgmv_share:.1f}% · {len(subset)}건"
        lens.append({"label": key, "value": value})
    return lens


def card_id(row: dict, index: int) -> str:
    return f"mtd-{row['date'].strftime('%Y%m%d')}-{index:02d}"


def card_tag(row: dict, rank_by_1d: dict[str, int]) -> tuple[str, str]:
    cid_key = f"{row['date'].isoformat()}::{row['brand']}"
    rank = rank_by_1d.get(cid_key, 999)
    if rank <= 3:
        return " top", "누적 Top"
    if row["clicks"] and row["viewers"] and (row["clicks"] / row["viewers"] * 100) < 1:
        return " risk", "저클릭"
    if row["gmv_1d"] >= 300_000_000:
        return " top", "고1D"
    return "", row["package_key"] if row["package_key"] != "기타" else "완료"


def render_card(row: dict, index: int, rank_by_1d: dict[str, int]) -> tuple[str, dict]:
    cid = card_id(row, index)
    cls, tag = card_tag(row, rank_by_1d)
    click_rate = row["clicks"] / row["viewers"] * 100 if row["viewers"] else None
    meta = f"{fmt_m_d(row['date'])} · {row['pd']} · {row['package_key']} · {row['pgm']}"
    metrics = {
        "시청": fmt_short_count(row["viewers"]),
        "클릭률": fmt_pct(click_rate),
        "구매": fmt_num(row["buyers"]),
        "거래액": f"1D {fmt_won(row['gmv_1d'])}",
    }
    metrics_html = "".join(
        f"<span><small>{esc(label)}</small><b>{esc(value)}</b></span>"
        for label, value in metrics.items()
    )
    note = (
        f"방송별 데이터 GMV {fmt_won(row['broadcast_gmv'])} · "
        f"1H {fmt_won(row['gmv_1h']) if row['gmv_1h'] else '—'} · "
        f"AF {fmt_won(row['af'])} · 비용 {fmt_won(row['cost'])} · 마진 {fmt_won(row['margin'])}"
    )
    next_action = "해석/PD 회고는 최신 회의노트와 붙여 별도 보강. 이 카드는 RAW 수치 readback 전용."
    html = f'''
        <article class="live-broadcast-card{cls}" data-live-broadcast-card="{cid}">
          <div class="live-broadcast-title"><div><b>{esc(row['brand'])}</b><small>{esc(meta)}</small></div><span class="live-broadcast-tag">{esc(tag)}</span></div>
          <div class="live-broadcast-metrics num">{metrics_html}</div>
          <div class="live-pd-note"><b>RAW 기준</b> {esc(note)}</div>
          <div class="live-next-action"><b>회고 상태</b> {esc(next_action)}</div>
        </article>'''
    contract = {
        "brand": row["brand"],
        "meta": meta,
        "metrics": metrics,
        "source_won": {
            "일 전체 GMV (라이브 브랜드 전체)": row["gmv_1d"],
            "방송별 데이터 GMV": row["broadcast_gmv"],
            "라이브 1H GMV": row["gmv_1h"],
        },
    }
    return html, contract


def render_week_group(month: int, week: int, rows: list[dict], rank_by_1d: dict[str, int], start_index: int, *, open_latest: bool) -> tuple[str, dict, int]:
    summary = summarize(rows)
    open_attr = " open" if open_latest else ""
    week_label = f"{month}월 {week}주차"
    cards = []
    contracts = {}
    index = start_index
    for row in rows:
        card_html, card_contract = render_card(row, index, rank_by_1d)
        cid = card_id(row, index)
        cards.append(card_html)
        contracts[cid] = card_contract
        index += 1
    summary_bits = (
        f"완료 {summary['n']}건 · 1D {fmt_won(summary['gmv_1d'])} · "
        f"방송별 {fmt_won(summary['broadcast_gmv'])} · 방당 {fmt_won(summary['avg_broadcast_gmv'])}"
    )
    html = f'''
        <details class="week-group live-week-group" data-live-week-group="{month}-{week}"{open_attr}>
          <summary data-week-toggle="live-{month}-{week}"><span class="week-label">{esc(week_label)} <small>{esc(fmt_range(rows))} · {esc(summary_bits)}</small></span><span class="week-chevron" aria-hidden="true"></span></summary>
          <div class="week-items">
            <div class="live-kpi-strip num live-week-kpis">{render_kpis(summary)}</div>
            <div class="live-broadcast-grid">{''.join(cards)}</div>
          </div>
        </details>'''
    return html, contracts, index


def render_section(
    rows: list[dict],
    now: dt.datetime,
    ingest: str | None,
    db_path: Path = DEFAULT_DUCKDB,
) -> tuple[str, dict]:
    completed = [r for r in rows if row_is_completed(r)]
    completed.sort(key=lambda r: (r["date"], r["brand"]))
    summary = summarize(completed)
    latest = summary["end"]
    month = now.month

    ranked = sorted(completed, key=lambda r: r["gmv_1d"], reverse=True)
    rank_by_1d = {f"{r['date'].isoformat()}::{r['brand']}": i + 1 for i, r in enumerate(ranked)}
    lens = package_lens(completed)
    lens_html = "".join(
        f'<div class="live-mix-row"><b>{esc(item["label"])}</b><span><em>{esc(item["value"])}</em><br>패키지 믹스는 거래액/도달을 분리해서 봅니다.</span></div>'
        for item in lens
    )

    weeks: dict[int, list[dict]] = {}
    for row in completed:
        weeks.setdefault(week_num(row["date"]), []).append(row)
    latest_week = week_num(latest) if latest else None
    week_html = []
    contracts: dict[str, dict] = {}
    idx = 1
    for week in sorted(weeks):
        block, week_contracts, idx = render_week_group(month, week, weeks[week], rank_by_1d, idx, open_latest=(week == latest_week))
        week_html.append(block)
        contracts.update(week_contracts)

    top3 = ranked[:3]
    top_cards = "".join(
        f'<div class="live-case"><span class="tag">Top {i}</span><span class="brand">{esc(r["brand"])}</span><p>{esc(fmt_m_d(r["date"]))} · 1D {esc(fmt_won(r["gmv_1d"]))} · 방송별 {esc(fmt_won(r["broadcast_gmv"]))} · {esc(r["package_key"])}</p></div>'
        for i, r in enumerate(top3, start=1)
    )
    week_summary_cards = "".join(
        f'<div class="step"><b>{month}월 {w}주차</b><small>{esc(fmt_range(rs))} · 완료 {len(rs)}건 · 1D {esc(fmt_won(summarize(rs)["gmv_1d"]))} · 방당 {esc(fmt_won(summarize(rs)["avg_broadcast_gmv"]))}</small></div>'
        for w, rs in sorted(weeks.items())
    )
    weekly_content = "".join(week_html)
    if not completed:
        weekly_content = (
            '<div class="live-content-empty" data-live-content-empty="true">'
            '<b>금월 완료 방송 없음</b><small>완료·GMV 집계 대상 방송이 생기면 주차별 카드가 표시됩니다.</small>'
            '</div>'
        )
    latest_range = fmt_range_value(summary)
    ingest_note = ingest or now.isoformat(timespec="seconds")
    title = "금월 누적 성과가 디폴트,<br>주차별로 쪼개서 확인"
    paragraph = (
        f"<b>1D 거래액 {fmt_won(summary['gmv_1d'])}</b>, "
        f"<b>방송별 데이터 GMV {fmt_won(summary['broadcast_gmv'])}</b>까지 반영했습니다. "
        f"완료·GMV 확인된 {summary['n']}건 기준이며, 미집계/0원 편성은 누적 성과에서 제외했습니다. "
        f"아래 주차별 보기에서 {month}월 각 주차를 펼쳐 방송별 수치를 확인합니다."
    )

    section = f'''<section id="liveWindow" class="live-window" data-live-window="weekly-performance" data-live-period="mtd" data-live-window-latest-date="{esc(str(latest))}" role="dialog" aria-modal="false" aria-labelledby="liveWindowTitle" aria-hidden="true">
  <div class="live-window-bar">
    <div class="live-window-title"><b id="liveWindowTitle">라이브 성과 상세탭</b><small>디폴트 금월 누적 · 주차별 보기 · 거래액 기준 분리</small></div>
    <button class="live-window-close" type="button" data-live-close>대시보드로 돌아가기</button>
  </div>
  <div class="live-window-body">
    <div class="live-hero">
      <div class="live-hero-main" aria-label="{month}월 금월 누적 라이브 성과 요약">
        <span class="live-eyebrow">{month}월 금월 누적 · {esc(latest_range)} · 완료 {summary['n']}건</span>
        <h2>{title}</h2>
        <p>{paragraph}</p>
        <div class="live-kpi-strip num" aria-label="라이브 금월 누적 핵심 지표">{render_kpis(summary)}</div>
      </div>
      <aside class="live-hero-side" aria-label="누적 패키지 믹스 해석">
        <div class="live-window-section-title">누적 패키지 믹스 해석</div>
        <div class="live-pulse num">{lens_html}</div>
      </aside>
    </div>
    <div class="live-board">
      <div class="live-panel" aria-label="누적 운영 판단">
        <h3>운영 판단</h3>
        <div class="live-insights">
          <div class="live-insight"><span><b>월 누적과 주간을 분리.</b> 상단은 금월 누적, 아래는 주차별 완료 방송입니다. 1회성 8/6 스냅샷이 아니라 최신 완료일 {esc(fmt_m_d(latest))}까지 갱신합니다.</span></div>
          <div class="live-insight"><span><b>성과 기준 분리.</b> 카드 거래액은 1D 브랜드 일거래액, 월/주간 효율은 방송별 데이터 GMV, 1H는 방송 중 성과로 분리합니다.</span></div>
          <div class="live-insight"><span><b>미집계/0원 편성 제외.</b> 아직 GMV가 들어오지 않은 향후/부분수집 row는 완료 성과로 보지 않고 다음 RAW 반영 때 자동 포함합니다.</span></div>
        </div>
      </div>
      <div class="live-panel" aria-label="누적 핵심 방송과 주차 요약">
        <h3>핵심 콜아웃</h3>
        <div class="live-card-grid">{top_cards}</div>
        <div class="live-next" data-live-week-summary="{month}">{week_summary_cards}</div>
      </div>
    </div>
    <div class="live-panel live-broadcast-panel" aria-label="주차별 방송 성과">
      <div class="live-broadcast-head"><div><h3>주차별 보기 · 방송별 성과</h3><small>데이터 사용 룰: 카드 거래액=1D 브랜드 일거래액 · 월/주간 효율=방송별 데이터 GMV · 1H=방송 중 성과.</small></div><small>시청 · 클릭률 · 구매 · 1D 거래액을 한 장에서 비교</small></div>
      <div class="live-weekly-accordion" data-live-weekly-view="{month}">{weekly_content}</div>
    </div>
    <div class="live-source"><b>basis</b> 방송별 카드 거래액=`일 전체 GMV (라이브 브랜드 전체)` · 월/주간 효율=`방송별 데이터 GMV` · 1H 실시간 성과=`라이브 1H GMV` · generic GMV 표기 금지 · source DuckDB live.raw_slots · ingest {esc(ingest_note)}</div>
  </div>
</section>'''

    contract = {
        "contract_id": f"mbd-live-window-{now.year}-{month:02d}-mtd-v2",
        "source": {
            "duckdb": str(db_path),
            "schema_table": "live.raw_slots",
            "period": f"{now.year}-{month:02d}-01~{latest}",
            "latest_completed_date": str(latest),
            "completed_row_count": summary["n"],
            "sheet_rows": [],
        },
        "rules": {
            "broadcast_card_amount": {"label": "거래액", "visible_prefix": "1D", "source_column": "일 전체 GMV (라이브 브랜드 전체)"},
            "period_efficiency": {"labels": ["방송별 GMV", "방당 GMV"], "source_column": "방송별 데이터 GMV"},
            "during_live": {"label": "1H", "source_column": "라이브 1H GMV"},
        },
        "required_copy": [
            "라이브 성과 상세탭",
            "디폴트 금월 누적",
            "주차별 보기",
            "거래액 기준 분리",
            "data-live-period=\"mtd\"",
            "data-live-window-latest-date=",
            f"{month}월 금월 누적",
            f"완료 {summary['n']}건",
            "미집계/0원 편성은 누적 성과에서 제외",
            "데이터 사용 룰: 카드 거래액=1D 브랜드 일거래액 · 월/주간 효율=방송별 데이터 GMV · 1H=방송 중 성과.",
            "방송별 카드 거래액=`일 전체 GMV (라이브 브랜드 전체)`",
            "월/주간 효율=`방송별 데이터 GMV`",
            "generic GMV 표기 금지",
            *(('data-live-content-empty="true"',) if not completed else ()),
        ],
        "forbidden_in_live_window": [
            "aria-label=\"8월 1주차 라이브 성과 요약\"",
            "총액은 회복됐지만,<br>방송당 효율 회복으로 보긴 어려움",
            "data-live-broadcast-card=\"frosch\"",
            "data-live-broadcast-card=\"cuchen\"",
            "data-live-broadcast-card=\"downing\"",
            "공식 회고 전문",
            "원본 rows 302–308",
            "<small>방송GMV</small>",
        ],
        "hero_kpis": hero_kpis(summary),
        "required_narrative": [
            "월 누적과 주간을 분리",
            "상단은 금월 누적, 아래는 주차별 완료 방송",
            f"최신 완료일 {fmt_m_d(latest)}까지 갱신",
            "카드 거래액은 1D 브랜드 일거래액",
            "월/주간 효율은 방송별 데이터 GMV",
        ],
        "package_lens": lens,
        "broadcast_cards": contracts,
    }
    return section, contract


def replace_live_section(html: str, section: str) -> str:
    start = html.index('<section id="liveWindow"')
    end = html.index('\n<script>', start)
    return html[:start] + section + html[end:]


def refresh(html_path: Path, contract_path: Path, db_path: Path, quiet: bool = False) -> dict:
    now = dt.datetime.now(KST)
    rows, ingest = fetch_rows(db_path, now.year, now.month)
    section, contract = render_section(rows, now, ingest, db_path)
    html = html_path.read_text(encoding="utf-8")
    updated = replace_live_section(html, section)
    html_changed = updated != html
    contract_text = json.dumps(contract, ensure_ascii=False, indent=2, default=str) + "\n"
    old_contract = contract_path.read_text(encoding="utf-8") if contract_path.exists() else ""
    contract_changed = old_contract != contract_text
    if html_changed:
        html_path.write_text(updated, encoding="utf-8")
    if contract_changed:
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(contract_text, encoding="utf-8")
    result = {
        "html_changed": html_changed,
        "contract_changed": contract_changed,
        "latest_completed_date": contract["source"]["latest_completed_date"],
        "completed_row_count": contract["source"]["completed_row_count"],
        "hero": contract["hero_kpis"],
        "broadcast_cards": len(contract["broadcast_cards"]),
    }
    if not quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", default=str(DEFAULT_HTML))
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument("--duckdb", default=str(DEFAULT_DUCKDB))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    refresh(Path(args.html), Path(args.contract), Path(args.duckdb), quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
