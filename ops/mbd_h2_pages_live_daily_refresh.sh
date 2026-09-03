#!/usr/bin/env bash
set -euo pipefail

ROOT="${MBD_H2_ROOT:-/Users/sb.lee/automations/mbd-h2-kr-dashboard}"
LOG_DIR="${MBD_H2_LOG_DIR:-$ROOT/logs}"
LOCK_DIR="/tmp/mbd_h2_pages_live_daily_refresh.lock"
RETRY_LIB="${MBD_H2_RETRY_LIB:-$ROOT/ops/mbd_h2_pages_retry.sh}"
# [2026-08-28] Pin the MBD interpreter — Hermes gateway venv does not include duckdb.
PY="${MBD_H2_PYTHON:-/usr/bin/python3}"
YT_SNAPSHOT_CACHE="/tmp/mbd_h2_youtube_target_snapshot.duckdb"
MBD_SNAPSHOT_CACHE="/tmp/mbd_h2_target_snapshot.duckdb"
# [2026-08-28] Bound transient recovery; deterministic failures remain fail-fast.
export MBD_H2_MAX_ATTEMPTS="${MBD_H2_MAX_ATTEMPTS:-3}"
export MBD_H2_RETRY_SLEEP_SECONDS="${MBD_H2_RETRY_SLEEP_SECONDS:-30}"
export MBD_H2_MAX_RUNTIME_SECONDS="${MBD_H2_MAX_RUNTIME_SECONDS:-1800}"
export MBD_H2_RUN_STARTED_EPOCH="$(/bin/date +%s)"
CURRENT_STAGE="bootstrap"

if [[ ! -r "$RETRY_LIB" ]]; then
  echo "ERROR: MBD H2 retry library not found: $RETRY_LIB"
  exit 1
fi
# shellcheck source=ops/mbd_h2_pages_retry.sh
source "$RETRY_LIB"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "ERROR: another MBD H2 Pages refresh is already running"
  exit 1
fi
mkdir -p "$LOG_DIR"
STAMP="$(date '+%Y%m%d_%H%M%S')"
LOG="$LOG_DIR/daily_live_refresh_${STAMP}.log"
exec 3>&1 4>&2
exec >"$LOG" 2>&1

cleanup() {
  local rc=$?
  trap - EXIT
  rmdir "$LOCK_DIR" 2>/dev/null || true
  if [[ "$rc" -ne 0 ]]; then
    printf 'ERROR: MBD H2 Pages refresh failed after bounded retry stage=%s rc=%s log=%s\n' \
      "$CURRENT_STAGE" "$rc" "$LOG" >&4
  fi
  return "$rc"
}
trap cleanup EXIT

cd "$ROOT"

# Never erase an operator/hotfix worktree. A dirty tree is a collision,
# not permission for an unattended reset.
CURRENT_STAGE="worktree_preflight"
if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  echo "ERROR: refusing refresh because the Pages worktree is dirty"
  git status --short
  exit 1
fi

# Keep the Pages repo on the latest main without destructive reset.
CURRENT_STAGE="git_fetch"
mbd_h2_run_with_retry "git_fetch" "$LOG" git fetch origin main
CURRENT_STAGE="git_checkout"
git checkout main
CURRENT_STAGE="git_ff_merge"
git merge --ff-only origin/main
BASE_SHA="$(git rev-parse HEAD)"

# Run code regressions against the clean checked-in artifact. The generated
# current-month candidate is validated below by its dynamic guards and smoke.
CURRENT_STAGE="refresh_regression_tests"
"$PY" -m unittest scripts/test_refresh_current_raw.py -v
CURRENT_STAGE="dashboard_regression_tests"
"$PY" -m unittest scripts/test_verify_dashboard.py -v
"$PY" -m unittest scripts/test_finalize_month_review.py -v

CURRENT_STAGE="fetch_target_youtube_snapshot"
mbd_h2_run_with_retry "fetch_target_youtube_snapshot" "$LOG" \
  "$PY" scripts/fetch_target_youtube_snapshot.py --output "$YT_SNAPSHOT_CACHE" --quiet
