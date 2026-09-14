import datetime as dt
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import refresh_live_window_from_duckdb as refresh  # noqa: E402
import verify_live_window_contract as verify  # noqa: E402


class LiveRetrospectiveOverlayTest(unittest.TestCase):
    def test_completed_broadcast_without_review_is_valid_pending_state(self):
        row = {
            "date": dt.date(2026, 9, 1),
            "brand": "신규 편성",
            "package": "스마트",
            "package_key": "스마트",
            "pgm": "일반",
            "pd": "Minnie",
            "viewers": 100,
            "clicks": 10,
            "buyers": 2,
            "gmv_1d": 1_000_000,
            "gmv_1h": 500_000,
            "broadcast_gmv": 800_000,
            "af": 100_000,
            "cost": 50_000,
            "margin": 50_000,
            "official_review": "",
            "review_sent": False,
            "competitor_live_history": "",
        }

        html, contract = refresh.render_section(
            [row], dt.datetime(2026, 9, 14, tzinfo=refresh.KST), "2026-09-14 09:00:00"
        )

        self.assertIn('data-live-retro="pending"', html)
        self.assertIn('data-live-retro="pending"', contract["required_copy"])
        self.assertNotIn('data-live-retro="available"', contract["required_copy"])

    def test_fetch_maps_public_review_fields_and_omits_internal_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "review.duckdb"
            con = duckdb.connect(str(db_path))
            con.execute("create schema live")
            con.execute("create schema meta")
            con.execute('''
                create table live.raw_slots(
                    "온에어 일자" varchar, "브랜드명" varchar, "패키지" varchar,
                    "PGM" varchar, "PD" varchar,
                    "라이브 시청자 (비로그인 포함)" varchar, "상품 클릭수" varchar,
                    "라이브 구매자수" varchar,
                    "일 전체 GMV (라이브 브랜드 전체)" varchar,
                    "라이브 1H GMV" varchar, "방송별 데이터 GMV" varchar,
                    "AF수취액" varchar, "비용" varchar, "마진액" varchar,
                    "내부회고" varchar, "공식 회고" varchar, "회고 발송" varchar,
                    "타사 라이브 이력" varchar
                )
            ''')
            con.execute('''
                insert into live.raw_slots values (
                    '2026-09-10','테스트 브랜드','스마트','일반','minnie',
                    '100','10','2','1000000','500000','800000',
                    '100000','50000','50000','비공개 메모','공식 회고 본문  ',
                    'TRUE','경쟁사 1회  '
                )
            ''')
            con.execute('''
                create table meta.ingest_log(last_ingest_at timestamp, status varchar)
            ''')
            con.execute("insert into meta.ingest_log values ('2026-09-14 09:00:00','ok')")
            con.close()

            rows, ingest = refresh.fetch_rows(db_path, 2026, 9)

        self.assertEqual(ingest, "2026-09-14 09:00:00")
        self.assertEqual(rows[0]["official_review"], "공식 회고 본문")
        self.assertTrue(rows[0]["review_sent"])
        self.assertEqual(rows[0]["competitor_live_history"], "경쟁사 1회")
        self.assertNotIn("internal_review", rows[0])
        self.assertNotIn("비공개 메모", repr(rows[0]))

    def test_card_escapes_review_and_never_renders_internal_field(self):
        review = '<script>alert("x")</script>\n두 번째 줄'
        competitor = "A&B <경쟁사>"
        row = {
            "date": dt.date(2026, 9, 10),
            "brand": "테스트 브랜드",
            "package_key": "스마트",
            "pgm": "일반",
            "viewers": 100,
            "clicks": 10,
            "buyers": 2,
            "gmv_1d": 1_000_000,
            "gmv_1h": 500_000,
            "broadcast_gmv": 800_000,
            "official_review": review,
            "review_sent": True,
            "competitor_live_history": competitor,
            "internal_review": "절대 공개 금지",
        }

        html, contract = refresh.render_card(row, 1, {})

        self.assertIn('data-live-retro="available"', html)
        self.assertIn('class="live-retro-trigger"', html)
        self.assertIn('&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;', html)
        self.assertIn('A&amp;B &lt;경쟁사&gt;', html)
        self.assertNotIn("절대 공개 금지", html)
        self.assertNotIn("내부회고", html)
        self.assertEqual(
            contract["retrospective"]["official_sha256"],
            hashlib.sha256(review.encode("utf-8")).hexdigest(),
        )

    def test_contract_verifier_detects_rendered_review_mutation(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "index.html").read_text(encoding="utf-8")
        contract = json.loads(
            (root / "data" / "live_window_contract.json").read_text(encoding="utf-8")
        )
        self.assertEqual(verify.check(html, contract), [])
        mutated = html.replace(
            'data-live-retro-field="official">',
            'data-live-retro-field="official">변조',
            1,
        )
        self.assertTrue(
            any("RETROSPECTIVE_HASH_MISMATCH" in error for error in verify.check(mutated, contract))
        )
        sent_mutation = html.replace(
            '<span>편성별 회고</span><small>발송 완료</small></button>',
            '<span>편성별 회고</span><small>발송 대기</small></button>',
            1,
        )
        self.assertTrue(
            any("RETROSPECTIVE_SENT_MISMATCH" in error for error in verify.check(sent_mutation, contract))
        )


if __name__ == "__main__":
    unittest.main()
