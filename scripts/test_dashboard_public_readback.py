"""Exercise the actual cron readback against current rendered dashboard bytes."""
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class PublicReadbackTest(unittest.TestCase):
    def probe(self, public):
        wrapper = (ROOT / 'ops/mbd_h2_pages_live_daily_refresh.sh').read_text()
        code = wrapper.split('public_readback_once() {\n"$PY" - <<\'PY\'\n', 1)[1].split('\nPY\n}', 1)[0]
        with patch('urllib.request.urlopen', return_value=io.BytesIO(public.encode())), contextlib.redirect_stdout(io.StringIO()):
            exec(compile(code, 'cron-public-readback', 'exec'), {'__name__': '__readback_test__'})

    def test_current_rendered_contract_passes(self):
        self.probe((ROOT / 'index.html').read_text())

    def test_stale_public_bytes_fail(self):
        with self.assertRaises(SystemExit) as error:
            self.probe((ROOT / 'index.html').read_text() + '\n<!-- stale -->')
        self.assertEqual(error.exception.code, 1)


if __name__ == '__main__':
    unittest.main()
