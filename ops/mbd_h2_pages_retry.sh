#!/usr/bin/env bash
# [2026-08-28] Job-local bounded retry primitives for MBD H2 Pages refresh.
# Total attempts are bounded; only explicit lock/network/429/5xx/Pages-stale signals retry.
# Deterministic code, auth, schema, test, worktree, and branch failures return immediately.

MBD_H2_TRANSIENT_FAILURE_PATTERN='Could not set lock on file|Conflicting lock is held|_duckdb\.IOException.*lock|Could not resolve host|Name or service not known|NameResolutionError|Temporary failure in name resolution|network is unreachable|Failed to connect|Connection refused|Connection reset|connection was reset|RemoteDisconnected|ConnectTimeout|ReadTimeout|TimeoutError|timed out|Operation timed out|temporarily unavailable|Temporary failure|TLS.*(error|failed)|SSL.*(error|failed)|RPC failed|remote end hung up unexpectedly|HTTP(Error)?[^0-9]*(429|500|502|503|504)|status.?code.?[:= ]*(429|500|502|503|504)|Service Unavailable|Bad Gateway|Gateway Timeout|MBD_H2_PAGES_STALE_PUBLIC_READBACK'
MBD_H2_AUTH_FAILURE_PATTERN='Permission denied|Authentication failed|Host key verification failed|Could not read from remote repository|Repository not found|could not read Username|fatal: Authentication'

mbd_h2_is_transient_failure_text() {
  printf '%s\n' "$1" | /usr/bin/grep -Eiq "$MBD_H2_TRANSIENT_FAILURE_PATTERN"
}

mbd_h2_is_auth_failure_text() {
  printf '%s\n' "$1" | /usr/bin/grep -Eiq "$MBD_H2_AUTH_FAILURE_PATTERN"
}

mbd_h2_runtime_budget_allows_retry() {
  local log_file="$1"
  local sleep_seconds="$2"
  local started="${MBD_H2_RUN_STARTED_EPOCH:-$(/bin/date +%s)}"
  local max_runtime="${MBD_H2_MAX_RUNTIME_SECONDS:-1800}"
  local now elapsed
  now="$(/bin/date +%s)"
  elapsed=$((now - started))
  if (( elapsed + sleep_seconds >= max_runtime )); then
    printf '[%s] runtime budget exhausted elapsed=%ss sleep=%ss max=%ss; no retry\n' \
      "$(/bin/date '+%Y-%m-%d %H:%M:%S %Z')" "$elapsed" "$sleep_seconds" "$max_runtime" >>"$log_file"
    return 1
  fi
  return 0
}

# Usage: mbd_h2_run_with_retry <label> <log_file> <command> [args...]
mbd_h2_run_with_retry() {
  local label="$1"
  local log_file="$2"
  shift 2
  local max_attempts="${MBD_H2_MAX_ATTEMPTS:-3}"
  local sleep_seconds="${MBD_H2_RETRY_SLEEP_SECONDS:-30}"
  local attempt=1
  local rc=1
  local attempt_output

  while (( attempt <= max_attempts )); do
    printf '[%s] [%s] attempt %s/%s\n' \
      "$(/bin/date '+%Y-%m-%d %H:%M:%S %Z')" "$label" "$attempt" "$max_attempts" >>"$log_file"

    attempt_output=""
    if attempt_output="$("$@" 2>&1)"; then
      rc=0
    else
      rc=$?
    fi

    if [[ "$rc" -eq 0 ]]; then
      if (( attempt > 1 )); then
        printf '[%s] [%s] recovered on attempt %s/%s\n' \
          "$(/bin/date '+%Y-%m-%d %H:%M:%S %Z')" "$label" "$attempt" "$max_attempts" >>"$log_file"
      fi
      return 0
    fi

    if mbd_h2_is_auth_failure_text "$attempt_output" || ! mbd_h2_is_transient_failure_text "$attempt_output"; then
      printf '[%s] [%s] non-transient failure; no retry rc=%s attempt=%s/%s\n' \
        "$(/bin/date '+%Y-%m-%d %H:%M:%S %Z')" "$label" "$rc" "$attempt" "$max_attempts" >>"$log_file"
      return "$rc"
    fi

    if (( attempt >= max_attempts )); then
      printf '[%s] [%s] transient retry exhausted after %s/%s attempts rc=%s\n' \
        "$(/bin/date '+%Y-%m-%d %H:%M:%S %Z')" "$label" "$attempt" "$max_attempts" "$rc" >>"$log_file"
      return "$rc"
    fi

    if ! mbd_h2_runtime_budget_allows_retry "$log_file" "$sleep_seconds"; then
      return "$rc"
    fi

    printf '[%s] [%s] transient failure; retrying same idempotent stage after %ss\n' \
      "$(/bin/date '+%Y-%m-%d %H:%M:%S %Z')" "$label" "$sleep_seconds" >>"$log_file"
    /bin/sleep "$sleep_seconds"
    attempt=$((attempt + 1))
  done
  return "$rc"
}

