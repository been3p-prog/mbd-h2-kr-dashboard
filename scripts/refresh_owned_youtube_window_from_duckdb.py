#!/usr/bin/env python3
"""Refresh the Owned Media / YouTube detail window from local YouTube DuckDB.

Default view is current-month cumulative YouTube Analytics. Weekly rows are
shown separately so the overlay is not frozen to one historical week.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as html_lib
import json
from pathlib import Path

import duckdb

KST = dt.timezone(dt.timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HTML = ROOT / "index.html"
DEFAULT_CONTRACT = ROOT / "data" / "owned_youtube_window_contract.json"
DEFAULT_YT_DB = Path("/Users/sb.lee/automations/youtube-view-snapshot/youtube_views.duckdb")


def esc(value) -> str:
    return html_lib.escape(str(value or ""), quote=True)


def ensure_date(value) -> dt.date:
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def fmt_m_d(day: dt.date | None) -> str:
    if not day:
        return "확인중"
    return f"{day.month}/{day.day}"


def fmt_range(start: dt.date | None, end: dt.date | None) -> str:
    if not start or not end:
        return "확인중"
    return f"{fmt_m_d(start)}–{fmt_m_d(end)}"


def fmt_count(value: float | int | None) -> str:
    value = float(value or 0)
    if abs(value) >= 10_000_000:
        text = f"{value / 10_000:.0f}만"
        return text
    if abs(value) >= 100_000:
        return f"{round(value / 10_000):,}만"
    if abs(value) >= 10_000:
        text = f"{value / 10_000:.1f}".rstrip("0").rstrip(".")
        return f"{text}만"
    return f"{round(value):,}"


def fmt_num(value: float | int | None) -> str:
    return f"{int(value or 0):,}"


def metric(label: str, value: str, em: str) -> str:
    return f'<div class="yt-kpi"><small>{esc(label)}</small><b>{esc(value)}</b><em>{esc(em)}</em></div>'


def fetch_month(con: duckdb.DuckDBPyConnection, year: int, month: int) -> dict:
    row = con.execute(
        """
        select period_start, period_end, metric_start_date, metric_end_date,
               period_complete, channel_view_count, channel_like_count,
               channel_comment_count, channel_share_count, channel_engagement_count,
               new_published_view_count, prior_published_view_count,
               unknown_publish_view_count, fetched_at, raw_status
        from v_youtube_monthly_analytics
        where period_start = ?
        order by fetched_at desc
        limit 1
        """,
        [dt.date(year, month, 1)],
    ).fetchone()
    if not row:
        raise RuntimeError(f"monthly YouTube analytics row not found for {year}-{month:02d}")
    keys = [
        "period_start", "period_end", "metric_start_date", "metric_end_date",
        "period_complete", "views", "likes", "comments", "shares", "engagement",
        "new_views", "prior_views", "unknown_views", "fetched_at", "raw_status",
    ]
    out = dict(zip(keys, row))
    for key in ("period_start", "period_end", "metric_start_date", "metric_end_date"):
        out[key] = ensure_date(out[key])
    return out


def fetch_weeks(con: duckdb.DuckDBPyConnection, month_start: dt.date, month_end: dt.date) -> list[dict]:
    rows = con.execute(
        """
        select period_start, period_end, metric_start_date, metric_end_date,
               period_complete, channel_view_count, channel_like_count,
               channel_comment_count, channel_share_count, channel_engagement_count,
               new_published_view_count, prior_published_view_count,
               unknown_publish_view_count, raw_status
        from v_youtube_weekly_analytics
        where period_start <= ? and period_end >= ?
        order by period_start
        """,
        [month_end, month_start],
    ).fetchall()
    keys = [
        "period_start", "period_end", "metric_start_date", "metric_end_date",
        "period_complete", "views", "likes", "comments", "shares", "engagement",
        "new_views", "prior_views", "unknown_views", "raw_status",
    ]
    out = []
    for row in rows:
        rec = dict(zip(keys, row))
        for key in ("period_start", "period_end", "metric_start_date", "metric_end_date"):
            rec[key] = ensure_date(rec[key])
        out.append(rec)
    return out


def fetch_publish_counts(con: duckdb.DuckDBPyConnection, start: dt.date, end: dt.date) -> dict:
    rows = con.execute(
        """
        select coalesce(form, '확인중') as form, count(*)
        from dim_video
        where publish_date >= ? and publish_date <= ? and is_active
        group by 1
        """,
        [start, end],
    ).fetchall()
    counts = {str(form): int(count) for form, count in rows}
    counts["total"] = sum(counts.values())
    return counts


def fetch_top_content(con: duckdb.DuckDBPyConnection, start: dt.date, end: dt.date, limit: int = 6) -> list[dict]:
    rows = con.execute(
        """
        with ranked as (
          select *, row_number() over(partition by video_id order by d_plus_n desc) as rn
          from v_public_dplusn_video
          where publish_date >= ? and publish_date <= ? and complete
        )
        select d_plus_n, video_id, publish_date, form, ip, title, url, views
        from ranked
        where rn = 1
        order by views desc
        limit ?
        """,
        [start, end, limit],
    ).fetchall()
    keys = ["d_plus_n", "video_id", "publish_date", "form", "ip", "title", "url", "views"]
    out = []
    for row in rows:
        rec = dict(zip(keys, row))
        rec["publish_date"] = ensure_date(rec["publish_date"])
        out.append(rec)
    return out


def kpis_for_period(row: dict, publish_counts: dict | None = None) -> list[dict]:
    kpis = [
        {"label": "조회수", "value": fmt_count(row["views"]), "em": f"{fmt_range(row['metric_start_date'], row['metric_end_date'])}"},
        {"label": "당월/당주 발행 기여", "value": fmt_count(row["new_views"]), "em": "기간 내 발행 cohort"},
        {"label": "기발행 기여", "value": fmt_count(row["prior_views"]), "em": "기존 library cohort"},
        {"label": "인게이지먼트", "value": fmt_num(row["engagement"]), "em": f"좋아요+댓글+공유"},
        {"label": "좋아요", "value": fmt_num(row["likes"]), "em": f"댓글 {fmt_num(row['comments'])}"},
        {"label": "공유", "value": fmt_num(row["shares"]), "em": f"미매칭 {fmt_count(row['unknown_views'])}"},
    ]
    if publish_counts is not None:
        kpis.insert(1, {"label": "발행", "value": f"{publish_counts.get('total', 0)}건", "em": f"LF {publish_counts.get('LF', 0)} · SF {publish_counts.get('SF', 0)}"})
    return kpis


def render_kpis(items: list[dict]) -> str:
    return "".join(metric(item["label"], item["value"], item["em"]) for item in items)


def render_week(row: dict, index: int) -> str:
    complete = "완료" if row["period_complete"] and row["views"] else "수집중"
    label = f"{fmt_range(row['period_start'], row['period_end'])} · {complete}"
    if row["views"]:
        summary = f"조회 {fmt_count(row['views'])} · 당주 {fmt_count(row['new_views'])} · 기발행 {fmt_count(row['prior_views'])}"
    else:
        summary = "Analytics weekly partial 미반영 · 월누적에는 포함 가능"
    return f'''
        <details class="week-group yt-week-group" data-yt-week-group="{index}"{'' if index != 1 else ' open'}>
          <summary data-week-toggle="yt-{index}"><span class="week-label">온드 {index}주차 <small>{esc(label)} · {esc(summary)}</small></span><span class="week-chevron" aria-hidden="true"></span></summary>
          <div class="week-items"><div class="yt-kpi-strip num yt-week-kpis">{render_kpis(kpis_for_period(row))}</div></div>
        </details>'''


def render_content_card(item: dict) -> str:
    tier = f"D+{item['d_plus_n']}"
    title = item["title"] or item["video_id"]
    meta = f"{item['form']} · {item['ip']} · {fmt_m_d(item['publish_date'])}"
    return f'''
        <article class="yt-weekly-card" data-yt-weekly-card="mtd-{esc(item['video_id'])}">
          <div class="yt-weekly-title"><div><b><a class="content-link" data-content-link="youtube" href="{esc(item['url'])}" target="_blank" rel="noopener">{esc(title)}<span aria-hidden="true">↗</span></a></b><small>{esc(meta)}</small></div><span class="yt-tier">{esc(tier)}</span></div>
          <div class="yt-weekly-metrics"><span><small>현재</small><b>{esc(fmt_num(item['views']))}</b></span><span><small>기준</small><b>public D+N</b></span><span><small>상태</small><b>complete</b></span></div>
          <div class="yt-card-note"><b>판정:</b> 월 누적 상세탭의 콘텐츠 참고 카드입니다. Highlight/Lowlight 판정은 동종 D+N 벤치마크가 있을 때만 별도 표시합니다.</div>
        </article>'''


def render_section(month: dict, weeks: list[dict], publish_counts: dict, top_content: list[dict], now: dt.datetime) -> tuple[str, dict]:
    m = month["period_start"].month
    metric_range = fmt_range(month["metric_start_date"], month["metric_end_date"])
    status = "확정" if month["period_complete"] else "진행 중"
    content_cards = "".join(render_content_card(item) for item in top_content)
    week_blocks = "".join(render_week(row, i + 1) for i, row in enumerate(weeks))
    section = f'''<section id="youtubeWindow" class="yt-window" data-yt-window="weekly-detail" data-yt-period="mtd" data-owned-media-window="youtube" data-yt-window-latest-date="{esc(str(month['metric_end_date']))}" role="dialog" aria-modal="false" aria-labelledby="youtubeWindowTitle" aria-hidden="true">
  <div class="yt-window-bar">
    <div class="yt-window-title"><b id="youtubeWindowTitle">온드미디어 상세탭</b><small>디폴트 금월 누적 · 주차별 보기 · YouTube Analytics 기준</small></div>
    <button class="yt-window-close" type="button" data-yt-close>대시보드로 돌아가기</button>
  </div>
  <div class="yt-window-body">
    <div class="yt-hero">
      <div class="yt-hero-main" aria-label="{m}월 온드미디어 금월 누적 요약">
        <span class="yt-eyebrow">{m}월 금월 누적 · {esc(metric_range)} · {esc(status)}</span>
        <h2>온드미디어도 금월 누적이 디폴트,<br>주차별로 쪼개서 확인</h2>
        <p><b>YouTube 조회수 {esc(fmt_count(month['views']))}</b>까지 반영했습니다. 상단은 월누적 Analytics, 아래는 주차별 Analytics row입니다. 콘텐츠 카드는 public D+N 참고이며, 월/주차 headline과 섞어 판정하지 않습니다.</p>
        <div class="yt-kpi-strip num" aria-label="온드미디어 금월 누적 핵심 지표">{render_kpis(kpis_for_period(month, publish_counts))}</div>
      </div>
      <aside class="yt-hero-side" aria-label="온드미디어 기준">
        <div class="yt-window-section-title">기준 분리</div>
        <div class="yt-bench-list">
          <div class="yt-bench-row"><b>디폴트</b><small>금월 누적: YouTube Analytics API period row, metric_end={esc(str(month['metric_end_date']))}.</small></div>
          <div class="yt-bench-row"><b>주차별 보기</b><small>weekly Analytics row. 현재 주차가 0/partial이면 월누적과 별도로 수집중 표시.</small></div>
          <div class="yt-bench-row"><b>콘텐츠 카드</b><small>public D+N complete snapshot. Highlight/Lowlight는 벤치마크가 있을 때만 별도 판정.</small></div>
        </div>
      </aside>
    </div>
    <div class="yt-board">
      <div class="yt-panel" aria-label="온드 월누적 판단">
        <h3>이번 달 누적 판단</h3>
        <div class="yt-insights">
          <div class="yt-insight"><span><b>월 누적과 주차별을 분리.</b> 이전처럼 특정 1주차 D+N 창으로 고정하지 않고, 디폴트는 {esc(metric_range)} 월누적입니다.</span></div>
          <div class="yt-insight"><span><b>당월 발행 vs 기발행 분리.</b> 당월 발행 기여 {esc(fmt_count(month['new_views']))}, 기발행 기여 {esc(fmt_count(month['prior_views']))}로 cohort를 분리합니다.</span></div>
          <div class="yt-insight"><span><b>partial 표시.</b> {esc(status)} 월은 metric_end까지의 누적이며, 월말 확정 전에는 확정 수치로 말하지 않습니다.</span></div>
        </div>
      </div>
      <div class="yt-panel" aria-label="온드 주차 요약">
        <h3>주차별 요약</h3>
        <div class="live-next" data-yt-week-summary="{m}">{''.join(f'<div class="step"><b>{fmt_range(w["period_start"], w["period_end"])}</b><small>조회 {fmt_count(w["views"])} · 당주 {fmt_count(w["new_views"])} · 기발행 {fmt_count(w["prior_views"])} · {"완료" if w["period_complete"] and w["views"] else "수집중"}</small></div>' for w in weeks)}</div>
      </div>
    </div>
    <div class="yt-panel yt-weekly-panel" aria-label="온드 주차별 상세">
      <div class="yt-weekly-head"><h3>주차별 보기</h3><small>Weekly = 월~일 KST. headline은 Analytics, 콘텐츠 카드는 public D+N 참고.</small></div>
      <div class="yt-weekly-accordion" data-yt-weekly-view="{m}">{week_blocks}</div>
    </div>
    <div class="yt-panel yt-weekly-panel" aria-label="D+N 콘텐츠 참고">
      <div class="yt-weekly-head"><h3>콘텐츠 D+N 참고</h3><small>완료된 public D+N snapshot만 표시. 월누적 headline과 기준을 섞지 않음.</small></div>
      <div class="yt-card-grid num">{content_cards}</div>
    </div>
    <div class="yt-source"><b>데이터 기준:</b> YouTube Analytics API monthly/weekly period rows + public D+N snapshot · fetched_at {esc(str(month['fetched_at']))} · raw_status {esc(month['raw_status'])}</div>
  </div>
</section>'''
    contract = {
        "contract_id": f"owned-youtube-window-{month['period_start'].year}-{m:02d}-mtd-v1",
        "source": {
            "duckdb": str(DEFAULT_YT_DB),
            "monthly_period": f"{month['metric_start_date']}~{month['metric_end_date']}",
            "period_complete": bool(month["period_complete"]),
            "raw_status": month["raw_status"],
        },
        "required_copy": [
            "온드미디어 상세탭", "디폴트 금월 누적", "주차별 보기", "YouTube Analytics 기준",
            "data-yt-period=\"mtd\"", "data-owned-media-window=\"youtube\"",
            f"{m}월 금월 누적", "월 누적과 주차별을 분리", "당월 발행 vs 기발행 분리",
            "콘텐츠 D+N 참고", "public D+N snapshot",
        ],
        "forbidden_in_youtube_window": [
            "LF 비포애프터가 주간 성장 대부분", "public snapshot 기준 2026-08-11 23:57 KST",
            "data-yt-weekly-card=\"beforeafter-lf-ep91\"", "data-yt-weekly-card=\"nationhome-lf-ep9\"",
        ],
        "hero_kpis": kpis_for_period(month, publish_counts),
        "weekly_rows": [
            {"period": fmt_range(w["period_start"], w["period_end"]), "views": w["views"], "complete": bool(w["period_complete"])}
            for w in weeks
        ],
        "content_cards": [item["video_id"] for item in top_content],
    }
    return section, contract


def replace_section(html: str, section: str) -> str:
    start = html.index('<section id="youtubeWindow"')
    end = html.index('\n<section id="liveWindow"', start)
    return html[:start] + section + html[end:]


def refresh(html_path: Path, contract_path: Path, db_path: Path, quiet: bool = False) -> dict:
    now = dt.datetime.now(KST)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        month = fetch_month(con, now.year, now.month)
        weeks = fetch_weeks(con, month["period_start"], month["period_end"])
        publish_counts = fetch_publish_counts(con, month["metric_start_date"], month["metric_end_date"])
        top_content = fetch_top_content(con, month["metric_start_date"], month["metric_end_date"])
    finally:
        con.close()
    section, contract = render_section(month, weeks, publish_counts, top_content, now)
    html = html_path.read_text(encoding="utf-8")
    updated = replace_section(html, section)
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
        "metric_end_date": str(month["metric_end_date"]),
        "views": month["views"],
        "published": publish_counts,
        "weeks": len(weeks),
        "content_cards": len(top_content),
    }
    if not quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", default=str(DEFAULT_HTML))
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument("--duckdb", default=str(DEFAULT_YT_DB))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    refresh(Path(args.html), Path(args.contract), Path(args.duckdb), quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