CURRENT_STAGE="fetch_target_mbd_snapshot"
mbd_h2_run_with_retry "fetch_target_mbd_snapshot" "$LOG" \
  "$PY" scripts/fetch_target_mbd_snapshot.py --output "$MBD_SNAPSHOT_CACHE" --quiet
CURRENT_STAGE="refresh_live_daily"
mbd_h2_run_with_retry "refresh_live_daily" "$LOG" "$PY" \
  scripts/refresh_live_daily_from_duckdb.py --quiet --duckdb "$MBD_SNAPSHOT_CACHE"
CURRENT_STAGE="refresh_live_window"
mbd_h2_run_with_retry "refresh_live_window" "$LOG" "$PY" \
  scripts/refresh_live_window_from_duckdb.py --quiet --duckdb "$MBD_SNAPSHOT_CACHE"
CURRENT_STAGE="refresh_owned_youtube_window"
mbd_h2_run_with_retry "refresh_owned_youtube_window" "$LOG" "$PY" \
  scripts/refresh_owned_youtube_window_from_duckdb.py --quiet --duckdb "$YT_SNAPSHOT_CACHE"

# Guard before committing. These deterministic checks must fail fast.
CURRENT_STAGE="git_diff_check"
git diff --check
CURRENT_STAGE="dashboard_freshness_guard"
"$PY" scripts/verify_dashboard.py index.html --require-fresh
CURRENT_STAGE="dashboard_smoke"
"$PY" scripts/smoke_dashboard.py index.html
CURRENT_STAGE="live_window_contract"
"$PY" scripts/verify_live_window_contract.py index.html

if git diff --quiet -- index.html data/live_window_contract.json data/owned_youtube_window_contract.json scripts/verify_dashboard.py scripts/test_verify_dashboard.py scripts/smoke_dashboard.py scripts/refresh_live_daily_from_duckdb.py scripts/refresh_live_window_from_duckdb.py scripts/refresh_owned_youtube_window_from_duckdb.py; then
  # Healthy no-op: stay silent for no_agent cron.
  exit 0
fi

CURRENT_STAGE="github_auth_context"
gh auth switch -u been3p-prog >/dev/null 2>&1

CURRENT_STAGE="git_commit"
git add index.html data/live_window_contract.json data/owned_youtube_window_contract.json scripts/verify_dashboard.py scripts/test_verify_dashboard.py scripts/smoke_dashboard.py scripts/refresh_live_daily_from_duckdb.py scripts/refresh_live_window_from_duckdb.py scripts/refresh_owned_youtube_window_from_duckdb.py
git commit -m "Daily refresh MBD H2 live dashboard"
HEAD_SHA="$(git rev-parse HEAD)"
CURRENT_STAGE="git_push"
mbd_h2_push_expected_commit "git_push" "$LOG" "$HEAD_SHA" "$BASE_SHA"

# Wait for the Pages verify+deploy workflow for this pushed commit.
CURRENT_STAGE="github_actions_discovery"
RUN_ID=""
for _ in {1..30}; do
  RUN_ID="$(gh run list --repo been3p-prog/mbd-h2-kr-dashboard --branch main --limit 10 --json databaseId,headSha,event,status --jq ".[] | select(.headSha == \"$HEAD_SHA\" and .event == \"push\") | .databaseId" | head -n1 || true)"
  if [[ -n "$RUN_ID" ]]; then break; fi
  /bin/sleep 5
done
if [[ -z "$RUN_ID" ]]; then
  echo "ERROR: GitHub Actions run not found for $HEAD_SHA"
  exit 1
fi

CURRENT_STAGE="github_actions_watch"
mbd_h2_run_with_retry "github_actions_watch" "$LOG" gh run watch "$RUN_ID" --repo been3p-prog/mbd-h2-kr-dashboard --exit-status

