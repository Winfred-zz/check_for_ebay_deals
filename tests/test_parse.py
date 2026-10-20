"""Offline parse tests against tests/fixtures/offline-scan HTML fixtures.

Asserts the F04/F12 contract: a malformed card is rejected alone and
never destroys its valid siblings or later cards; optional fields
(seller info) never inherit from the previous card; the pinned
"Shop on eBay" placeholder is skipped silently (not an error);
parse_usd_price accepts only exact USD amounts — ranges, other
currencies, and malformed text are rejected with a reason, never
coerced to a number. Duplicate item IDs keep the CHEAPEST card
(CR09): a cheaper duplicate supersedes the kept card and is logged
with the kept card's list index (not its DOM position).
"""

import pytest

from parse import (
    canonical_listing_url,
    decimal_from_price_text,
    extract_item_id,
    parse_search_html,
    parse_search_page,
    parse_usd_price,
)


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
         ["missing price", "not an /itm/ link", "duplicate id superseded"]),
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
    [("broken", [0, 2, 0]), ("condition", [2, 3])],
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


@pytest.mark.parametrize(
    ("url", "item_id", "reason"),
    [
        ("https://www.ebay.com/itm/123456", "123456", None),
        ("https://www.ebay.com/itm/nice-title-slug/123456", "123456", None),
        ("https://www.ebay.com/itm/123456?mkc=1&hash=x", "123456", None),
        ("https://www.ebay.com/itm/123456#footer", "123456", None),
        ("https://www.ebay.co.uk/itm/123456", "123456", None),
        (
            "https://www.ebay.com/itm/abc123",
            None,
            "missing numeric listing id in link",
        ),
        ("https://www.ebay.com/itm/", None, "missing /itm/ id"),
        ("https://www.ebay.com/itm", None, "missing /itm/ id"),
        ("https://www.ebay.com/str/123456", None, "not an /itm/ link"),
        ("http://www.ebay.com/itm/123456", None, "link is not an https listing"),
        (
            "https://evil.com/itm/123456",
            None,
            "link host is not an official eBay item host",
        ),
        (
            "https://www.ebay.com.itm/123456",
            None,
            "link host is not an official eBay item host",
        ),
        (
            "https://www.ebay.com@evil.invalid/itm/123456",
            None,
            "link carries credentials",
        ),
        ("", None, "link missing"),
        (None, None, "link missing"),
    ],
)
def test_item_id_extraction_rules(
    url: "str | None", item_id: "str | None", reason: "str | None"
) -> None:
    assert extract_item_id(url) == (item_id, reason)


def test_canonical_urls_drop_query_fragment_port_case():
    cases = {
        "https://www.ebay.com/itm/123456?mkc=1#frag": "https://www.ebay.com/itm/123456",
        "https://WWW.EBAY.COM:443/itm/title/123456": "https://www.ebay.com/itm/123456",
    }
    for raw, canonical in cases.items():
        assert canonical_listing_url(raw) == canonical


def test_canonical_rejects_invalid_links():
    for raw in ("http://www.ebay.com/itm/1", "https://evil.com/itm/1"):
        assert canonical_listing_url(raw) is None


def test_unsafe_cheapest_card_rejected_next_cheapest_survives():
    """A valid card stays eligible when the cheapest raw link is unsafe."""
    cards = """
    <div class="s-item__info">
      <div class="s-item__title"><span>safe cheap</span></div>
      <a class="s-item__link" href="https://evil.com/itm/1"><span>l</span></a>
      <span class="s-item__price">$1.00</span>
    </div>
    <div class="s-item__info">
      <div class="s-item__title"><span>valid pricier</span></div>
      <a class="s-item__link" href="https://www.ebay.com/itm/222333444555"><span>l</span></a>
      <span class="s-item__price">$9.00</span>
    </div>
    """
    items, rejected = parse_search_html(cards)
    assert [item["id"] for item in items] == ["222333444555"]
    assert [entry["reason"] for entry in rejected] == [
        "link host is not an official eBay item host"
    ]


def test_card_links_are_canonical():
    items, _ = parse_search_html(fixture_text("grigri"))
    for item in items:
        assert item["link"] == f"https://www.ebay.com/itm/{item['id']}"


def test_cheapest_duplicate_supersedes_costlier_kept_card():
    cards = """
    <div class="s-item__info">
      <div class="s-item__title"><span>costly</span></div>
      <a class="s-item__link" href="https://www.ebay.com/itm/777000111222"><span>l</span></a>
      <span class="s-item__price">$12.34</span>
    </div>
    <div class="s-item__info">
      <div class="s-item__title"><span>cheap</span></div>
      <a class="s-item__link" href="https://www.ebay.com/itm/777000111222?mkc=1"><span>l</span></a>
      <span class="s-item__price">$5.00</span>
    </div>
    """
    items, rejected = parse_search_html(cards)
    assert len(items) == 1
    assert str(items[0]["price_usd"]) == "5.00"
    assert items[0]["title"] == "cheap"
    assert items[0]["link"] == "https://www.ebay.com/itm/777000111222"
    assert rejected[0]["reason"] == "duplicate id superseded"


@pytest.mark.parametrize(
    ("text", "price"),
    [("$1,234.56", "1234.56"), ("1,234.56", "1234.56"), ("$5.00", "5.00")],
)
def test_decimal_from_price_text(text: str, price: str) -> None:
    assert str(decimal_from_price_text(text)) == price


@pytest.mark.parametrize("text", ["FREE", "$1..00", "12.345", "abc"])
def test_decimal_from_price_text_refuses_garbage(text: str) -> None:
    with pytest.raises(ValueError, match="not an exact USD amount"):
        decimal_from_price_text(text)


@pytest.mark.parametrize(
    ("html", "layout", "count"),
    [
        ("<h1>10 results</h1>", "suspicious", 10),
        ("<div>0 results</div>", "no-results", 0),
        ("<div>Your search did not match any items</div>", "no-results", None),
        ("<div>Access Denied</div>", "denied", None),
        ("<h1>10 results</h1><div class='s-item__info'><span>x</span></div>",
         "results", 10),
        ("<div>challenge <div class='s-item__info'>"
         "<span>Access Denied captcha</span></div></div>", "denied", None),
        ("<div>mystery page</div>", "suspicious", None),
    ],
)
def test_layout_classification(html: str, layout: str, count: "int | None") -> None:
    page = parse_search_page(html)
    assert page["layout"] == layout
    assert page["result_count"] == count


def test_single_parse_returns_cards_and_layout_together():
    page = parse_search_page(fixture_text("grigri"))
    assert page["layout"] == "results"
    assert [item["id"] for item in page["items"]] == [
        "167111111111",
        "167222222222",
    ]
    assert [entry["reason"] for entry in page["rejected"]] == [
        "excluded condition",
        "duplicate id",
    ]
