import contextlib
import datetime as dt
import io
import unittest
from unittest import mock

import export_bot_metrics
import publish_bot_metrics as publish


MANIFEST = '<script type="application/json" id="mbd-public-guard">{"default_month":10}</script>'
SEPTEMBER = '<div class="mvk mv" data-m="9"><div data-current-as-of="2026-09-30"></div></div>'
OCTOBER = '<div class="mvk mv" data-m="10"><div class="kpis"><div data-current-as-of="2026-10-01"></div></div></div>'
ROLLOVER = (MANIFEST + SEPTEMBER + OCTOBER).encode()


class PublishCutoffTests(unittest.TestCase):
    def test_rollover_selects_default_month_not_first_clock(self):
        self.assertEqual(publish.dashboard_cutoff(ROLLOVER), dt.date(2026, 10, 1))

    def test_class_tokens_attribute_order_and_single_quotes(self):
        surface = "<div data-m='10' class='mv mvk selected'><span data-current-as-of='2026-10-01'></span></div>"
        self.assertEqual(publish.dashboard_cutoff((MANIFEST + surface).encode()), dt.date(2026, 10, 1))

    def test_other_surfaces_and_comments_do_not_supply_cutoff(self):
        html = MANIFEST + SEPTEMBER + '<!-- ' + OCTOBER + ' -->' + '<div class="mvk mv" data-m="10"></div>'
        with self.assertRaises(ValueError):
            publish.dashboard_cutoff(html.encode())

    def test_invalid_manifests_fail_closed(self):
        bad = [
            '', MANIFEST + MANIFEST,
            MANIFEST.replace('{"default_month":10}', '{'),
            MANIFEST.replace('{"default_month":10}', '[]'),
            MANIFEST.replace('{"default_month":10}', '{}'),
            MANIFEST.replace('{"default_month":10}', '{"default_month":9,"default_month":10}'),
            MANIFEST.replace('{"default_month":10}', '{"default_month":10,"other":NaN}'),
            MANIFEST.replace('application/json', 'text/plain'),
            MANIFEST.replace('id="mbd-public-guard"', 'id="mbd-public-guard" id="other"'),
            MANIFEST.replace('</script>', ''),
        ]
        for value in ('0', '13', 'true', 'null', '10.0', '"10"'):
            bad.append(MANIFEST.replace(':10', ':' + value))
        for manifest in bad:
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                publish.dashboard_cutoff((manifest + SEPTEMBER + OCTOBER).encode())

    def test_invalid_surfaces_fail_closed(self):
        bad = [
            '', OCTOBER + OCTOBER,
            OCTOBER.replace('data-m="10"', ''),
            OCTOBER.replace('data-m="10"', 'data-m="10" data-m="9"'),
            OCTOBER.replace('data-m="10"', 'data-m="10x"'),
            OCTOBER.replace('class="mvk mv"', 'class="mvk mv" class="other"'),
            OCTOBER.replace('data-current-as-of="2026-10-01"', ''),
            OCTOBER.replace('data-current-as-of="2026-10-01"', 'data-current-as-of="2026-10-01" data-current-as-of="2026-10-02"'),
            OCTOBER.replace('</div>', '<span data-current-as-of="2026-10-01"></span></div>', 1),
            OCTOBER[:-6],
            '<div class="mvk mv" data-m="10">' + OCTOBER + '</div>',
            OCTOBER.replace('class="mvk mv"', 'class="not-mvk mv"'),
        ]
        for surface in bad:
            with self.subTest(surface=surface), self.assertRaises(ValueError):
                publish.dashboard_cutoff((MANIFEST + SEPTEMBER + surface).encode())

    def test_invalid_cutoffs_fail_closed(self):
        for value in ('', '2026-09-30', '2026-10-32', '2026-10-1', '20261001', '2026-10-01T00:00:00', 'not-a-date'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                publish.dashboard_cutoff((MANIFEST + OCTOBER.replace('2026-10-01', value)).encode())
        with self.assertRaises(ValueError):
            publish.dashboard_cutoff((MANIFEST + OCTOBER.replace('="2026-10-01"', '')).encode())

    def run_main(self, html, public=None):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch('sys.argv', ['publish', '--mbd', 'unused-mbd', '--youtube', 'unused-yt', '--check-only']))
        stack.enter_context(mock.patch.object(publish.Path, 'read_bytes', return_value=html))
        network = stack.enter_context(mock.patch.object(publish.urllib.request, 'urlopen'))
        network.return_value.__enter__.return_value.read.return_value = html if public is None else public
        export = stack.enter_context(mock.patch.object(publish, 'export', return_value={}))
        ssh = stack.enter_context(mock.patch.object(publish.subprocess, 'run', side_effect=AssertionError('no external writes')))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        return export, ssh

    def test_main_passes_october_cutoff_to_export(self):
        export, ssh = self.run_main(ROLLOVER)
        publish.main()
        export.assert_called_once_with('unused-mbd', 'unused-yt', 'index.html', dt.date(2026, 10, 1))
        ssh.assert_not_called()

    def test_main_invalid_selection_never_exports_or_writes(self):
        export, ssh = self.run_main((MANIFEST + SEPTEMBER).encode())
        with self.assertRaises(ValueError):
            publish.main()
        export.assert_not_called()
        ssh.assert_not_called()

    def test_public_byte_guard_remains(self):
        export, ssh = self.run_main(ROLLOVER, public=b'other release')
        with self.assertRaisesRegex(RuntimeError, 'public HTML'):
            publish.main()
        export.assert_not_called()
        ssh.assert_not_called()

    def test_export_still_rejects_september_cutoff_for_october(self):
        with mock.patch.object(export_bot_metrics.Path, 'read_bytes', return_value=ROLLOVER), \
                mock.patch.object(export_bot_metrics.duckdb, 'connect') as connect:
            with self.assertRaisesRegex(ValueError, 'dashboard month differs from cache cutoff'):
                export_bot_metrics.export('unused-mbd', 'unused-yt', 'unused-html', dt.date(2026, 9, 30))
            connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