# Public readback: deployed bytes and dynamic current-month YouTube contract must match.
public_readback_once() {
"$PY" - <<'PY'
import hashlib, json, pathlib, re, sys, time, urllib.request
from scripts.verify_dashboard import youtube_week_state_ok

url=f'https://been3p-prog.github.io/mbd-h2-kr-dashboard/?daily-refresh={time.time_ns()}'
local=pathlib.Path('index.html').read_text(encoding='utf-8')
contract=json.loads(pathlib.Path('data/owned_youtube_window_contract.json').read_text(encoding='utf-8'))
public=urllib.request.urlopen(
    urllib.request.Request(url, headers={'User-Agent':'HermesCron/1.0','Cache-Control':'no-cache'}),
    timeout=30,
).read().decode('utf-8','replace')

def first(pattern, text):
    match=re.search(pattern, text, re.S)
    return match.group(1) if match else None

def segment_between(text, start_marker, end_marker=None):
    start=text.index(start_marker)
    end=text.index(end_marker, start) if end_marker else len(text)
    return text[start:end]

def month_segment(text, class_name, month):
    marker=f'class="{class_name} mv" data-m="{month}"'
    start=text.index(marker)
    if month < 12:
        end=text.index(f'class="{class_name} mv" data-m="{month + 1}"', start)
    elif class_name == 'mvk':
        end=text.index('class="mvr mv" data-m="1"', start)
    else:
        end=text.index('<section id="youtubeWindow"', start)
    return text[start:end]

manifest_raw=first(r'<script type="application/json" id="mbd-public-guard">(.*?)</script>', local)
manifest=json.loads(manifest_raw)
public_manifest_raw=first(r'<script type="application/json" id="mbd-public-guard">(.*?)</script>', public)
public_manifest=json.loads(public_manifest_raw)
month=int(manifest['default_month'])
main=contract['main_surface']
source_as_of=contract['source']['source_as_of']
contract_payload_sha=hashlib.sha256(
    json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode()
).hexdigest()
local_month=month_segment(local, 'mvr', month)
public_month=month_segment(public, 'mvr', month)
local_raw_top=month_segment(local, 'mvk', month)
public_raw_top=month_segment(public, 'mvk', month)
local_live_window=segment_between(local, '<section id="liveWindow"', '<script>')
public_live_window=segment_between(public, '<section id="liveWindow"', '<script>')
local_yt_window=segment_between(local, '<section id="youtubeWindow"', '<section id="liveWindow"')
public_yt_window=segment_between(public, '<section id="youtubeWindow"', '<section id="liveWindow"')
expected_chip=first(r'<span class="chip">((?:LIVE )?RAW [^<]+)</span>', local)
expected_avg=first(r'<div class="qk2">1D 평균 거래액</div>\s*<div class="qv num">([^<]+)</div>', local_month)
expected_count=first(rf'data-live-quality-mom="{month}-overall".*?<div class="qm2 num">([^<]+)</div>', local_month)
expected_raw_value=first(r'현재 RAW 누적 · [^<]+</div><div class="v num">([^<]+)</div>', local_raw_top)
expected_raw_range=first(r'현재 RAW 누적 · ([^<]+)</div>', local_raw_top)
expected_team_raw_rows=re.findall(
    r'RAW 누적 · [^<]+</span><b>[^<]+<span class="mutpct">진척 [^<]+</span>', local_month
)
expected_live_latest=first(r'data-live-window-latest-date="([^"]+)"', local_live_window)
expected_yt_latest=first(r'data-yt-window-latest-date="([^"]+)"', local_yt_window)
latest_publish=main.get('latest_publish_date') or 'none'
snapshot_date=main.get('snapshot_date') or 'none'
elapsed=int(main['elapsed_weeks'])
publish_count=int(main['publish_count'])
average_views=int(main['average_views'])
lf_average_views=int(main['lf_average_views'])
sf_average_views=int(main['sf_average_views'])
subscriber_count=main.get('subscriber_count')
subscriber_source=str(subscriber_count) if subscriber_count is not None else 'none'
d7_completed=int(main['d7_completed'])
checks={
  'public_bytes_match_committed': hashlib.sha256(public.encode()).hexdigest() == hashlib.sha256(local.encode()).hexdigest(),
  'contract_month_matches_manifest': int(main['month']) == month,
  'expected_chip_present': bool(expected_chip) and expected_chip in public,
  'current_raw_value_present': bool(expected_raw_value) and f'<div class="v num">{expected_raw_value}</div>' in public_raw_top,
  'current_raw_range_present': bool(expected_raw_range) and f'현재 RAW 누적 · {expected_raw_range}' in public_raw_top,
  'all_team_raw_rows_match': len(expected_team_raw_rows) == 3 and all(row in public_month for row in expected_team_raw_rows),
  'expected_live_avg_present': bool(expected_avg) and f'<div class="qv num">{expected_avg}</div>' in public_month,
  'expected_live_count_present': bool(expected_count) and expected_count in public_month,
  'youtube_publish_count': f'data-yt-main-source-publish-count="{publish_count}"' in public_month,
  'youtube_latest_publish_date': f'data-yt-main-source-latest-publish-date="{latest_publish}"' in public_month,
  'youtube_snapshot_date': f'data-yt-main-source-snapshot-date="{snapshot_date}"' in public_month,
  'youtube_elapsed_week_marker': f'data-yt-main-source-elapsed-weeks="{elapsed}"' in public_month,
  'youtube_elapsed_week_groups': youtube_week_state_ok(public_month, public_yt_window, month, elapsed),
  'youtube_rendered_publish_rows': public_month.count('data-content-link="youtube"') == publish_count,
  'youtube_quality_basis': 'data-yt-main-quality-basis="analytics-d7"' in public_month,
  'youtube_average_views': f'data-yt-main-source-average-views="{average_views}"' in public_month,
  'youtube_lf_average_views': f'data-yt-main-source-lf-average-views="{lf_average_views}"' in public_month,
  'youtube_sf_average_views': f'data-yt-main-source-sf-average-views="{sf_average_views}"' in public_month,
  'youtube_subscriber_count': f'data-yt-main-source-subscriber-count="{subscriber_source}"' in public_month,
  'youtube_d7_completed_marker': f'data-yt-main-source-d7-completed="{d7_completed}"' in public_month,
  'youtube_quality_mom': f'data-yt-quality-mom-main="{month}"' in public_month,
  'youtube_quality_trend': f'data-quality-trend="youtube-{month}"' in public_month and 'D+7 Analytics' in public_month,
  'youtube_d7_completion': f'D+7 완료 {main["d7_completed"]}/{publish_count}건' in public_month,
  'youtube_publish_split': f'SF {main["sf_publish_count"]}건 · LF {main["lf_publish_count"]}건' in public_month,
  'youtube_quality_source_as_of': public_manifest.get('source_snapshot_as_of', {}).get('yt_quality') == source_as_of['yt_quality'],
  'youtube_owned_source_as_of': public_manifest.get('source_snapshot_as_of', {}).get('owned_media') == source_as_of['owned_media'],
  'youtube_source_payload_sha256': public_manifest.get('source_payload_sha256') == contract_payload_sha,
  'live_window_mtd': 'data-live-period="mtd"' in public_live_window and '금월 누적 성과가 디폴트' in public_live_window,
  'live_window_latest': bool(expected_live_latest) and f'data-live-window-latest-date="{expected_live_latest}"' in public_live_window,
  'yt_window_mtd': 'data-yt-period="mtd"' in public_yt_window and '온드미디어 상세탭' in public_yt_window,
  'yt_window_latest': bool(expected_yt_latest) and f'data-yt-window-latest-date="{expected_yt_latest}"' in public_yt_window,
}
if not all(checks.values()):
    marker = (
        'MBD_H2_PAGES_STALE_PUBLIC_READBACK'
        if not checks['public_bytes_match_committed']
        else 'ERROR: public readback contract failed'
    )
    print(marker, checks, {
        'month': month,
        'main_surface': main,
        'expected_chip': expected_chip,
        'expected_raw_value': expected_raw_value,
        'expected_raw_range': expected_raw_range,
        'expected_team_raw_rows': expected_team_raw_rows,
        'expected_live_avg': expected_avg,
        'expected_live_count': expected_count,
        'expected_live_latest': expected_live_latest,
        'expected_yt_latest': expected_yt_latest,
    })
    sys.exit(1)
PY
}

CURRENT_STAGE="public_readback"
mbd_h2_run_with_retry "public_readback" "$LOG" public_readback_once

# Healthy success: stay silent for no_agent cron.
