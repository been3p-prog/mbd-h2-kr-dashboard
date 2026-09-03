#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "ops" / "mbd_h2_pages_cron_runner.sh"
RETRY_LIB = REPO / "ops" / "mbd_h2_pages_retry.sh"
TRANSPORT = REPO / "scripts" / "snapshot_transport.py"


class MbdH2CronRuntimeTest(unittest.TestCase):
    def _run(self, args, *, cwd=None, env=None, check=True):
        return subprocess.run(
            args,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=check,
        )

    def test_cron_runner_defaults_to_canonical_origin(self):
        runner = RUNNER.read_text(encoding="utf-8")
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=REPO,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertIn(remote, runner)
        self.assertNotIn("sb-lee-been/mbd-h2-kr-dashboard", runner)

    def test_cron_runner_sanitizes_clone_failure_for_credential_bearing_override(self):
        self.assertTrue(RUNNER.is_file(), f"missing cron runner: {RUNNER}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            redaction_marker = "DO_NOT_ECHO_CLONE_CONTEXT"
            clone_url = str(root / f"missing-repo-{redaction_marker}.git")
            env = os.environ.copy()
            env.update(
                {
                    "MBD_H2_CLONE_URL": clone_url,
                    "MBD_H2_RUNTIME_PARENT": str(root / "runtime"),
                    "MBD_H2_LOG_DIR": str(root / "logs"),
                }
            )
            result = self._run(["/bin/bash", str(RUNNER)], env=env, check=False)
            combined = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ERROR: MBD H2 cron runner clone failed", result.stderr)
            self.assertNotIn(redaction_marker, combined)
            self.assertNotIn(clone_url, combined)

    def test_cron_runner_rejects_credential_bearing_clone_url_before_clone(self):
        self.assertTrue(RUNNER.is_file(), f"missing cron runner: {RUNNER}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential_marker = "DO_NOT_ECHO_CLONE_CREDENTIAL"
            clone_url = f"file://x-access-token:{credential_marker}@localhost{root}/source"
            env = os.environ.copy()
            env.update(
                {
                    "MBD_H2_CLONE_URL": clone_url,
                    "MBD_H2_RUNTIME_PARENT": str(root / "runtime"),
                    "MBD_H2_LOG_DIR": str(root / "logs"),
                }
            )
            result = self._run(["/bin/bash", str(RUNNER)], env=env, check=False)
            combined = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ERROR: MBD H2 cron runner rejected credential-bearing clone URL", result.stderr)
            self.assertNotIn(credential_marker, combined)
            self.assertNotIn(clone_url, combined)

    def _write_inert_daily_wrapper_fixture(self, source: Path) -> None:
        (source / "ops").mkdir(parents=True)
        (source / "scripts").mkdir(parents=True)
        (source / "data").mkdir(parents=True)
        wrapper = source / "ops" / "mbd_h2_pages_live_daily_refresh.sh"
        shutil.copy2(REPO / "ops" / "mbd_h2_pages_live_daily_refresh.sh", wrapper)
        wrapper.chmod(0o644)
        shutil.copy2(RETRY_LIB, source / "ops" / "mbd_h2_pages_retry.sh")
        inert_test = (
            "import unittest\n\n"
            "class InertCronFixtureTest(unittest.TestCase):\n"
            "    def test_inert(self):\n"
            "        self.assertTrue(True)\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        )
        for name in (
            "test_refresh_current_raw.py",
            "test_ops_runtime.py",
            "test_verify_dashboard.py",
            "test_finalize_month_review.py",
        ):
            (source / "scripts" / name).write_text(inert_test, encoding="utf-8")
        inert_script = "raise SystemExit(0)\n"
        for name in (
            "fetch_target_youtube_snapshot.py",
            "fetch_target_mbd_snapshot.py",
            "refresh_live_daily_from_duckdb.py",
            "refresh_live_window_from_duckdb.py",
            "refresh_owned_youtube_window_from_duckdb.py",
            "verify_dashboard.py",
            "smoke_dashboard.py",
            "verify_live_window_contract.py",
        ):
            (source / "scripts" / name).write_text(inert_script, encoding="utf-8")
        (source / "index.html").write_text("<html></html>\n", encoding="utf-8")
        (source / "data" / "live_window_contract.json").write_text("{}\n", encoding="utf-8")
        (source / "data" / "owned_youtube_window_contract.json").write_text("{}\n", encoding="utf-8")

    def test_cron_runner_executes_actual_daily_wrapper_in_clean_isolated_clone(self):
        self.assertTrue(RUNNER.is_file(), f"missing cron runner: {RUNNER}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            runtime_parent = root / "runtime"
            persistent_logs = root / "logs"
            py_log = root / "python-shim.jsonl"
            py_shim = root / "python-shim"
            source.mkdir()
            self._write_inert_daily_wrapper_fixture(source)
            wrapper_fixture = source / "ops" / "mbd_h2_pages_live_daily_refresh.sh"
            self.assertEqual(wrapper_fixture.stat().st_mode & 0o777, 0o644)
            py_shim.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "entry = {\n"
                "    'argv': sys.argv[1:],\n"
                "    'cwd': os.getcwd(),\n"
                "    'root': os.environ.get('MBD_H2_ROOT'),\n"
                "    'log_dir': os.environ.get('MBD_H2_LOG_DIR'),\n"
                "}\n"
                "with pathlib.Path(os.environ['PY_SHIM_LOG']).open('a', encoding='utf-8') as fh:\n"
                "    fh.write(json.dumps(entry, sort_keys=True) + '\\n')\n"
                "raise SystemExit(0)\n",
                encoding="utf-8",
            )
            py_shim.chmod(0o755)
            self._run(["git", "init", "-b", "main"], cwd=source)
            self._run(["git", "add", "."], cwd=source)
            self._run(
                [
                    "git", "-c", "user.name=MBD Test", "-c", "user.email=mbd-test@example.invalid",
                    "commit", "-m", "fixture",
                ],
                cwd=source,
            )
            (source / "operator-uncommitted.txt").write_text("preserve me\n", encoding="utf-8")
            dirty_before = self._run(
                ["git", "status", "--porcelain", "--untracked-files=all"], cwd=source
            ).stdout

            env = os.environ.copy()
            env.update(
                {
                    "MBD_H2_CLONE_URL": str(source),
                    "MBD_H2_RUNTIME_PARENT": str(runtime_parent),
                    "MBD_H2_LOG_DIR": str(persistent_logs),
                    "MBD_H2_PYTHON": str(py_shim),
                    "MBD_H2_MAX_ATTEMPTS": "2",
                    "MBD_H2_RETRY_SLEEP_SECONDS": "0",
                    "MBD_H2_MAX_RUNTIME_SECONDS": "60",
                    "PY_SHIM_LOG": str(py_log),
                }
            )
            result = self._run(["/bin/bash", str(RUNNER)], env=env)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
            self.assertEqual(
                self._run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=source).stdout,
                dirty_before,
            )
            self.assertEqual(list(runtime_parent.glob("mbd-h2-pages-refresh.*")), [])
            log_files = sorted(persistent_logs.glob("daily_live_refresh_*.log"))
            self.assertEqual(len(log_files), 1)
            log_text = log_files[0].read_text(encoding="utf-8")
            self.assertIn("[fetch_target_youtube_snapshot] attempt 1/2", log_text)
            self.assertIn("[fetch_target_mbd_snapshot] attempt 1/2", log_text)
            self.assertTrue(py_log.is_file(), "daily wrapper must honor MBD_H2_PYTHON")
            entries = [json.loads(line) for line in py_log.read_text(encoding="utf-8").splitlines()]
            self.assertGreaterEqual(len(entries), 8)
            self.assertFalse(
                any("scripts/test_ops_runtime.py" in entry["argv"] for entry in entries),
                "production wrapper must not recursively run its own cron-runner regression suite",
            )
            roots = {entry["root"] for entry in entries}
            self.assertEqual(len(roots), 1)
            executed = Path(next(iter(roots)))
            executed_resolved = executed.resolve()
            self.assertNotEqual(executed_resolved, source.resolve())
            self.assertTrue(str(executed_resolved).startswith(str(runtime_parent.resolve())))
            self.assertEqual(
                {Path(entry["cwd"]).resolve() for entry in entries},
                {executed_resolved},
            )
            self.assertEqual({entry["log_dir"] for entry in entries}, {str(persistent_logs)})
            self.assertFalse(executed.exists(), "ephemeral clone must be cleaned after execution")

    def test_cron_runner_parent_signals_stop_child_and_remove_clone(self):
        self.assertTrue(RUNNER.is_file(), f"missing cron runner: {RUNNER}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            runtime_parent = root / "runtime"
            ready = root / "child-ready"
            grandchild_pid_file = root / "grandchild-pid"
            escaped_ready = root / "escaped-ready"
            wrapper = source / "ops" / "mbd_h2_pages_live_daily_refresh.sh"
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text(
                "#!/bin/bash\n"
                "if [[ \"${RUNNER_ESCAPE_DESCENDANT:-0}\" != 1 ]]; then\n"
                "  printf '%s\\n' \"$$\" > \"$RUNNER_GRANDCHILD_PID_FILE\"\n"
                "  : > \"$RUNNER_CHILD_READY\"\n"
                "  exec /bin/sleep 30\n"
                "fi\n"
                "/usr/bin/python3 -c 'import os,pathlib,signal,time; os.setsid(); "
                "[signal.signal(s, signal.SIG_IGN) for s in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)]; "
                "pathlib.Path(os.environ[\"RUNNER_ESCAPED_READY\"]).touch(); time.sleep(30)' &\n"
                "grandchild_pid=$!\n"
                "while [[ ! -f \"$RUNNER_ESCAPED_READY\" ]]; do /bin/sleep 0.01; done\n"
                "printf '%s\\n' \"$grandchild_pid\" > \"$RUNNER_GRANDCHILD_PID_FILE\"\n"
                ": > \"$RUNNER_CHILD_READY\"\n"
                "wait \"$grandchild_pid\"\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o644)
            self._run(["git", "init", "-b", "main"], cwd=source)
            self._run(["git", "add", "."], cwd=source)
            self._run(
                [
                    "git", "-c", "user.name=MBD Test", "-c", "user.email=mbd-test@example.invalid",
                    "commit", "-m", "fixture",
                ],
                cwd=source,
            )
            env = os.environ.copy()
            env.update(
                {
                    "MBD_H2_CLONE_URL": str(source),
                    "MBD_H2_RUNTIME_PARENT": str(runtime_parent),
                    "MBD_H2_LOG_DIR": str(root / "logs"),
                    "RUNNER_CHILD_READY": str(ready),
                    "RUNNER_GRANDCHILD_PID_FILE": str(grandchild_pid_file),
                    "RUNNER_ESCAPED_READY": str(escaped_ready),
                }
            )
            for escape_descendant in (False, True):
                env["RUNNER_ESCAPE_DESCENDANT"] = "1" if escape_descendant else "0"
                for signal_value, expected_rc in (
                    (signal.SIGTERM, 143),
                    (signal.SIGHUP, 129),
                    (signal.SIGINT, 130),
                ):
                    with self.subTest(escape_descendant=escape_descendant, signal=signal_value):
                        ready.unlink(missing_ok=True)
                        grandchild_pid_file.unlink(missing_ok=True)
                        escaped_ready.unlink(missing_ok=True)
                        process = subprocess.Popen(
                            ["/bin/bash", str(RUNNER)],
                            env=env,
                            text=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                        timed_out = False
                        grandchild_pid = None
                        grandchild_running = True
                        try:
                            for _ in range(100):
                                if ready.exists():
                                    break
                                time.sleep(0.02)
                            self.assertTrue(ready.exists(), "daily wrapper did not reach interruption point")
                            grandchild_pid = int(grandchild_pid_file.read_text(encoding="utf-8").strip())
                            process.send_signal(signal_value)
                            try:
                                process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                timed_out = True
                            for _ in range(40):
                                state = self._run(
                                    ["/bin/ps", "-o", "stat=", "-p", str(grandchild_pid)],
                                    check=False,
                                ).stdout.strip()
                                if not state or state.startswith("Z"):
                                    grandchild_running = False
                                    break
                                time.sleep(0.05)
                        finally:
                            if process.poll() is None:
                                os.killpg(process.pid, signal.SIGKILL)
                                process.wait(timeout=5)
                            if grandchild_pid is not None and grandchild_running:
                                try:
                                    os.kill(grandchild_pid, signal.SIGKILL)
                                except ProcessLookupError:
                                    grandchild_running = False
                        self.assertFalse(timed_out, "runner deferred parent-only signal while child remained active")
                        self.assertFalse(grandchild_running, "runner left a child or escaped descendant alive")
                        self.assertEqual(process.returncode, expected_rc)
                        self.assertEqual(list(runtime_parent.glob("mbd-h2-pages-refresh.*")), [])

    def test_daily_wrapper_fails_fast_when_github_auth_switch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            self._write_inert_daily_wrapper_fixture(source)
            self._run(["git", "init", "-b", "main"], cwd=source)
            self._run(["git", "add", "."], cwd=source)
            self._run(
                [
                    "git", "-c", "user.name=MBD Test", "-c", "user.email=mbd-test@example.invalid",
                    "commit", "-m", "fixture",
                ],
                cwd=source,
            )

            py_shim = root / "python-shim"
            py_shim.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "if sys.argv[1:2] == ['scripts/refresh_live_daily_from_duckdb.py']:\n"
                "    pathlib.Path('index.html').write_text('<html>changed</html>\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            py_shim.chmod(0o755)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            gh_log = root / "gh.log"
            gh_shim = fake_bin / "gh"
            gh_shim.write_text(
                "#!/bin/bash\n"
                "printf '%s\\n' \"$*\" >> \"$GH_SHIM_LOG\"\n"
                "exit 86\n",
                encoding="utf-8",
            )
            gh_shim.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "MBD_H2_CLONE_URL": str(source),
                    "MBD_H2_RUNTIME_PARENT": str(root / "runtime"),
                    "MBD_H2_LOG_DIR": str(root / "logs"),
                    "MBD_H2_PYTHON": str(py_shim),
                    "MBD_H2_RETRY_SLEEP_SECONDS": "0",
                    "MBD_H2_MAX_RUNTIME_SECONDS": "60",
                    "GH_SHIM_LOG": str(gh_log),
                    "PATH": f"{fake_bin}:{env['PATH']}",
                }
            )
            result = self._run(["/bin/bash", str(RUNNER)], env=env, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stage=github_auth_context", result.stderr)
            self.assertEqual(gh_log.read_text(encoding="utf-8").strip(), "auth switch -u been3p-prog")

    def _exercise_push_helper(self, mode: str, *, max_attempts: int = 2):
        self.assertTrue(RETRY_LIB.is_file(), f"missing retry library: {RETRY_LIB}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_git = root / "git"
            calls = root / "calls.log"
            log = root / "push.log"
            credential_marker = "DO_NOT_ECHO_PUSH_CREDENTIAL"
            expected_sha = "1" * 40
            base_sha = "2" * 40
            moved_sha = "3" * 40
            fake_git.write_text(
                "#!/bin/bash\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_GIT_CALLS\"\n"
                "if [[ \"$1\" == 'push' ]]; then\n"
                "  if [[ \"$FAKE_GIT_MODE\" == 'success' ]]; then exit 0; fi\n"
                "  if [[ \"$FAKE_GIT_MODE\" == 'nontransient' ]]; then\n"
                "    printf 'remote rejected https://x-access-token:%s@example.invalid/repo.git\\n' \"$FAKE_GIT_CREDENTIAL\" >&2\n"
                "    exit 1\n"
                "  fi\n"
                "  if [[ \"$FAKE_GIT_MODE\" == 'push_auth_and_transient' ]]; then\n"
                "    printf 'Permission denied; Operation timed out https://x-access-token:%s@example.invalid/repo.git\\n' \"$FAKE_GIT_CREDENTIAL\" >&2\n"
                "    exit 1\n"
                "  fi\n"
                "  printf 'Operation timed out https://x-access-token:%s@example.invalid/repo.git\\n' \"$FAKE_GIT_CREDENTIAL\" >&2\n"
                "  exit 1\n"
                "fi\n"
                "if [[ \"$1\" == 'ls-remote' ]]; then\n"
                "  case \"$FAKE_GIT_MODE\" in\n"
                "    remote_match) printf '%s\\trefs/heads/main\\n' \"$FAKE_EXPECTED_SHA\" ;;\n"
                "    remote_match_warning)\n"
                "      printf 'warning https://x-access-token:%s@example.invalid/repo.git\\n' \"$FAKE_GIT_CREDENTIAL\" >&2\n"
                "      printf '%s\\trefs/heads/main\\n' \"$FAKE_EXPECTED_SHA\"\n"
                "      ;;\n"
                "    remote_moved) printf '%s\\trefs/heads/main\\n' \"$FAKE_MOVED_SHA\" ;;\n"
                "    nontransient|push_auth_and_transient) printf '%s\\trefs/heads/main\\n' \"$FAKE_BASE_SHA\" ;;\n"
                "    ls_remote_failure)\n"
                "      printf 'ls-remote failed https://x-access-token:%s@example.invalid/repo.git\\n' \"$FAKE_GIT_CREDENTIAL\" >&2\n"
                "      exit 2\n"
                "      ;;\n"
                "    ls_remote_auth)\n"
                "      printf 'Permission denied https://x-access-token:%s@example.invalid/repo.git\\n' \"$FAKE_GIT_CREDENTIAL\" >&2\n"
                "      exit 77\n"
                "      ;;\n"
                "  esac\n"
                "  exit 0\n"
                "fi\n"
                "exit 64\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "MBD_H2_GIT_BIN": str(fake_git),
                    "MBD_H2_MAX_ATTEMPTS": str(max_attempts),
                    "MBD_H2_RETRY_SLEEP_SECONDS": "0",
                    "MBD_H2_MAX_RUNTIME_SECONDS": "60",
                    "FAKE_GIT_CALLS": str(calls),
                    "FAKE_GIT_MODE": mode,
                    "FAKE_GIT_CREDENTIAL": credential_marker,
                    "FAKE_EXPECTED_SHA": expected_sha,
                    "FAKE_BASE_SHA": base_sha,
                    "FAKE_MOVED_SHA": moved_sha,
                }
            )
            script = (
                f"source {RETRY_LIB!s}; "
                f"mbd_h2_push_expected_commit push {log!s} {expected_sha} {base_sha}"
            )
            result = self._run(["/bin/bash", "-c", script], env=env, check=False)
            call_lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
            log_text = log.read_text(encoding="utf-8")
            self.assertNotIn(credential_marker, log_text)
            self.assertNotIn("x-access-token", log_text)
            return result, call_lines, log_text

    def test_push_helper_returns_success_without_remote_probe(self):
        result, calls, _ = self._exercise_push_helper("success")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, ["push origin main"])

    def test_push_helper_accepts_ambiguous_push_when_remote_matches(self):
        result, calls, log_text = self._exercise_push_helper("remote_match")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, ["push origin main", "ls-remote origin refs/heads/main"])
        self.assertIn("remote already matches expected commit; recovered", log_text)

    def test_push_helper_ignores_credential_warning_before_matching_sha(self):
        result, calls, log_text = self._exercise_push_helper("remote_match_warning")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, ["push origin main", "ls-remote origin refs/heads/main"])
        self.assertIn("remote already matches expected commit; recovered", log_text)

    def test_push_helper_fails_when_remote_moved(self):
        result, calls, log_text = self._exercise_push_helper("remote_moved")
        self.assertEqual(result.returncode, 65)
        self.assertEqual(calls, ["push origin main", "ls-remote origin refs/heads/main"])
        self.assertIn("remote moved to unexpected commit", log_text)

    def test_push_helper_bounds_retry_when_remote_readback_fails(self):
        result, calls, log_text = self._exercise_push_helper("ls_remote_failure")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            calls,
            [
                "push origin main",
                "ls-remote origin refs/heads/main",
                "push origin main",
                "ls-remote origin refs/heads/main",
            ],
        )
        self.assertIn("transient push retry exhausted after 2/2 attempts", log_text)

    def test_push_helper_fails_fast_on_nontransient_rejection(self):
        result, calls, log_text = self._exercise_push_helper("nontransient")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, ["push origin main", "ls-remote origin refs/heads/main"])
        self.assertIn("non-transient push failure; no retry", log_text)

    def test_push_helper_fails_fast_when_remote_readback_auth_fails(self):
        result, calls, log_text = self._exercise_push_helper("ls_remote_auth")
        self.assertEqual(result.returncode, 77)
        self.assertEqual(calls, ["push origin main", "ls-remote origin refs/heads/main"])
        self.assertIn("non-transient remote readback auth failure; no retry rc=77", log_text)

    def test_push_helper_fails_fast_when_push_contains_auth_and_transient_text(self):
        result, calls, log_text = self._exercise_push_helper("push_auth_and_transient")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, ["push origin main", "ls-remote origin refs/heads/main"])
        self.assertIn("non-transient push auth failure; no retry rc=1", log_text)

    def test_generic_retry_signal_interruption_leaves_no_raw_attempt_log(self):
        self.assertTrue(RETRY_LIB.is_file(), f"missing retry library: {RETRY_LIB}")
        for signal_value in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(signal=signal_value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                temp_dir = root / "tmp"
                temp_dir.mkdir()
                ready = root / "ready"
                log = root / "retry.log"
                command = root / "retry-command"
                credential_marker = "DO_NOT_PERSIST_INTERRUPTED_CREDENTIAL"
                command.write_text(
                    "#!/bin/bash\n"
                    "printf 'Operation timed out https://x-access-token:%s@example.invalid/repo.git\\n' \"$RETRY_CREDENTIAL\" >&2\n"
                    ": > \"$RETRY_READY\"\n"
                    "/bin/sleep 30\n"
                    "exit 1\n",
                    encoding="utf-8",
                )
                command.chmod(0o755)
                env = os.environ.copy()
                env.update(
                    {
                        "TMPDIR": str(temp_dir),
                        "MBD_H2_MAX_ATTEMPTS": "2",
                        "MBD_H2_RETRY_SLEEP_SECONDS": "0",
                        "MBD_H2_MAX_RUNTIME_SECONDS": "60",
                        "RETRY_READY": str(ready),
                        "RETRY_CREDENTIAL": credential_marker,
                    }
                )
                script = (
                    f"source {RETRY_LIB!s}; "
                    f"mbd_h2_run_with_retry git_fetch {log!s} {command!s}"
                )
                process = subprocess.Popen(
                    ["/bin/bash", "-c", script],
                    env=env,
                    text=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                try:
                    for _ in range(100):
                        if ready.exists():
                            break
                        time.sleep(0.02)
                    self.assertTrue(ready.exists(), "retry command did not reach interruption point")
                    os.killpg(process.pid, signal_value)
                    process.wait(timeout=5)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                self.assertEqual(list(temp_dir.glob("mbd-h2-git_fetch.*")), [])

    def test_retry_library_retries_allowlisted_network_failure(self):
        self.assertTrue(RETRY_LIB.is_file(), f"missing retry library: {RETRY_LIB}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counter = root / "counter"
            log = root / "retry.log"
            command = root / "flaky.sh"
            command.write_text(
                "#!/bin/bash\n"
                "n=0; [[ -f \"$1\" ]] && n=$(cat \"$1\")\n"
                "n=$((n+1)); printf '%s' \"$n\" > \"$1\"\n"
                "if [[ \"$n\" -eq 1 ]]; then\n"
                "  echo 'transient_network: Operation timed out' >&2\n"
                "  exit 1\n"
                "fi\n",
                encoding="utf-8",
            )
            command.chmod(0o755)
            script = (
                f"source {RETRY_LIB!s}; "
                "export MBD_H2_MAX_ATTEMPTS=3 MBD_H2_RETRY_SLEEP_SECONDS=0 "
                "MBD_H2_MAX_RUNTIME_SECONDS=60 MBD_H2_RUN_STARTED_EPOCH=$(/bin/date +%s); "
                f"mbd_h2_run_with_retry probe {log!s} {command!s} {counter!s}"
            )
            self._run(["/bin/bash", "-c", script])
            self.assertEqual(counter.read_text(), "2")
            self.assertIn("transient failure; retrying", log.read_text())

    def test_retry_library_fails_fast_on_auth_failure(self):
        self.assertTrue(RETRY_LIB.is_file(), f"missing retry library: {RETRY_LIB}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counter = root / "counter"
            log = root / "retry.log"
            command = root / "auth_fail.sh"
            command.write_text(
                "#!/bin/bash\n"
                "n=0; [[ -f \"$1\" ]] && n=$(cat \"$1\")\n"
                "n=$((n+1)); printf '%s' \"$n\" > \"$1\"\n"
                "echo 'non_transient_auth: Permission denied' >&2\n"
                "exit 1\n",
                encoding="utf-8",
            )
            command.chmod(0o755)
            script = (
                f"source {RETRY_LIB!s}; "
                "export MBD_H2_MAX_ATTEMPTS=3 MBD_H2_RETRY_SLEEP_SECONDS=0 "
                "MBD_H2_MAX_RUNTIME_SECONDS=60 MBD_H2_RUN_STARTED_EPOCH=$(/bin/date +%s); "
                f"mbd_h2_run_with_retry probe {log!s} {command!s} {counter!s}"
            )
            result = self._run(["/bin/bash", "-c", script], check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(counter.read_text(), "1")
            self.assertIn("non-transient failure; no retry", log.read_text())

    def test_retry_library_fails_fast_on_generic_public_readback_failure(self):
        self.assertTrue(RETRY_LIB.is_file(), f"missing retry library: {RETRY_LIB}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counter = root / "counter"
            log = root / "retry.log"
            command = root / "public_readback_contract_fail.sh"
            command.write_text(
                "#!/bin/bash\n"
                "n=0; [[ -f \"$1\" ]] && n=$(cat \"$1\")\n"
                "n=$((n+1)); printf '%s' \"$n\" > \"$1\"\n"
                "echo 'ERROR: public readback failed deterministic contract mismatch' >&2\n"
                "exit 1\n",
                encoding="utf-8",
            )
            command.chmod(0o755)
            script = (
                f"source {RETRY_LIB!s}; "
                "export MBD_H2_MAX_ATTEMPTS=3 MBD_H2_RETRY_SLEEP_SECONDS=0 "
                "MBD_H2_MAX_RUNTIME_SECONDS=60 MBD_H2_RUN_STARTED_EPOCH=$(/bin/date +%s); "
                f"mbd_h2_run_with_retry public_readback {log!s} {command!s} {counter!s}"
            )
            result = self._run(["/bin/bash", "-c", script], check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(counter.read_text(), "1")
            self.assertIn("non-transient failure; no retry", log.read_text())

    def test_retry_library_retries_explicit_pages_stale_public_readback_marker(self):
        self.assertTrue(RETRY_LIB.is_file(), f"missing retry library: {RETRY_LIB}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counter = root / "counter"
            log = root / "retry.log"
            command = root / "pages_stale.sh"
            command.write_text(
                "#!/bin/bash\n"
                "n=0; [[ -f \"$1\" ]] && n=$(cat \"$1\")\n"
                "n=$((n+1)); printf '%s' \"$n\" > \"$1\"\n"
                "if [[ \"$n\" -eq 1 ]]; then\n"
                "  echo 'MBD_H2_PAGES_STALE_PUBLIC_READBACK: public bytes differ from committed artifact' >&2\n"
                "  exit 1\n"
                "fi\n",
                encoding="utf-8",
            )
            command.chmod(0o755)
            script = (
                f"source {RETRY_LIB!s}; "
                "export MBD_H2_MAX_ATTEMPTS=3 MBD_H2_RETRY_SLEEP_SECONDS=0 "
                "MBD_H2_MAX_RUNTIME_SECONDS=60 MBD_H2_RUN_STARTED_EPOCH=$(/bin/date +%s); "
                f"mbd_h2_run_with_retry public_readback {log!s} {command!s} {counter!s}"
            )
            self._run(["/bin/bash", "-c", script])
            self.assertEqual(counter.read_text(), "2")
            self.assertIn("transient failure; retrying", log.read_text())

    def _transport_module(self):
        self.assertTrue(TRANSPORT.is_file(), f"missing transport helper: {TRANSPORT}")
        spec = importlib.util.spec_from_file_location("snapshot_transport_test", TRANSPORT)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def _script_module(self, relative: str):
        path = REPO / relative
        self.assertTrue(path.is_file(), f"missing script: {path}")
        script_dir = str(REPO / "scripts")
        sys.path.insert(0, script_dir)
        try:
            spec = importlib.util.spec_from_file_location(f"{path.stem}_test", path)
            assert spec is not None
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            return module
        finally:
            sys.path.remove(script_dir)

    def _assert_no_transport_secret(self, detail: str, secrets: tuple[str, ...]) -> None:
        for secret in secrets:
            self.assertNotIn(secret, detail)
        self.assertNotIn("ssh -i", detail)
        self.assertNotIn("scp", detail)

    def test_transport_diagnostics_allowlist_network_and_redact_context(self):
        transport = self._transport_module()
        detail = transport.describe_transport_failure(
            255,
            "ssh: connect to host 192.168.7.238 port 22: Operation timed out; key=/secret/key",
        )
        self.assertEqual(detail, "transient_network: Operation timed out rc=255")
        self.assertNotIn("192.168.7.238", detail)
        self.assertNotIn("/secret/key", detail)

    def test_transport_diagnostics_fail_fast_on_auth_error(self):
        transport = self._transport_module()
        detail = transport.describe_transport_failure(
            255,
            "cnc-media@192.168.7.238: Permission denied (publickey).",
        )
        self.assertEqual(detail, "non_transient_auth: Permission denied rc=255")
        self.assertNotIn("192.168.7.238", detail)

    def _exercise_fetcher_transport_exception(self, relative: str, fake_run) -> tuple[str, list[list[str]]]:
        module = self._script_module(relative)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "keys" / "id_secret_key"
            key.parent.mkdir()
            key.write_text("not a real key\n", encoding="utf-8")
            output = root / "snapshot.duckdb"
            host = "secret-host.example.invalid"
            remote_python = "/remote/secret/python"
            remote_db = "/remote/secret/source.duckdb"
            secrets = (str(key), host, remote_python, remote_db)
            calls: list[list[str]] = []

            def recording_run(args, **kwargs):
                calls.append([str(arg) for arg in args])
                return fake_run(args, kwargs, calls)

            def fail_validate(path):
                self.fail(f"validate_snapshot should not run after transport exception: {path}")

            original_run = module.subprocess.run
            module.subprocess.run = recording_run
            module.validate_snapshot = fail_validate
            try:
                with self.assertRaises(RuntimeError) as raised:
                    module.sync_snapshot(
                        output,
                        host=host,
                        key=key,
                        remote_python=remote_python,
                        remote_db=remote_db,
                    )
            finally:
                module.subprocess.run = original_run
            detail = str(raised.exception)
            self.assertTrue(raised.exception.__suppress_context__)
            self._assert_no_transport_secret(detail, secrets)
            self.assertEqual(list(root.glob("snapshot.duckdb.*.partial")), [])
            return detail, calls

    def test_snapshot_fetcher_copy_timeouts_are_secret_safe_transient_failures(self):
        for relative in (
            "scripts/fetch_target_youtube_snapshot.py",
            "scripts/fetch_target_mbd_snapshot.py",
        ):
            with self.subTest(fetcher=relative):
                def fake_run(args, kwargs, calls):
                    if len(calls) == 1:
                        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))
                    return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

                detail, calls = self._exercise_fetcher_transport_exception(relative, fake_run)
                self.assertIn("copy failed: transient_network: Operation timed out", detail)
                self.assertEqual(len(calls), 2)
                self.assertIn("/bin/rm", calls[-1])

    def test_snapshot_fetcher_transfer_timeouts_are_secret_safe_transient_failures(self):
        for relative in (
            "scripts/fetch_target_youtube_snapshot.py",
            "scripts/fetch_target_mbd_snapshot.py",
        ):
            with self.subTest(fetcher=relative):
                def fake_run(args, kwargs, calls):
                    if len(calls) == 2:
                        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))
                    return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

                detail, calls = self._exercise_fetcher_transport_exception(relative, fake_run)
                self.assertIn("transfer failed: transient_network: Operation timed out", detail)
                self.assertEqual(len(calls), 3)
                self.assertIn("/bin/rm", calls[-1])

    def test_snapshot_fetcher_unknown_oserror_is_secret_safe_non_transient_failure(self):
        for relative in (
            "scripts/fetch_target_youtube_snapshot.py",
            "scripts/fetch_target_mbd_snapshot.py",
        ):
            with self.subTest(fetcher=relative):
                def fake_run(args, kwargs, calls):
                    if len(calls) == 1:
                        raise OSError(f"spawn failed for raw argv {args!r}")
                    return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

                detail, calls = self._exercise_fetcher_transport_exception(relative, fake_run)
                self.assertIn("copy failed: non_transient_transport: command failed", detail)
                self.assertEqual(len(calls), 2)
                self.assertIn("/bin/rm", calls[-1])

    def test_snapshot_fetcher_cleanup_oserror_after_success_is_secret_safe(self):
        for relative in (
            "scripts/fetch_target_youtube_snapshot.py",
            "scripts/fetch_target_mbd_snapshot.py",
        ):
            with self.subTest(fetcher=relative), tempfile.TemporaryDirectory() as tmp:
                module = self._script_module(relative)
                root = Path(tmp)
                key = root / "keys" / "id_secret_key"
                key.parent.mkdir()
                key.write_text("not a real key\n", encoding="utf-8")
                output = root / "snapshot.duckdb"
                host = "secret-host.example.invalid"
                remote_python = "/remote/secret/python"
                remote_db = "/remote/secret/source.duckdb"
                calls = 0

                def fake_run(args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        Path(args[-1]).write_bytes(b"snapshot")
                    if calls == 3:
                        raise OSError(f"cleanup failed for raw argv {args!r}")
                    return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

                original_run = module.subprocess.run
                original_validate = getattr(module, "validate_snapshot")
                module.subprocess.run = fake_run
                setattr(module, "validate_snapshot", lambda path: {"fixture": True})
                try:
                    with self.assertRaises(RuntimeError) as raised:
                        module.sync_snapshot(
                            output,
                            host=host,
                            key=key,
                            remote_python=remote_python,
                            remote_db=remote_db,
                        )
                finally:
                    module.subprocess.run = original_run
                    setattr(module, "validate_snapshot", original_validate)
                detail = str(raised.exception)
                self.assertTrue(raised.exception.__suppress_context__)
                self.assertIsNone(raised.exception.__cause__)
                self._assert_no_transport_secret(
                    detail,
                    (str(key), host, remote_python, remote_db),
                )
                self.assertIn("temp cleanup failed: non_transient_transport: command failed", detail)

    def test_snapshot_fetcher_missing_key_error_does_not_echo_path(self):
        for relative in (
            "scripts/fetch_target_youtube_snapshot.py",
            "scripts/fetch_target_mbd_snapshot.py",
        ):
            with self.subTest(fetcher=relative), tempfile.TemporaryDirectory() as tmp:
                module = self._script_module(relative)
                missing_key = Path(tmp) / "secret" / "id_private_key"
                with self.assertRaises(RuntimeError) as raised:
                    module.sync_snapshot(Path(tmp) / "snapshot.duckdb", key=missing_key)
                self.assertNotIn(str(missing_key), str(raised.exception))
                self.assertEqual(str(raised.exception), "target SSH key missing")

    def test_daily_wrapper_honors_isolated_root_and_repo_retry_library(self):
        wrapper = (REPO / "ops" / "mbd_h2_pages_live_daily_refresh.sh").read_text(encoding="utf-8")
        self.assertIn('ROOT="${MBD_H2_ROOT:-/Users/sb.lee/automations/mbd-h2-kr-dashboard}"', wrapper)
        self.assertIn('LOG_DIR="${MBD_H2_LOG_DIR:-$ROOT/logs}"', wrapper)
        self.assertIn('RETRY_LIB="${MBD_H2_RETRY_LIB:-$ROOT/ops/mbd_h2_pages_retry.sh}"', wrapper)
        self.assertNotIn('"$PY" -m unittest scripts/test_ops_runtime.py -v', wrapper)
        self.assertNotIn(".hermes/scripts/lib/mbd_h2_pages_retry.sh", wrapper)
        self.assertIn("MBD_H2_PAGES_STALE_PUBLIC_READBACK", wrapper)
        self.assertIn("ERROR: public readback contract failed", wrapper)
        self.assertNotIn("ERROR: public readback failed", wrapper)

    def test_daily_wrapper_never_uses_retired_company_mac_mbd_db(self):
        wrapper = (REPO / "ops" / "mbd_h2_pages_live_daily_refresh.sh").read_text(encoding="utf-8")
        self.assertIn("scripts/fetch_target_mbd_snapshot.py", wrapper)
        self.assertIn('--duckdb "$MBD_SNAPSHOT_CACHE"', wrapper)
        self.assertNotIn("/Users/sb.lee/automations/mbd/mbd.duckdb", wrapper)


if __name__ == "__main__":
    unittest.main()
