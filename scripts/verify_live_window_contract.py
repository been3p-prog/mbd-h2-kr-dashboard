#!/usr/bin/env python3
"""Verify the MBD Live-window numeric/data-basis contract.

This is a deterministic, stdlib-only release gate. The source-backed values live in
`data/live_window_contract.json`; refresh that contract from `[DB]구좌 RAW` before
changing the Live window copy or numbers.
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "data" / "live_window_contract.json"


def extract_live_window(html: str) -> str:
    match = re.search(r'(<section id="liveWindow"[\s\S]*?</section>)\s*<script>', html)
    if not match:
        raise ValueError("liveWindow section not found or not followed by script")
    return match.group(1)


def extract_card(live_html: str, card_id: str) -> str:
    pattern = re.compile(
        rf'(<article[^>]+data-live-broadcast-card="{re.escape(card_id)}"[^>]*>[\s\S]*?</article>)'
    )
    match = pattern.search(live_html)
    if not match:
        raise ValueError(f"broadcast card {card_id!r} not found")
    return match.group(1)


def compact_text(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html_lib.unescape(text)
    return " ".join(text.split())


def require(errors: list[str], haystack: str, needle: str, scope: str) -> None:
    if needle not in haystack:
        errors.append(f"MISSING[{scope}] {needle!r}")


def forbid(errors: list[str], haystack: str, needle: str, scope: str) -> None:
    if needle in haystack:
        errors.append(f"FORBIDDEN[{scope}] {needle!r}")


def check(html: str, contract: dict) -> list[str]:
    errors: list[str] = []
    live = extract_live_window(html)
    live_text = compact_text(live)

    for marker in contract.get("required_copy", []):
        require(errors, live, marker, "required_copy")

    for marker in contract.get("forbidden_in_live_window", []):
        forbid(errors, live, marker, "live_window")
        forbid(errors, live_text, marker, "live_window_text")

    cards = contract.get("broadcast_cards", {})
    actual_card_count = live.count('data-live-broadcast-card=')
    if actual_card_count != len(cards):
        errors.append(f"CARD_COUNT {actual_card_count} != contract {len(cards)}")

    for metric in contract.get("hero_kpis", []):
        label, value, em = metric["label"], metric["value"], metric["em"]
        require(errors, live, f"<small>{label}</small><b>{value}</b><em>{em}</em>", f"hero_kpi:{label}")

    for marker in contract.get("required_narrative", []):
        require(errors, live_text, marker, "narrative")

    for row in contract.get("package_lens", []):
        require(errors, live_text, row["label"], f"package:{row['label']}")
        require(errors, live_text, row["value"], f"package:{row['label']}")

    for card_id, spec in cards.items():
        try:
            card = extract_card(live, card_id)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        text = compact_text(card)
        require(errors, text, spec["brand"], f"card:{card_id}:brand")
        require(errors, text, spec["meta"], f"card:{card_id}:meta")
        for label, value in spec["metrics"].items():
            require(errors, card, f"<small>{label}</small><b>{value}</b>", f"card:{card_id}:metric:{label}")
        # Broadcast cards must show 1D amount, not ambiguous/broadcast GMV headline.
        if "<small>거래액</small><b>1D " not in card:
            errors.append(f"CARD_AMOUNT_NOT_1D[{card_id}]")
        for bad_label in ("방송GMV", "GMV", "방송별 데이터 GMV"):
            forbid(errors, card, f"<small>{bad_label}</small>", f"card:{card_id}")
        # Numeric notes may include 방송별 데이터 GMV for context, but the
        # visible card amount itself must remain the 1D 거래액 metric checked
        # above. Do not forbid raw broadcast-GMV numbers in the note body.

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("html_path", nargs="?", default=str(ROOT / "index.html"))
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    args = parser.parse_args(argv)

    html = Path(args.html_path).read_text(encoding="utf-8")
    contract = json.loads(Path(args.contract).read_text(encoding="utf-8"))
    errors = check(html, contract)
    if errors:
        print("LIVE_WINDOW_CONTRACT=FAIL")
        for error in errors:
            print(error)
        return 1
    print("LIVE_WINDOW_CONTRACT=GREEN")
    print(f"contract={contract['contract_id']} source_rows={contract['source']['sheet_rows']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
