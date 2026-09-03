#!/usr/bin/env python3
"""Fetch the dashboard-sized MBD DuckDB snapshot from the canonical Mac."""
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

from snapshot_transport import describe_transport_exception, describe_transport_failure

KST = dt.timezone(dt.timedelta(hours=9))
DEFAULT_OUTPUT = Path("/tmp/mbd_h2_target_snapshot.duckdb")
DEFAULT_HOST = "cnc-media@192.168.7.238"
DEFAULT_KEY = Path("/Users/sb.lee/.ssh/id_ed25519_mbd_server")
DEFAULT_REMOTE_PYTHON = "/Users/cnc-media/automations/.venvs/mbd/bin/python"
DEFAULT_REMOTE_DB = "/Users/cnc-media/automations/mbd/mbd.duckdb"
SOURCE_TEAMS = {"ad_gen", "ad_int", "live"}
REQUIRED_COLUMNS = {
    ("ad_gen", "booking_pred"): {"date", "status", "ad_type", "party_type", "revenue"},
    ("ad_int", "contract"): {"계약 시작일", "매출 귀속월", "미셀 매출액"},
    ("live", "raw_slots"): {
        "온에어 일자", "브랜드명", "1P/3P", "패키지", "PGM", "비고 (프로모션)",
        "PD", "라이브 시청자 (비로그인 포함)", "상품 클릭수", "라이브 구매자수",
        "일 전체 GMV (라이브 브랜드 전체)", "라이브 1H GMV", "방송별 데이터 GMV",
        "AF수취액", "비용", "마진액",
    },
    ("meta", "targets"): {"team", "metric", "ym", "kind", "value_num"},
    ("meta", "ingest_log"): {"team", "last_ingest_at", "status"},
    ("snapshot", "meta"): {"captured_at", "source_mtime"},
    ("revenue", "integrated_ssot"): {
        "revenue_month", "include_in_mbd_revenue", "revenue_team",
        "team_attributed_revenue", "package_or_slot_type", "source_date", "brand_name",
    },
}
REMOTE_COPY_SCRIPT = r'''
import datetime as dt
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
    for schema in ("ad_gen", "ad_int", "live", "meta", "snapshot", "revenue"):
        con.execute("create schema target." + schema)
    for schema, table in (
        ("ad_gen", "booking_pred"),
        ("ad_int", "contract"),
        ("live", "raw_slots"),
        ("meta", "targets"),
        ("meta", "ingest_log"),
        ("revenue", "integrated_ssot"),
    ):
        con.execute(
            "create table target." + schema + "." + table
            + " as select * from source." + schema + "." + table
        )
    captured = dt.datetime.now(dt.timezone.utc)
    source_mtime = dt.datetime.fromtimestamp(os.stat(source).st_mtime, dt.timezone.utc)
    con.execute(
        "create table target.snapshot.meta as select ?::timestamptz captured_at, ?::timestamptz source_mtime",
        [captured, source_mtime],
    )
finally:
    con.close()
'''


def _as_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _assert_fresh(label: str, value, as_of: dt.date) -> dt.date:
    if value is None:
        raise RuntimeError(f"target MBD source has no freshness marker: {label}")
    source_date = _as_date(value)
    if source_date < as_of - dt.timedelta(days=1):
        raise RuntimeError(
            f"stale target MBD source: source={label} source_date={source_date} as_of={as_of}"
        )
    if source_date > as_of + dt.timedelta(days=1):
        raise RuntimeError(
            f"future target MBD source: source={label} source_date={source_date} as_of={as_of}"
        )
    return source_date


