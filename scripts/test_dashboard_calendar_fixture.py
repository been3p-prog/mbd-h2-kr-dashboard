# [2026-11-01] 합성 월 전환은 주변 레이아웃을 복제하지 않는다 — 닫힘·월 노드·전역 스크립트를 검증한다.
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dashboard_calendar_fixture as fixture


class CalendarStructure(HTMLParser):
    def __init__(self, source: str):
        super().__init__()
        self.depth = 0
        self.minimum_depth = 0
        self.wrappers = Counter()
        self.surfaces = Counter()
        self.scripts = 0
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script":
            self.scripts += 1
        if tag != "div":
            return
        self.depth += 1
        classes = attributes.get("class", "")
        if classes in {"grid2", "gauge12", "sidecol", "foot"}:
            self.wrappers[classes] += 1
        if classes in {"mvk mv", "mvs mv", "mvr mv"}:
            self.surfaces[(classes, attributes.get("data-m"))] += 1
        if classes.startswith("g ") and "data-m" in attributes:
            self.surfaces[("g", attributes["data-m"])] += 1

    def handle_endtag(self, tag):
        if tag == "div":
            self.depth -= 1
            self.minimum_depth = min(self.minimum_depth, self.depth)


class CalendarFixtureTest(unittest.TestCase):
    def test_synthetic_month_preserves_complete_calendar_structure(self):
        source = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        original = CalendarStructure(source)
        self.assertEqual(original.depth, 0)
        for month in (8, 9, 10, 11, 12):
            with self.subTest(month=month):
                candidate = fixture.synthetic_month_state(source, month)
                parsed = CalendarStructure(candidate)
                self.assertEqual(parsed.depth, 0)
                self.assertEqual(parsed.minimum_depth, 0)
                self.assertEqual(parsed.wrappers, original.wrappers)
                self.assertEqual(parsed.surfaces, original.surfaces)
                self.assertEqual(parsed.scripts, original.scripts)
                self.assertEqual(re.findall(r"\bvar CUR = (\d+);", candidate), [str(month)])


if __name__ == "__main__":
    unittest.main()
