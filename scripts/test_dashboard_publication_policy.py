"""A visibly stale optional feed must not stop fresh mandatory-feed publication."""
import datetime as dt
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_dashboard as guard

ROOT = Path(__file__).resolve().parents[1]


class PublicationPolicyTest(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / 'index.html').read_text()
        _, manifest = guard.extract_manifest(self.html)
        self.now = dt.datetime.fromisoformat(manifest['built_at_kst']) + dt.timedelta(hours=1)
        # Always exercise degraded mode, even when the checked-in feed recovers.
        self.html = self.mutate(self.html, guard.OPTIONAL_STALE_SOURCES, 'stale', 72)

    def mutate(self, html, sources, status, age):
        raw, manifest = guard.extract_manifest(html)
        for source in sources:
            manifest['source_snapshot_as_of'][source] = (self.now - dt.timedelta(hours=age)).isoformat()
            if source in manifest['source_status']:
                manifest['source_status'][source] = status
            html = re.sub(r'<span[^>]*data-stale-source="' + source + r'"[^>]*>.*?</span>', '', html)
            if status == 'stale':
                html = html.replace('<div class="chips num">', '<div class="chips num">'
                                    f'<span class="chip warn" data-stale-source="{source}">원천 지연 · 마지막 확인</span>', 1)
        return html.replace(raw, json.dumps(manifest, ensure_ascii=False, separators=(',', ':')), 1)

    def release_errors(self, html):
        return guard.verify(html, self.now, require_fresh=True,
                            allow_stale_sources=guard.OPTIONAL_STALE_SOURCES)

    def test_disclosed_optional_staleness_passes_release_but_not_strict_default(self):
        self.assertEqual(self.release_errors(self.html), [])
        errors = guard.verify(self.html, self.now, require_fresh=True)
        for source in guard.OPTIONAL_STALE_SOURCES:
            self.assertTrue(any(f'source_status.{source}' in error for error in errors), errors)

    def test_each_optional_source_requires_exactly_one_disclosure(self):
        for source in guard.OPTIONAL_STALE_SOURCES:
            marker = f'data-stale-source="{source}"'
            for replacement in ('data-removed-source="' + source + '"', marker + ' ' + marker):
                with self.subTest(source=source, replacement=replacement):
                    errors = self.release_errors(self.html.replace(marker, replacement, 1))
                    self.assertTrue(any(f'source_status.{source}' in error for error in errors), errors)

    def test_mandatory_source_staleness_still_blocks_release(self):
        for source in ('revenue_mirror', 'live_quality', 'okr_targets'):
            with self.subTest(source=source):
                bad = self.mutate(self.html, {source}, 'stale', 72)
                errors = self.release_errors(bad)
                self.assertTrue(any(f'stale snapshot: source_snapshot_as_of.{source}' in e for e in errors), errors)
                if source != 'revenue_mirror':
                    self.assertTrue(any(f'source_status.{source}' in e for e in errors), errors)

    def test_optional_current_status_does_not_bypass_freshness(self):
        for source in guard.OPTIONAL_STALE_SOURCES:
            bad = self.mutate(self.html, {source}, 'current', 72)
            self.assertTrue(any(f'stale snapshot: source_snapshot_as_of.{source}' in e
                                for e in self.release_errors(bad)))

    def test_optional_unavailable_status_is_not_an_accepted_last_good(self):
        for source in guard.OPTIONAL_STALE_SOURCES:
            bad = self.mutate(self.html, {source}, 'unavailable', 72)
            self.assertTrue(any(f'source_status.{source}' in e for e in self.release_errors(bad)))

    def test_ci_and_daily_wrapper_use_same_bounded_publication_policy(self):
        self.assertEqual(guard.OPTIONAL_STALE_SOURCES, {'yt_quality', 'owned_media'})
        for path in ('.github/workflows/dashboard-guard.yml', 'ops/mbd_h2_pages_live_daily_refresh.sh'):
            text = (ROOT / path).read_text().replace('\\\n', ' ')
            commands = re.findall(r'(?:python|"\$PY") scripts/verify_dashboard.py index.html[^\n]*(?:\n\s+--allow-stale-source[^\n]*)?', text)
            self.assertTrue(commands, path)
            for command in commands:
                self.assertIn('--require-fresh', command)
                self.assertEqual(set(re.findall(r'--allow-stale-source (\w+)', command)), guard.OPTIONAL_STALE_SOURCES)


if __name__ == '__main__':
    unittest.main()
