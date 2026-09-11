# Bot parity release check — 2026-09-11

## Released and verified

- Dashboard implementation revision: `c8398d9ec1ba90118c44045e666db2d34f25a168`.
- GitHub Actions verify/deploy run `34598294576`: success.
- Public HTML SHA256: `dcf5a75b870efe793b870977c50ff35b94db0fc46b4b4da29a81563f074b1564`.
- Seven runtime code files installed with pre-write baseline hash checks, code
  backups, and exact post-write checksum readback. Only three bot processes
  restarted; all reported running.
- Private shared cache published only after exact public HTML readback, mode 0600.
- Local regression suite: 258 tests passed, including 36 parity tests. Browser
  guards passed at 1440px and 390px without overflow. Python, Node and shell
  syntax checks passed. `git diff --check` passed.
- Deployed shared client, including its real public-version/freshness gate:
  all 13 monthly, weekly, daily, brand/detail and schedule cases exactly matched
  local expected answers. This is a runtime calculation check, not Slack E2E.
- Actual Slack readback in **been_jobs only**: YouTube monthly, weekly, daily,
  detail and schedule replies all matched (5/5). Daily equality includes the
  explicit unavailable daily-channel-total disclosure; it does not establish a
  daily Analytics source.

## Not yet verified

Live and ad bots did not answer app-authored test messages. The existing Slack
connection attaches `bot_id` even with `as_user=true`; their existing bot-loop
guards deliberately ignore those messages. Those guards were not weakened.
Native user-message testing is pending computer-use permission. Do not label
these two Slack delivery routes E2E-passed based on process state or engine tests.

No test messages were sent to any other channel. No source data, authorization,
membership, cron, gateway, or Hermes state was changed. Original source gaps
(Live 1P/3P attribution and YouTube daily channel Analytics) remain explicit.
The next naturally scheduled daily refresh has not yet been observed.

## Reproducible probes

`integrations/test_slack_parity.py` requires `--send`, verifies the exact been_jobs
channel, and stores receipt metadata without raw replies or credentials. The
default stops immediately if Slack marks a message app-authored. The explicit
`--domain youtube --allow-app-authored` option tests only the YouTube route that
accepts this transport. Never disable a production bot-loop guard for testing.

The deployment tool is baseline-specific: after installation, rerunning its
first-release preconditions intentionally fails. A subsequent runtime update or
rollback needs reviewed current checksums, exact-target readback and the same
three-process restart boundary; it must not blindly overwrite newer code.
