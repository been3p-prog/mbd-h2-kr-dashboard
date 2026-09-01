#!/usr/bin/env python3
"""Fetch a consistent read-only YouTube DuckDB snapshot from MBD Mac."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import duckdb

KST = dt.timezone(dt.timedelta(hours=9))
DEFAULT_OUTPUT = Path("/tmp/mbd_h2_youtube_target_snapshot.duckdb")
DEFAULT_HOST = "cnc-media@192.168.7.238"
DEFAULT_KEY = Path("/Users/sb.lee/.ssh/id_ed25519_mbd_server")
DEFAULT_REMOTE_PYTHON = "/Users/cnc-media/automations/.venvs/mbd/bin/python"
DEFAULT_REMOTE_DB = "/Users/cnc-media/automations/youtube-view-snapshot/youtube_views.duckdb"
REQUIRED_RELATIONS = {
    "dim_video",
    "fact_analytics_d7",
    "fact_channel_snapshot",
    "fact_snapshot",
    "v_channel_daily_subscribers",
    "v_latest_snapshot",
    "v_public_dplusn_video",
    "v_youtube_monthly_analytics",
    "v_youtube_weekly_analytics",
}
REQUIRED_COLUMNS = {
    "dim_video": {"video_id", "publish_date", "is_active", "form", "title", "url"},
    "fact_analytics_d7": {
        "video_id", "d7_complete", "fetched_at", "metric_end_date", "view_count",
        "like_count", "comment_count", "share_count",
    },
    "fact_channel_snapshot": {"snapshot_date", "captured_at", "subscriber_count"},
    "fact_snapshot": {"snapshot_date", "captured_at", "video_id"},
    "v_channel_daily_subscribers": {
        "snapshot_date", "captured_at", "subscriber_count", "raw_status",
    },
    "v_latest_snapshot": {"video_id", "cumulative_view_count", "snapshot_date"},
    "v_public_dplusn_video": {
        "d_plus_n", "video_id", "publish_date", "form", "ip", "title", "url",
        "views", "complete",
    },
    "v_youtube_monthly_analytics": {
        "period_start", "period_end", "metric_start_date", "metric_end_date",
        "period_complete", "channel_view_count", "channel_like_count",
        "channel_comment_count", "channel_share_count", "channel_engagement_count",
        "new_published_view_count", "prior_published_view_count",
        "unknown_publish_view_count", "fetched_at", "raw_status",
    },
    "v_youtube_weekly_analytics": {
        "period_start", "period_end", "metric_start_date", "metric_end_date",
        "period_complete", "channel_view_count", "channel_like_count",
        "channel_comment_count", "channel_share_count", "channel_engagement_count",
        "new_published_view_count", "prior_published_view_count",
        "unknown_publish_view_count", "fetched_at", "raw_status",
    },
}
FRESHNESS_COLUMNS = {
    "v_youtube_monthly_analytics": "fetched_at",
    "v_youtube_weekly_analytics": "fetched_at",
    "fact_analytics_d7": "fetched_at",
    "v_channel_daily_subscribers": "captured_at",
}
REMOTE_COPY_SCRIPT = r'''
import os
import re
import sys
import duckdb

source, target = sys.argv[1:3]
safe_path = re.compile(r"/[A-Za-z0-9_./-]+").fullmatch
if not safe_path(source) or not safe_path(target):
    raise ValueError("unsafe DuckDB path")
try:
    os.unlink(target)
except FileNotFoundError:
    pass
con = duckdb.connect()
try:
    con.execute("attach '" + source + "' as source (read_only)")
    con.execute("attach '" + target + "' as target")
    con.execute("copy from database source to target")
finally:
    con.close()
'''


def validate_snapshot(path: Path, *, as_of: dt.date | None = None) -> dict:
    as_of = as_of or dt.datetime.now(KST).date()
    con = duckdb.connect(str(path), read_only=True)
    try:
        relations = {
            row[0]
            for row in con.execute(
                "select table_name from information_schema.tables where table_schema = 'main'"
            ).fetchall()
        }
        missing = sorted(REQUIRED_RELATIONS - relations)
        if missing:
            raise RuntimeError(f"target YouTube snapshot missing relations: {','.join(missing)}")
        for relation, required in REQUIRED_COLUMNS.items():
            quoted_relation = '"' + relation + '"'
            columns = {
                row[0]
                for row in con.execute(
                    "select column_name from information_schema.columns "
                    "where table_schema = 'main' and table_name = ?",
                    [relation],
                ).fetchall()
            }
            missing_columns = sorted(required - columns)
            if missing_columns:
                raise RuntimeError(
                    f"target YouTube snapshot missing columns in {relation}: "
                    f"{','.join(missing_columns)}"
                )
            if con.execute("select count(*) from " + quoted_relation).fetchone()[0] == 0:
                raise RuntimeError(f"target YouTube snapshot empty relation: {relation}")
        source_times = {
            relation: con.execute(
                'select max("' + column + '") from "' + relation + '"'
            ).fetchone()[0]
            for relation, column in FRESHNESS_COLUMNS.items()
        }
        snapshot_date, captured_at = con.execute(
            "select max(snapshot_date), max(captured_at) from fact_snapshot"
        ).fetchone()
        latest_publish = con.execute(
            "select max(publish_date) from dim_video where is_active"
        ).fetchone()[0]
    finally:
        con.close()
    if snapshot_date is None or captured_at is None:
        raise RuntimeError("target YouTube snapshot has no captured snapshot")
    if snapshot_date < as_of - dt.timedelta(days=1):
        raise RuntimeError(
            f"stale target YouTube snapshot: snapshot_date={snapshot_date} as_of={as_of}"
        )
    if snapshot_date > as_of + dt.timedelta(days=1):
        raise RuntimeError(
            f"future target YouTube snapshot: snapshot_date={snapshot_date} as_of={as_of}"
        )
    for relation, source_time in source_times.items():
        if source_time is None:
            raise RuntimeError(f"target YouTube source has no freshness marker: {relation}")
        source_date = source_time.date() if isinstance(source_time, dt.datetime) else source_time
        if source_date < as_of - dt.timedelta(days=1):
            raise RuntimeError(
                f"stale target YouTube source: relation={relation} "
                f"source_date={source_date} as_of={as_of}"
            )
        if source_date > as_of + dt.timedelta(days=1):
            raise RuntimeError(
                f"future target YouTube source: relation={relation} "
                f"source_date={source_date} as_of={as_of}"
            )
    return {
        "snapshot_date": str(snapshot_date),
        "captured_at": str(captured_at),
        "latest_publish_date": str(latest_publish) if latest_publish else None,
        "bytes": path.stat().st_size,
    }


def _ssh_args(key: Path, host: str) -> list[str]:
    return [
        "ssh",
        "-i",
        str(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout=10",
        host,
    ]


def sync_snapshot(
    output: Path,
    *,
    host: str = DEFAULT_HOST,
    key: Path = DEFAULT_KEY,
    remote_python: str = DEFAULT_REMOTE_PYTHON,
    remote_db: str = DEFAULT_REMOTE_DB,
) -> dict:
    if not key.is_file():
        raise RuntimeError(f"target SSH key missing: {key}")
    output.parent.mkdir(parents=True, exist_ok=True)
    remote_copy = f"/tmp/mbd_h2_youtube_snapshot_{uuid.uuid4().hex}.duckdb"
    partial = output.with_name(f"{output.name}.{uuid.uuid4().hex}.partial")
    ssh = _ssh_args(key, host)
    primary_error: BaseException | None = None
    try:
        copied = subprocess.run(
            ssh + [remote_python, "-", remote_db, remote_copy],
            input=REMOTE_COPY_SCRIPT,
            text=True,
            capture_output=True,
            timeout=180,
        )
        if copied.returncode != 0:
            raise RuntimeError(f"target DuckDB snapshot copy failed rc={copied.returncode}")
        fetched = subprocess.run(
            ["scp", "-q", "-i", str(key), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", f"{host}:{remote_copy}", str(partial)],
            text=True,
            capture_output=True,
            timeout=180,
        )
        if fetched.returncode != 0:
            raise RuntimeError(f"target DuckDB snapshot transfer failed rc={fetched.returncode}")
        result = validate_snapshot(partial)
        os.replace(partial, output)
        result["output"] = str(output)
        return result
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        partial.unlink(missing_ok=True)
        try:
            cleaned = subprocess.run(
                ssh + ["/bin/rm", "-f", remote_copy],
                text=True,
                capture_output=True,
                timeout=30,
            )
            if cleaned.returncode != 0:
                raise RuntimeError(f"target DuckDB temp cleanup failed rc={cleaned.returncode}")
        except (OSError, subprocess.SubprocessError) as exc:
            if primary_error is None:
                raise RuntimeError(
                    f"target DuckDB temp cleanup failed: {type(exc).__name__}"
                ) from exc
            print(
                f"WARNING: target DuckDB temp cleanup failed: {type(exc).__name__}",
                file=sys.stderr,
            )
        except RuntimeError as exc:
            if primary_error is None:
                raise
            print(f"WARNING: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    result = sync_snapshot(Path(args.output))
    if not args.quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
