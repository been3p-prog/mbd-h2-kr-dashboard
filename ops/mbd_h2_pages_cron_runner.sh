#!/usr/bin/env bash
# Run the generated-dashboard refresh from a clean ephemeral clone so operator
# work is never reset, merged, or blocked by unattended automation.
set -euo pipefail

CLONE_URL="${MBD_H2_CLONE_URL:-https://github.com/been3p-prog/mbd-h2-kr-dashboard.git}"
RUNTIME_PARENT="${MBD_H2_RUNTIME_PARENT:-${TMPDIR:-/tmp}}"
LOG_DIR="${MBD_H2_LOG_DIR:-/Users/sb.lee/.hermes/logs/mbd-h2-pages}"

if [[ "$CLONE_URL" =~ ^[[:alpha:]][[:alnum:]+.-]*://[^/@]+@ ]]; then
  echo "ERROR: MBD H2 cron runner rejected credential-bearing clone URL" >&2
  exit 1
fi

/bin/mkdir -p "$RUNTIME_PARENT" "$LOG_DIR"
RUN_ROOT="$(/usr/bin/mktemp -d "$RUNTIME_PARENT/mbd-h2-pages-refresh.XXXXXX")"
CHILD_PID=""

kill_escaped_descendants() {
  local parent_pid="$1"
  local child_pid
  local current_ppid
  local current_pgid
  while IFS= read -r child_pid; do
    [[ "$child_pid" =~ ^[0-9]+$ ]] || continue
    kill_escaped_descendants "$child_pid"
    current_ppid=""
    current_pgid=""
    if ! read -r current_ppid current_pgid < <(
      /bin/ps -o ppid=,pgid= -p "$child_pid" 2>/dev/null
    ); then
      continue
    fi
    if [[ "$current_ppid" == "$parent_pid" && "$current_pgid" != "$CHILD_PID" ]]; then
      kill -TERM "$child_pid" 2>/dev/null || true
      current_ppid=""
      current_pgid=""
      if read -r current_ppid current_pgid < <(
        /bin/ps -o ppid=,pgid= -p "$child_pid" 2>/dev/null
      ) && [[ "$current_ppid" == "$parent_pid" && "$current_pgid" != "$CHILD_PID" ]]; then
        kill -KILL "$child_pid" 2>/dev/null || true
      fi
    fi
  done < <(/usr/bin/pgrep -P "$parent_pid" 2>/dev/null || true)
}

stop_child() {
  if [[ -n "$CHILD_PID" ]]; then
    kill_escaped_descendants "$CHILD_PID"
    if ! kill -TERM -- "-$CHILD_PID" 2>/dev/null; then
      kill -TERM "$CHILD_PID" 2>/dev/null || true
    fi
    /bin/sleep 0.5
    if kill -0 -- "-$CHILD_PID" 2>/dev/null; then
      kill -KILL -- "-$CHILD_PID" 2>/dev/null || true
    elif kill -0 "$CHILD_PID" 2>/dev/null; then
      kill -KILL "$CHILD_PID" 2>/dev/null || true
    fi
    wait "$CHILD_PID" 2>/dev/null || true
    CHILD_PID=""
  fi
}

cleanup() {
  local rc=$?
  trap - EXIT
  stop_child
  /bin/rm -rf "$RUN_ROOT"
  exit "$rc"
}

on_signal() {
  local exit_code="$1"
  trap - HUP INT TERM
  stop_child
  exit "$exit_code"
}

trap cleanup EXIT
trap 'on_signal 129' HUP
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

if ! /usr/bin/git clone --quiet --depth 1 --branch main "$CLONE_URL" "$RUN_ROOT" >/dev/null 2>&1; then
  echo "ERROR: MBD H2 cron runner clone failed" >&2
  exit 1
fi

export MBD_H2_ROOT="$RUN_ROOT"
export MBD_H2_LOG_DIR="$LOG_DIR"
cd "$RUN_ROOT"
set -m
/bin/bash "$RUN_ROOT/ops/mbd_h2_pages_live_daily_refresh.sh" &
CHILD_PID=$!
set +m
if wait "$CHILD_PID"; then
  rc=0
else
  rc=$?
fi
CHILD_PID=""
exit "$rc"