def validate_snapshot(path: Path, *, as_of: dt.date | None = None) -> dict:
    as_of = as_of or dt.datetime.now(KST).date()
    con = duckdb.connect(str(path), read_only=True)
    try:
        for (schema, table), required in REQUIRED_COLUMNS.items():
            columns = {
                row[0]
                for row in con.execute(
                    "select column_name from information_schema.columns "
                    "where table_schema = ? and table_name = ?",
                    [schema, table],
                ).fetchall()
            }
            missing = sorted(required - columns)
            if missing:
                raise RuntimeError(
                    f"target MBD snapshot missing columns in {schema}.{table}: {','.join(missing)}"
                )
            quoted = f'"{schema}"."{table}"'
            if con.execute("select count(*) from " + quoted).fetchone()[0] == 0:
                raise RuntimeError(f"target MBD snapshot empty relation: {schema}.{table}")

        current_targets = {
            row[0]
            for row in con.execute(
                "select team from meta.targets where ym = ? and metric = '매출' and kind = 'target'",
                [as_of.strftime("%Y-%m")],
            ).fetchall()
        }
        missing_targets = sorted(SOURCE_TEAMS - current_targets)
        if missing_targets:
            raise RuntimeError(f"target MBD snapshot missing current targets: {','.join(missing_targets)}")

        completed_month = (as_of.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
        completed_actual_teams = {
            row[0]
            for row in con.execute(
                "select revenue_team from revenue.integrated_ssot "
                "where revenue_month = ? and include_in_mbd_revenue group by revenue_team",
                [completed_month],
            ).fetchall()
        }
        expected_actual_teams = {"일반광고", "통합광고", "라이브커머스"}
        missing_actuals = sorted(expected_actual_teams - completed_actual_teams)
        if missing_actuals:
            raise RuntimeError(
                "target MBD snapshot missing completed-month actual teams: "
                + ",".join(missing_actuals)
            )

        ingest_rows = con.execute(
            "select team, max(last_ingest_at) from meta.ingest_log "
            "where team in ('ad_gen','ad_int','live') "
            "and lower(trim(status)) in ('ok','success') group by team"
        ).fetchall()
        ingest = {str(team): stamp for team, stamp in ingest_rows}
        missing_ingest = sorted(SOURCE_TEAMS - set(ingest))
        if missing_ingest:
            raise RuntimeError(f"target MBD snapshot missing ingest markers: {','.join(missing_ingest)}")
        captured_at, source_mtime = con.execute(
            "select max(captured_at), max(source_mtime) from snapshot.meta"
        ).fetchone()
    finally:
        con.close()

    captured_date = _assert_fresh("snapshot.captured_at", captured_at, as_of)
    source_mtime_date = _assert_fresh("snapshot.source_mtime", source_mtime, as_of)
    ingest_dates = {
        team: str(_assert_fresh(f"meta.ingest_log:{team}", ingest[team], as_of))
        for team in sorted(SOURCE_TEAMS)
    }
    return {
        "captured_at": str(captured_at),
        "captured_date": str(captured_date),
        "source_mtime_date": str(source_mtime_date),
        "completed_actual_month": completed_month,
        "ingest_dates": ingest_dates,
        "bytes": path.stat().st_size,
    }


def _ssh_args(key: Path, host: str) -> list[str]:
    return [
        "ssh", "-i", str(key), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "ConnectTimeout=10", host,
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
        raise RuntimeError("target SSH key missing")
    output.parent.mkdir(parents=True, exist_ok=True)
    remote_copy = f"/tmp/mbd_h2_target_snapshot_{uuid.uuid4().hex}.duckdb"
    partial = output.with_name(f"{output.name}.{uuid.uuid4().hex}.partial")
    ssh = _ssh_args(key, host)
    primary_error: BaseException | None = None
    try:
        try:
            copied = subprocess.run(
                ssh + [remote_python, "-", remote_db, remote_copy],
                input=REMOTE_COPY_SCRIPT,
                text=True,
                capture_output=True,
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = describe_transport_exception(exc)
            raise RuntimeError(f"target MBD snapshot copy failed: {detail}") from None
        if copied.returncode != 0:
            detail = describe_transport_failure(copied.returncode, getattr(copied, "stderr", None))
            raise RuntimeError(f"target MBD snapshot copy failed: {detail}")
        try:
            fetched = subprocess.run(
                [
                    "scp", "-q", "-i", str(key), "-o", "BatchMode=yes",
                    "-o", "IdentitiesOnly=yes", f"{host}:{remote_copy}", str(partial),
                ],
                text=True,
                capture_output=True,
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = describe_transport_exception(exc)
            raise RuntimeError(f"target MBD snapshot transfer failed: {detail}") from None
        if fetched.returncode != 0:
            detail = describe_transport_failure(fetched.returncode, getattr(fetched, "stderr", None))
            raise RuntimeError(f"target MBD snapshot transfer failed: {detail}")
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
                cleanup_stderr = getattr(cleaned, "stderr", None)
                if cleanup_stderr:
                    detail = describe_transport_failure(cleaned.returncode, cleanup_stderr)
                    raise RuntimeError(f"target MBD temp cleanup failed: {detail}")
                raise RuntimeError(f"target MBD temp cleanup failed rc={cleaned.returncode}")
        except (OSError, subprocess.SubprocessError) as exc:
            if primary_error is None:
                detail = describe_transport_exception(exc)
                raise RuntimeError(f"target MBD temp cleanup failed: {detail}") from None
            print(
                f"WARNING: target MBD temp cleanup failed: {type(exc).__name__}",
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