# Push one already-created commit. Never recommit or force-push.
# If the push response is ambiguous, remote SHA readback decides whether to retry.
mbd_h2_push_expected_commit() {
  local label="$1"
  local log_file="$2"
  local expected_sha="$3"
  local base_sha="$4"
  local git_bin="${MBD_H2_GIT_BIN:-git}"
  local max_attempts="${MBD_H2_MAX_ATTEMPTS:-3}"
  local sleep_seconds="${MBD_H2_RETRY_SLEEP_SECONDS:-30}"
  local attempt=1
  local rc=1
  local push_output remote_output remote_sha remote_rc candidate ref extra

  while (( attempt <= max_attempts )); do
    printf '[%s] [%s] attempt %s/%s expected_sha=%s\n' \
      "$(/bin/date '+%Y-%m-%d %H:%M:%S %Z')" "$label" "$attempt" "$max_attempts" "$expected_sha" >>"$log_file"

    push_output=""
    if push_output="$("$git_bin" push origin main 2>&1)"; then
      return 0
    else
      rc=$?
    fi

    remote_output=""
    remote_rc=0
    if remote_output="$("$git_bin" ls-remote origin refs/heads/main 2>&1)"; then
      remote_sha=""
      while read -r candidate ref extra; do
        if [[ "${#candidate}" -eq 40 && "$candidate" != *[!0-9a-fA-F]* && "$ref" == "refs/heads/main" && -z "$extra" ]]; then
          remote_sha="$candidate"
          break
        fi
      done <<<"$remote_output"
    else
      remote_rc=$?
      remote_sha=""
    fi

    if [[ "$remote_sha" == "$expected_sha" ]]; then
      printf '[%s] [%s] push response failed but remote already matches expected commit; recovered\n' \
        "$(/bin/date '+%Y-%m-%d %H:%M:%S %Z')" "$label" >>"$log_file"
      return 0
    fi

    if [[ -n "$remote_sha" && "$remote_sha" != "$base_sha" ]]; then
      printf '[%s] [%s] remote moved to unexpected commit remote=%s expected=%s base=%s; no retry\n' \
        "$(/bin/date '+%Y-%m-%d %H:%M:%S %Z')" "$label" "$remote_sha" "$expected_sha" "$base_sha" >>"$log_file"
      return 65
    fi

    if [[ "$remote_rc" -ne 0 ]] && mbd_h2_is_auth_failure_text "$remote_output"; then
      printf '[%s] [%s] non-transient remote readback auth failure; no retry rc=%s\n' \
        "$(/bin/date '+%Y-%m-%d %H:%M:%S %Z')" "$label" "$remote_rc" >>"$log_file"
      return "$remote_rc"
    fi

    if mbd_h2_is_auth_failure_text "$push_output"; then
      printf '[%s] [%s] non-transient push auth failure; no retry rc=%s\n' \
        "$(/bin/date '+%Y-%m-%d %H:%M:%S %Z')" "$label" "$rc" >>"$log_file"
      return "$rc"
    fi

    if ! mbd_h2_is_transient_failure_text "$push_output"; then
      printf '[%s] [%s] non-transient push failure; no retry rc=%s\n' \
        "$(/bin/date '+%Y-%m-%d %H:%M:%S %Z')" "$label" "$rc" >>"$log_file"
      return "$rc"
    fi

    if (( attempt >= max_attempts )); then
      printf '[%s] [%s] transient push retry exhausted after %s/%s attempts rc=%s\n' \
        "$(/bin/date '+%Y-%m-%d %H:%M:%S %Z')" "$label" "$attempt" "$max_attempts" "$rc" >>"$log_file"
      return "$rc"
    fi

    if ! mbd_h2_runtime_budget_allows_retry "$log_file" "$sleep_seconds"; then
      return "$rc"
    fi

    printf '[%s] [%s] transient push failure with remote still at base/unknown; retrying same commit after %ss\n' \
      "$(/bin/date '+%Y-%m-%d %H:%M:%S %Z')" "$label" "$sleep_seconds" >>"$log_file"
    /bin/sleep "$sleep_seconds"
    attempt=$((attempt + 1))
  done
  return "$rc"
}
