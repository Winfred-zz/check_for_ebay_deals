"""Offline parse tests against tests/fixtures/offline-scan HTML fixtures.

Asserts the F04/F12 contract: a malformed card is rejected alone and
never destroys its valid siblings or later cards; optional fields
(seller info) never inherit from the previous card; the pinned
"Shop on eBay" placeholder is skipped silently (not an error); and
parse_usd_price accepts only exact USD amounts — ranges, other
currencies, and malformed text are rejected with a reason, never
coerced to a number.
"""

import pytest

from parse import parse_search_html, parse_usd_price


def fixture_text(name: str) -> str:
    with open(f"tests/fixtures/offline-scan/{name}.html",
              encoding="utf-8") as handle:
        return handle.read()


@pytest.mark.parametrize(
    ("name", "ids", "reasons"),
    [
        ("grigri", ["167111111111", "167222222222"],
         ["excluded condition", "duplicate id"]),
        ("broken", ["111000222000"],
         ["missing price", "not an /itm/ link", "duplicate id"]),
        ("condition", ["222000111000", "222000222000"],
         ["range price", "unsupported currency"]),
    ],
)
def test_malformed_cards_rejected_alone(
    name: str, ids: list[str], reasons: list[str]
) -> None:
    items, rejected = parse_search_html(fixture_text(name))
    assert [item["id"] for item in items] == ids
    assert [entry["reason"] for entry in rejected] == reasons


@pytest.mark.parametrize(
    ("name", "positions"),
    [("broken", [0, 2, 3]), ("condition", [2, 3])],
)
def test_valid_siblings_survive(name: str, positions: list[int]) -> None:
    _, rejected = parse_search_html(fixture_text(name))
    assert [entry["position"] for entry in rejected] == positions


def test_seller_info_never_inherits_from_previous_card() -> None:
    items, _ = parse_search_html(fixture_text("condition"))
    assert items[0]["seller_name"] == "oldseller"
    assert items[1]["seller_name"] is None
    assert items[1]["sales_count"] is None
    assert items[1]["seller_rating"] is None


def test_placeholder_card_skipped_without_rejection() -> None:
    items, rejected = parse_search_html(fixture_text("empty"))
    assert items == []
    assert [entry["reason"] for entry in rejected] == ["missing title"]


@pytest.mark.parametrize(
    ("raw", "price", "reason"),
    [
        ("US $19.50", "19.50", None),
        ("$1,234.56", "1234.56", None),
        ("$20.00 to $30.00", None, "range price"),
        ("\u20ac20.00", None, "unsupported currency"),
        ("FREE", None, "malformed price"),
    ],
)
def test_price_exactness_not_stripped_float(
    raw: str, price: str | None, reason: str | None
) -> None:
    parsed, parsed_reason = parse_usd_price(raw)
    if price is None:
        assert parsed is None
    else:
        assert str(parsed) == price
    assert parsed_reason == reason
