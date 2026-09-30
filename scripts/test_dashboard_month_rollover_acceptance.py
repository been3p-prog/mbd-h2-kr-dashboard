# [2026-09-30] Deterministic 2026-10-01 month-rollover acceptance.
#   Exercises the real production functions (no network, no source DB writes, no
#   git writes) against an in-memory copy of the checked-in index.html to prove:
#     - default_month advances to 10 and September becomes historical
#       (마감 확인 필요/pending_close), never fabricated as finalized/closed.
#     - August, the actually-closed month, is untouched by the rollover.
#     - October's current-month RAW and YouTube surfaces hydrate as structurally
#       valid empty placeholders under both renderer modules.
#     - Neither renderer module hardcodes a September-only assumption (both
#       accept month=10 without special-casing 9).
#     - The inverse case: once September has been legitimately sealed (label
#       ends " · 확정" and every mvk/mvs/mvr surface is already data-phase=
#       "closed", e.g. by finalize_month_review), a later rollover call must
#       preserve that accepted close rather than re-deriving pending_close from
#       month order alone (see the `sealed` check in
#       dashboard_period_state.update_default_month_state).
import datetime as dt
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dashboard_period_state as period  # noqa: E402
import refresh_live_daily_from_duckdb as daily  # noqa: E402
import refresh_owned_youtube_window_from_duckdb as owned  # noqa: E402
import verify_dashboard as guard  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _force_month_state(html: str, month: int, phase_label: str, phase: str) -> str:
    """Force one month's selector option + mvk/mvs/mvr phase to an explicit,
    self-contained state, independent of whatever that month's phase happens
    to be in the checked-in artifact at the moment this runs."""
    html, option_count = re.subn(
        rf'<option value="{month}"(?: selected)?>([^<]+) · [^<]+</option>',
        lambda m: f'<option value="{month}">{m.group(1)} · {phase_label}</option>',
        html, count=1,
    )
    if option_count != 1:
        raise RuntimeError(f'month {month} option not found to force')
    html, phase_count = re.subn(
        rf'(class="(?:mvk|mvs|mvr) mv" data-m="{month}" data-phase=")[^"]+(")',
        rf'\g<1>{phase}\g<2>', html,
    )
    if phase_count != 3:
        raise RuntimeError(f'month {month} phase surfaces mismatch: {phase_count}')
    return html


class OctoberRolloverAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "index.html").read_text(encoding="utf-8")

    def test_default_month_advances_and_september_becomes_historical_not_closed(self):
        for renderer in (daily, owned):
            # [2026-09-30] Force September to an explicit unclosed ('current')
            #   baseline before rolling to October — independent of whatever
            #   September's momentary phase is in the checked-in artifact
            #   (current, pending_close, or already sealed) at test-run time.
            unclosed = _force_month_state(self.source, 9, '진행 중', 'current')
            rolled = renderer.update_default_month_state(unclosed, 10)
            self.assertRegex(rolled, r"\bvar CUR = 10;")
            self.assertIn('<option value="10" selected>2026년 10월 · 진행 중</option>', rolled)
            self.assertIn('<option value="9">2026년 9월 · 마감 확인 필요</option>', rolled)
            self.assertIn('<option value="8">2026년 8월 · 확정</option>', rolled)
            self.assertIn('class="mvk mv" data-m="10" data-phase="current"', rolled)
            self.assertIn('class="mvk mv" data-m="9" data-phase="pending_close"', rolled)
            self.assertIn('class="mvk mv" data-m="8" data-phase="closed"', rolled)
            # September must never be relabeled 확정/closed purely from month order.
            self.assertNotIn('<option value="9">2026년 9월 · 확정</option>', rolled)
            self.assertNotIn('class="mvk mv" data-m="9" data-phase="closed"', rolled)
            # August's finalized surfaces are untouched by the rollover.
            self.assertEqual(
                guard._month_surface(self.source, "mvr", 8),
                guard._month_surface(rolled, "mvr", 8),
            )
            # Idempotent: re-applying the same rollover is a no-op.
            self.assertEqual(renderer.update_default_month_state(rolled, 10), rolled)

    def test_accepted_closed_september_survives_october_rollover(self):
        for renderer in (daily, owned):
            # [2026-09-30] Construct an explicit accepted-closed September
            #   baseline directly — independent of whatever September's
            #   momentary phase is in the checked-in artifact — the way an
            #   accepted month-close (finalize_month_review) would leave it:
            #   option label ends " · 확정" and every mvk/mvs/mvr September
            #   surface is data-phase="closed".
            sealed = _force_month_state(self.source, 9, '확정', 'closed')
            # Sanity: the fixture really matches the accepted-close precondition.
            self.assertIn('<option value="9">2026년 9월 · 확정</option>', sealed)
            self.assertEqual(
                set(re.findall(
                    r'class="(?:mvk|mvs|mvr) mv" data-m="9" data-phase="([^"]+)"', sealed)),
                {"closed"},
            )

            resealed = renderer.update_default_month_state(sealed, 10)
            self.assertIn('<option value="9">2026년 9월 · 확정</option>', resealed)
            self.assertIn('class="mvk mv" data-m="9" data-phase="closed"', resealed)
            self.assertIn('class="mvs mv" data-m="9" data-phase="closed"', resealed)
            self.assertIn('class="mvr mv" data-m="9" data-phase="closed"', resealed)
            self.assertNotIn('<option value="9">2026년 9월 · 마감 확인 필요</option>', resealed)
            self.assertNotIn('class="mvk mv" data-m="9" data-phase="pending_close"', resealed)
            # October and the rest of the calendar are unaffected by the seal.
            self.assertIn('<option value="10" selected>2026년 10월 · 진행 중</option>', resealed)
            self.assertIn('class="mvk mv" data-m="10" data-phase="current"', resealed)
            self.assertIn('<option value="8">2026년 8월 · 확정</option>', resealed)
            self.assertIn('class="mvk mv" data-m="8" data-phase="closed"', resealed)
            # Idempotent under repeated application, same as the pending-close path.
            self.assertEqual(renderer.update_default_month_state(resealed, 10), resealed)

    def test_october_current_raw_surface_hydrates_as_valid_empty_placeholder(self):
        rolled = daily.update_default_month_state(self.source, 10)
        snapshot = {
            "as_of": "2026-10-01",
            "range_label": "10/1~10/1",
            "ad_gen_won": 0,
            "ad_int_won": 0,
            "live_won": 0,
            "target_won": 1_365_682_548,
            "team_targets_won": {"ad_gen": 900_000_000, "ad_int": 250_000_000, "live": 215_682_548},
            "total_won": 0,
            "progress_pct": 0.0,
        }
        updated = daily.update_current_raw_surfaces(rolled, snapshot)

        month10_top = updated.split('class="mvk mv" data-m="10"', 1)[1].split(
            'class="mvk mv" data-m="11"', 1
        )[0]
        month10_detail = updated.split('class="mvr mv" data-m="10"', 1)[1].split(
            'class="mvr mv" data-m="11"', 1
        )[0]
        self.assertIn('data-phase="current"', month10_top)
        self.assertIn('data-current-raw-empty="true"', month10_top)
        self.assertIn("현재 RAW 누적 · 10/1~10/1", month10_top)
        self.assertIn('<div class="v num">0</div>', month10_top)
        self.assertIn("MoM —", month10_top)
        self.assertEqual(month10_detail.count("RAW 누적 · 10/1~10/1"), 3)
        for team_key in ("ad_gen", "ad_int", "live"):
            self.assertIn(f'data-current-raw-team-empty="{team_key}"', month10_detail)
        self.assertIn("진척 0.0%", month10_detail)
        # Idempotent re-hydration and September's own surfaces stay intact.
        self.assertEqual(updated, daily.update_current_raw_surfaces(updated, snapshot))
        self.assertEqual(
            guard._month_surface(rolled, "mvr", 9),
            guard._month_surface(updated, "mvr", 9),
        )

    def test_october_youtube_window_hydrates_as_valid_empty_placeholder(self):
        rolled = owned.update_default_month_state(self.source, 10)
        month = {
            "period_start": dt.date(2026, 10, 1),
            "period_end": dt.date(2026, 10, 31),
            "metric_start_date": dt.date(2026, 10, 1),
            "metric_end_date": dt.date(2026, 10, 1),
            "period_complete": False,
            "views": 0,
            "new_views": 0,
            "prior_views": 0,
            "engagement": 0,
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "unknown_views": 0,
            "fetched_at": dt.datetime(2026, 10, 1, 0, 45),
            "raw_status": "awaiting_current_month_analytics",
        }

        section, contract = owned.render_section(
            month, [], {"total": 0}, [], dt.datetime(2026, 10, 1, 10, 0)
        )

        self.assertIn('data-yt-content-empty="true"', section)
        self.assertIn("D+N 완료 콘텐츠 없음", section)
        self.assertIn('data-yt-weekly-empty="true"', section)
        self.assertIn("주간 Analytics 집계 대기 중", section)
        self.assertIn("10월 금월 누적", section)

        # [2026-09-30] Actually merge the rendered section into the rolled
        #   artifact via replace_section instead of asserting an unrelated fact
        #   about `rolled` alone — proves the month-selector state
        #   update_default_month_state just set survives the section swap.
        merged = owned.replace_section(rolled, section)
        self.assertIn('<option value="10" selected>2026년 10월 · 진행 중</option>', merged)
        self.assertIn('class="mvk mv" data-m="10" data-phase="current"', merged)
        self.assertIn('data-yt-content-empty="true"', merged)
        self.assertIn('data-yt-weekly-empty="true"', merged)

        # The returned contract's essential October empty/current fields.
        self.assertEqual(contract["source"]["period_complete"], False)
        self.assertEqual(contract["source"]["raw_status"], "awaiting_current_month_analytics")
        self.assertEqual(contract["content_cards"], [])
        self.assertEqual(contract["weekly_rows"], [])


if __name__ == "__main__":
    unittest.main()
