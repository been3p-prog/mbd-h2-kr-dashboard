# YouTube API connection — 2026-09-11

## Scope and rollback

Read existing host-local YouTube credentials and canonical DB; enrich only a
disposable dashboard snapshot. No canonical DB/Sheet/auth/cron changes. The
existing snapshot → render → Pages → private cache flow acquires this overlay
on subsequent runs. The common answer engine is invoked per query, so its
single-file update requires no bot-process restart.

Runtime installer backs up the previous engine and private cache, validates the
exact previous hash, then verifies installed bytes. Rollback restores those exact
files and reverts the reviewed Pages commit; no destructive checkout/reset.

## Measured candidate

- API capture: 2026-09-11 22:19 KST; actual channel data through September 8,
  not the September 10 request end. Standalone September views: 1,439,009.
- Platform views: Shorts 633,239; videoOnDemand 722,914; liveStream 28,924;
  posts 53,932. Their sum and the daily sum reconcile for this period, but
  the implementation retains residual QA rather than assuming permanent parity.
- Registered September cohort: 16; verified completed D7: 3, pending: 13.
  Completed average: 15,266. August completed 25/25; average 54,902.
- Missing public uploads found: 8, all September (including September 2 Live).
  Connected as a separate visible list by verified ID/public status, not title
  matching or inferred LF/SF. The manual schedule remains 47 rows, 16 matched,
  22 planned, 3 unmatched, 3 community, 3 undetermined; 10 date-only slots separate.
- Live revenue attribution is not fabricated: source 1P/3P blanks remain
  explicitly excluded from strict 3P RAW. Nothing was written into source data.

## Verification

- Baseline 258 tests; candidate 287 tests passed.
- HTML freshness/scope guard, Live numeric contract, desktop 1440px/mobile 390px
  browser smoke passed with zero horizontal overflow.
- Additional browser checks: 8 visible discovery IDs in main/detail, 8 official
  September daily rows; final discovery reachable on mobile.
- Export re-renders exact HTML sections and checks the verified API payload hash
  against the public contract before emitting the private bot cache.
- Actual release acceptance additionally requires exact Pages byte readback,
  remote client/cache checksum readback and Slack replies in been_jobs only.
  A natural next scheduled tick is separate evidence, not implied by manual tests.

## Release readback

- Implementation revision `8e6a6a739f9561dfdaeab69b565d07b968b2adb0`;
  Actions `34604638149` verify/deploy succeeded.
- Public HTML SHA256 `37102412841589f636b771b438223a46bd0583b4c4364f594f023c597c4f9146`
  matched exactly before private cache publication.
- Shared engine and private cache were installed with exact checksum readback;
  all 15 deployed, public-version-gated question cases matched local answers.
- Seven actual YouTube replies were received in been_jobs. Four initially differed
  solely by Slack's `🔴`→`:red_circle:` / `🏠`→`:house:` conversion in public video
  titles. Only these exact transport aliases were added to the comparison; a
  regression test ensures numeric/date changes still fail comparison.
  Readback of the same seven threads then passed **7/7**, without resending.
  Final local regression suite including the transport test passed **288/288**.
- No tests were sent to other channels. Live/Ads actual Slack tests were not
  repeated in this release; their current deployed computation passed the shared
  15-case probe. A natural next scheduled refresh remains unobserved.
