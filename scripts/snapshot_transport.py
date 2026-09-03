#!/usr/bin/env python3
"""Secret-safe diagnostics for SSH/SCP snapshot transport failures."""
from __future__ import annotations

import re
import subprocess

_AUTH_PATTERNS = (
    (re.compile(r"permission denied", re.I), "Permission denied"),
    (re.compile(r"host key verification failed", re.I), "Host key verification failed"),
    (re.compile(r"remote host identification has changed", re.I), "Remote host identification changed"),
    (re.compile(r"too many authentication failures", re.I), "Too many authentication failures"),
)

_TRANSIENT_PATTERNS = (
    (re.compile(r"operation timed out|connection timed out", re.I), "Operation timed out"),
    (re.compile(r"network is unreachable|no route to host", re.I), "Network is unreachable"),
    (re.compile(r"connection refused", re.I), "Connection refused"),
    (re.compile(r"connection reset|connection closed by remote host", re.I), "Connection reset"),
    (re.compile(r"could not resolve host(?:name)?|temporary failure in name resolution", re.I), "Could not resolve host"),
)


def describe_transport_failure(returncode: int, stderr: str | None) -> str:
    """Classify allowlisted transport errors without echoing raw host/path data."""
    raw = stderr or ""
    for pattern, label in _AUTH_PATTERNS:
        if pattern.search(raw):
            return f"non_transient_auth: {label} rc={returncode}"
    for pattern, label in _TRANSIENT_PATTERNS:
        if pattern.search(raw):
            return f"transient_network: {label} rc={returncode}"
    return f"non_transient_transport: command failed rc={returncode}"


def describe_transport_exception(exc: BaseException) -> str:
    """Classify subprocess invocation exceptions without echoing argv/path data."""
    if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)):
        return "transient_network: Operation timed out"
    return "non_transient_transport: command failed"
