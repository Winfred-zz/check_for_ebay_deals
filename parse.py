"""Pure HTML parsing for one eBay search page (no network, no logging).

parse_search_html() is a function under test with fixtures (step 3):
- Each card is parsed defensively; one invalid card never destroys its
  valid siblings or later cards (F04/F12).
- Optional fields (subtitle, seller info) reset per card — no leakage
  from a previous card.
- Prices are validated, not stripped-and-floated: exact USD amounts only
  (F13). Ranges ("$20 to $30"), other currencies, and malformed values
  are rejected with a reason, never turned into a zero price.
- Item IDs come from the /itm/<id> link path; malformed links and
  duplicate IDs are handled explicitly (F14).
- Excluded condition: the explicit "Parts Only" subtitle only; a
  missing subtitle does NOT exclude a listing (F11, approved decision).

Rejected cards return structured reasons (no credentials, no full-page
dumps); the caller logs them.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import TypedDict, cast

from bs4 import BeautifulSoup

CARD_SELECTOR = ".s-item__info"
PLACEHOLDER_TITLE = "Shop on eBay"
EXCLUDED_SUBTITLES = {"Parts Only"}
USD_PRICE_RE = re.compile(
    r"(?:US )?\$(?P<amount>[0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\Z"
)
OTHER_CURRENCY_RE = re.compile(r"[€£¥₹]|USD\b|\bbritish\b|\beuros?\b", re.IGNORECASE)
ID_RE = re.compile(r"[0-9A-Za-z_-]{1,64}\Z")


class ItemRecord(TypedDict):
    """One validated listing card."""
    id: str
    title: str
    subtitle: str | None
    price_usd: Decimal
    seller_name: str | None
    sales_count: str | None
    seller_rating: str | None
    link: str


class RejectionRecord(TypedDict):
    """Why a card was rejected and which card position it sat at."""
    reason: str
    position: int

def parse_usd_price(raw: str) -> tuple[Decimal | None, str | None]:
    """Return a Decimal for an exact USD amount, or None + reason."""
    text = raw.strip()
    if re.search(r"\bto\b", text, re.IGNORECASE):  # "$20 to $30" ranges
        return None, "range price"
    match = USD_PRICE_RE.match(text)
    if not match:
        if OTHER_CURRENCY_RE.search(text):
            return None, "unsupported currency"
        return None, "malformed price"
    try:
        return Decimal(match.group("amount").replace(",", "")), None
    except InvalidOperation:
        return None, "malformed price"


def extract_item_id(href: object) -> tuple[str | None, str | None]:
    """Extract the listing ID from an /itm/ URL; None + reason if invalid."""
    if not isinstance(href, str):
        return None, "link missing"
    path = href
    if path.startswith(("http://", "https://")):
        path = path.split("/", 2)[-1] if "://" in path else path
    parts = [segment for segment in path.split("/") if segment]
    try:
        index = next(i for i, segment in enumerate(parts) if segment == "itm")
    except StopIteration:
        return None, "not an /itm/ link"
    if index + 1 >= len(parts):
        return None, "missing /itm/ id"
    item_id = parts[index + 1].split("?")[0]
    if not ID_RE.match(item_id):
        return None, "malformed listing id"
    return item_id, None


def parse_search_html(html: str) -> tuple[list[ItemRecord], list[RejectionRecord]]:
    """Parse one search page.

    Returns (items, rejected). Each item: {id, title, subtitle, price_usd,
    seller_name, sales_count, seller_rating, link}. Each rejected:
    {reason, position}. A malformed card is rejected alone: later cards
    continue to parse (F04).
    """
    soup = BeautifulSoup(html, "lxml")
    items: list[ItemRecord] = []
    rejected: list[RejectionRecord] = []
    seen_ids: set[str] = set()
    for position, card in enumerate(soup.select(CARD_SELECTOR)):
        title_tag = card.select_one(".s-item__title span")
        if title_tag is None:
            rejected.append({"reason": "missing title", "position": position})
            continue
        title = title_tag.get_text(" ", strip=True)
        if title == PLACEHOLDER_TITLE:
            continue  # eBay's pinned placeholder card; not an error
        link_tag = card.select_one(".s-item__link")
        href = link_tag.get("href") if link_tag else None
        item_id, id_reason = extract_item_id(href)
        price_tag = card.select_one(".s-item__price")
        if price_tag is None:
            rejected.append({"reason": "missing price", "position": position})
            continue
        price_usd, price_reason = parse_usd_price(price_tag.get_text(" ", strip=True))
        if price_usd is None:
            rejected.append({"reason": cast("str", price_reason), "position": position})
            continue
        subtitle_tag = card.select_one(".s-item__subtitle span")
        subtitle = (
            subtitle_tag.get_text(" ", strip=True) if subtitle_tag else None
        )
        if subtitle in EXCLUDED_SUBTITLES:
            rejected.append({"reason": "excluded condition", "position": position})
            continue
        seller_tag = card.select_one(".s-item__seller-info-text")
        seller_name = sales_count = seller_rating = None
        if seller_tag:
            fields = seller_tag.get_text(" ", strip=True).split(" ")
            if len(fields) >= 3:
                seller_name, sales_count, seller_rating = fields[:3]
        if item_id is None:
            rejected.append({"reason": cast("str", id_reason), "position": position})
            continue
        if item_id in seen_ids:
            rejected.append({"reason": "duplicate id", "position": position})
            continue
        seen_ids.add(item_id)
        items.append(
            {
                "id": item_id,
                "title": title,
                "subtitle": subtitle,
                "price_usd": price_usd,
                "seller_name": seller_name,
                "sales_count": sales_count,
                "seller_rating": seller_rating,
                "link": cast("str", href),
            }
        )
    return items, rejected
