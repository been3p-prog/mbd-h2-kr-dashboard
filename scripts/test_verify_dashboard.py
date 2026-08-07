# [2026-08-07] DOM-first LIVE 아티팩트 릴리스 가드 회귀 테스트.
#   기본은 repo index.html(배포 후 = 승인 골격). 로컬/CI 검증 시 MBD_DASHBOARD_HTML 로
#   /tmp LIVE 후보를 주입해 결정론적으로 돌린다(CI 기본은 약화하지 않음).
#   now 는 manifest built_at 에서 파생 → 배포일자에 관계없이 freshness 판정이 안정적.
import datetime as dt
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_dashboard as vd  # noqa: E402
import smoke_dashboard as sd  # noqa: E402


def _load_dashboard_html():
    override = os.environ.get("MBD_DASHBOARD_HTML")
    path = Path(override) if override else Path(__file__).resolve().parents[1] / "index.html"
    return path.read_text(encoding="utf-8"), str(path)


class DashboardGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html, cls.path = _load_dashboard_html()
        # manifest built_at 기준으로 fresh/stale now 파생 (아티팩트 실제 날짜와 무관하게 안정)
        _, manifest = vd.extract_manifest(cls.html)
        built = dt.datetime.fromisoformat(manifest["built_at_kst"])
        cls.now = built + dt.timedelta(hours=1)       # 모든 스탬프 48h 이내
        cls.stale_now = built + dt.timedelta(hours=100)  # 48h SLA 초과

    # ── 승인 골격 GREEN ─────────────────────────────────────────────
    def test_live_candidate_is_clean(self):
        self.assertEqual(vd.verify(self.html, self.now), [],
                         f"base artifact ({self.path}) must be a clean LIVE build")

    def test_live_candidate_passes_require_fresh(self):
        self.assertEqual(vd.verify(self.html, self.now, require_fresh=True), [])

    def test_scope_excludes_ogam(self):
        self.assertNotIn("ogam", vd.ALLOWED_REVENUE_TEAMS)
        self.assertEqual(vd.MANIFEST_SCOPE, ["ad_gen", "ad_int", "live"])

    # ── 구 payload 재출현 (negative control) ────────────────────────
    def test_legacy_top_summary_payload_reappearance_fails(self):
        bad = self.html.replace(
            "</body>", "<script>window.__TOP_SUMMARY_DATA__ = {}</script></body>", 1)
        self.assertTrue(any("legacy embedded payload" in e for e in vd.verify(bad, self.now)))

    def test_legacy_sot_payload_reappearance_fails(self):
        bad = self.html.replace(
            "</body>", "<script>window.__MBD_SOT_DATA__ = {}</script></body>", 1)
        self.assertTrue(any("legacy embedded payload" in e for e in vd.verify(bad, self.now)))

    # ── LIVE 마커 missing / duplicate / tampered ────────────────────
    def test_missing_live_marker_fails(self):
        bad = self.html.replace("LIVE 빌드", "", 1)
        self.assertTrue(any("LIVE marker" in e for e in vd.verify(bad, self.now)))

    def test_duplicate_live_marker_fails(self):
        bad = self.html.replace("LIVE 빌드", "LIVE 빌드 LIVE 빌드", 1)
        self.assertTrue(any("LIVE marker" in e and "2x" in e for e in vd.verify(bad, self.now)))

    def test_tampered_live_marker_reintroduces_pre_approval_fails(self):
        bad = self.html.replace("고정 URL 운영본", "승인 전 비공개", 1)
        errors = vd.verify(bad, self.now)
        self.assertTrue(any("pre-approval marker" in e for e in errors))
        self.assertTrue(any("LIVE marker" in e for e in errors))

    def test_staging_marker_reappearance_fails(self):
        bad = self.html.replace("LIVE 빌드", "STAGING 빌드", 1)
        self.assertTrue(any("pre-approval marker" in e for e in vd.verify(bad, self.now)))

    # ── 안전하지 않은 링크 (negative control) ───────────────────────
    def test_javascript_uri_fails(self):
        bad = self.html.replace("</body>", '<a href="javascript:alert(1)">x</a></body>', 1)
        self.assertTrue(any("javascript:" in e for e in vd.verify(bad, self.now)))

    def test_blank_link_without_noopener_fails(self):
        bad = self.html.replace(
            "</body>", '<a href="https://x.example" target="_blank">x</a></body>', 1)
        self.assertTrue(any("rel=noopener" in e for e in vd.verify(bad, self.now)))

    # ── 월 셀렉터 (negative control) ────────────────────────────────
    def test_missing_month_option_fails(self):
        bad = self.html.replace('<option value="8"', '<option value="99"', 1)
        self.assertTrue(any("1..12" in e for e in vd.verify(bad, self.now)))

    # ── manifest 변조 / raw key (negative control) ──────────────────
    def test_manifest_raw_key_injection_fails(self):
        raw, manifest = vd.extract_manifest(self.html)
        manifest["rows"] = [{"secret": "x"}]
        bad = self.html.replace(raw, json.dumps(manifest, ensure_ascii=False), 1)
        errors = vd.verify(bad, self.now)
        self.assertTrue(errors)
        self.assertTrue(any("unexpected keys" in e or "forbidden raw/credential" in e for e in errors))

    def test_manifest_schema_tamper_fails(self):
        raw, manifest = vd.extract_manifest(self.html)
        manifest["schema"] = "evil-v9"
        bad = self.html.replace(
            raw, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), 1)
        self.assertTrue(any("schema" in e for e in vd.verify(bad, self.now)))

    def test_manifest_non_current_source_status_fails(self):
        raw, manifest = vd.extract_manifest(self.html)
        manifest["source_status"]["live_quality"] = "unavailable"
        bad = self.html.replace(
            raw, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), 1)
        self.assertTrue(any("source_status" in e for e in vd.verify(bad, self.now)))

    def test_manifest_missing_source_timestamp_key_fails(self):
        raw, manifest = vd.extract_manifest(self.html)
        del manifest["source_snapshot_as_of"]["owned_media"]
        bad = self.html.replace(
            raw, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), 1)
        self.assertTrue(any("source_snapshot_as_of keys" in e for e in vd.verify(bad, self.now)))

    def test_manifest_default_month_out_of_range_fails(self):
        raw, manifest = vd.extract_manifest(self.html)
        manifest["default_month"] = 99
        bad = self.html.replace(
            raw, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), 1)
        self.assertTrue(any("default_month" in e for e in vd.verify(bad, self.now)))

    def test_manifest_payload_hash_format_fails(self):
        raw, manifest = vd.extract_manifest(self.html)
        manifest["source_payload_sha256"] = "not-a-sha"
        bad = self.html.replace(
            raw, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), 1)
        self.assertTrue(any("source_payload_sha256" in e for e in vd.verify(bad, self.now)))

    def test_manifest_naive_timestamp_fails(self):
        raw, manifest = vd.extract_manifest(self.html)
        manifest["built_at_kst"] = "2026-08-07T02:27:29"  # tz 제거
        bad = self.html.replace(
            raw, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), 1)
        self.assertTrue(any("timezone-aware" in e for e in vd.verify(bad, self.now)))

    def test_manifest_future_timestamp_beyond_clock_skew_fails(self):
        raw, manifest = vd.extract_manifest(self.html)
        manifest["built_at_kst"] = (self.now + dt.timedelta(hours=1)).isoformat()
        bad = self.html.replace(
            raw, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), 1)
        self.assertTrue(any("future" in e for e in vd.verify(bad, self.now, require_fresh=True)))

    def test_missing_manifest_fails(self):
        bad = vd.MANIFEST_RE.sub("", self.html, count=1)
        self.assertTrue(any("manifest" in e and "missing" in e for e in vd.verify(bad, self.now)))

    # ── freshness SLA (negative control) ────────────────────────────
    def test_stale_snapshot_only_fails_under_require_fresh(self):
        # require_fresh=False → 노후여도 통과(다른 위반 없음), True → stale RED
        self.assertEqual(vd.verify(self.html, self.stale_now, require_fresh=False), [])
        stale_errors = vd.verify(self.html, self.stale_now, require_fresh=True)
        self.assertTrue(any("stale snapshot" in e for e in stale_errors))


class SmokeViewportPolicyTest(unittest.TestCase):
    def _result(self, width):
        return {
            "doc": {"sw": width, "cw": width, "bsw": width, "iw": width},
            "errors": [],
        }

    def test_mobile_chrome_min_width_within_responsive_breakpoint_is_accepted(self):
        errors = sd._check_viewport(
            self._result(500), 390, 844, "mobile", switch_expected=None)
        self.assertEqual(errors, [])

    def test_mobile_width_outside_responsive_breakpoint_fails(self):
        errors = sd._check_viewport(
            self._result(1000), 390, 844, "mobile", switch_expected=None)
        self.assertTrue(any("responsive breakpoint" in e for e in errors))

    def test_desktop_css_viewport_must_match_requested_width(self):
        errors = sd._check_viewport(
            self._result(1200), 1440, 900, "desktop", switch_expected=None)
        self.assertTrue(any("requested width" in e for e in errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
