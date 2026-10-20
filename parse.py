"""Pure HTML/URL parsing for one eBay search page (no network, no logging).

Approved contract (0003 CR09/CR16/CR22):
- One BeautifulSoup pass per page: `parse_search_page()` returns the
  layout classification AND the card list, so layout classification
  and card extraction never parse the same HTML twice (CR22).
- Each card is parsed defensively; one invalid card never destroys its
  valid siblings or later cards (F04/F12). Invalid links are rejected
  as individual cards BEFORE cheapest selection, so a valid card
  remains eligible when the cheapest raw link was unsafe.
- Listing links are parsed with urllib.parse: https scheme only, the
  official eBay item hosts, a /itm/ path, and the numeric item ID =
  the last all-digit path segment (supports /itm/123456 and
  /itm/title-slug/123456). Query strings and fragments are tolerated;
  Markdown link delimiters in the URL are percent-encoded.
- Cards are canonicalized to https://<host>/itm/<id> (no query,
  fragment, port, or userinfo). The canonical URL's ID equals the
  dedup ID by construction.
- Duplicate item IDs keep the CHEAPEST card (ties: first-seen);
  higher-priced duplicates are rejected with a reason.
- Prices are validated, not stripped-and-floated: exact USD amounts
  only (F13). Ranges ("$20 to $30"), other currencies, and malformed
  values are rejected with a reason, never turned into a zero price.
- Excluded condition: the explicit "Parts Only" subtitle only; a
  missing subtitle does NOT exclude a listing (F11, approved decision).
- Layout classification uses unambiguous markers with explicit
  precedence: deny markers, then a numeric "<n> results" count
  (n>0 with no cards = suspicious, never clean empty), then known
  zero-result wording (CR16).

Rejected cards return structured reasons (no credentials, no full-page
dumps); the caller logs them.
"""

import re
import urllib.parse
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
NUMERIC_ID_RE = re.compile(r"[0-9]+\Z")
EBAY_ITEM_HOSTS_RE = re.compile(
    r"^www\.ebay\.(com|co\.uk|com\.au|de|ca|it|fr|es|nl)$",
    re.IGNORECASE,
)
# "10 results" style counts: CR16 substring bug — "0 results" inside
# "10 results" must never classify a positive count as a clean empty.
RESULT_COUNT_RE = re.compile(r"(?<![0-9])([0-9]+)\s+results", re.IGNORECASE)
DENY_MARKERS = ("access denied", "captcha")
ZERO_RESULT_MARKERS = (
    "did not match any items",
    "no exact matches",
    "0 results",
    "no results",
)

# Characters that must never survive raw in an alert URL: Markdown
# link delimiters, quotes/backticks, whitespace, and control chars.
_URL_UNSAFE_RE = re.compile(r"""[]()<>["'`\s]|[\x00-\x1f\x7f]""")


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


class ParsedPage(TypedDict):
    """Single-parse page outcome: layout plus validated cards."""
    layout: str  # "results" | "no-results" | "denied" | "suspicious"
    items: list[ItemRecord]
    rejected: list[RejectionRecord]
    result_count: int | None


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


def decimal_from_price_text(text: str) -> Decimal:
    """Decimal for a rendered display price ('$1,234.56' or '1,234.56').

    Used by oversized-message recovery to rebuild validated data.
    Raises ValueError on any non-exact form.
    """
    stripped = text.strip().lstrip("$")
    match = re.fullmatch(r"[0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?", stripped)
    if not match:
        raise ValueError(f"stored price text is not an exact USD amount: {text!r}")
    return Decimal(stripped.replace(",", ""))


def extract_item_id(href: object) -> tuple[str | None, str | None]:
    """Extract the numeric listing ID from an /itm/ URL; None + reason.

    Rules (CR09): https scheme, official eBay item host (no port, no
    userinfo), /itm/ path prefix, item ID = last all-digit path
    segment. Title-slug paths (/itm/title/123456) are accepted;
    /itm/123456?params and #fragments are accepted. Non-numeric,
    empty, and missing segments are rejected.
    """
    if not isinstance(href, str) or not href.strip():
        return None, "link missing"
    try:
        parsed = urllib.parse.urlparse(href)
    except ValueError:
        return None, "malformed link"
    if parsed.scheme != "https":
        return None, "link is not an https listing"
    netloc = parsed.netloc
    if "@" in netloc:
        return None, "link carries credentials"
    host = netloc.split(":")[0] if ":" in netloc else netloc
    if not EBAY_ITEM_HOSTS_RE.match(host):
        return None, "link host is not an official eBay item host"
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments or segments[0].lower() != "itm":
        return None, "not an /itm/ link"
    id_segment = segments[-1].lower()
    if id_segment == "itm":
        return None, "missing /itm/ id"
    if not NUMERIC_ID_RE.match(id_segment):
        return None, "missing numeric listing id in link"
    return id_segment, None


def canonical_listing_url(url: str) -> str | None:
    """Canonical https://<host>/itm/<id> for a valid listing URL, else None.

    The canonical form drops query/fragment/port/userinfo, lowercases
    the host, and percent-encodes Markdown-delimiter characters, so
    the stored alert link can never open a second Markdown link.
    The canonical URL's ID equals the dedup ID by construction.
    """
    item_id, _ = extract_item_id(url)
    if item_id is None:
        return None
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.split(":")[0].lower()
    canonical = f"https://{host}/itm/{item_id}"
    if _URL_UNSAFE_RE.search(canonical):  # defense: canonical is ASCII-safe
        return None
    return canonical


def classify_layout(soup: "BeautifulSoup") -> tuple[str, int | None]:
    """Classify a parsed page: layout label + parsed result count.

    Precedence (CR16): deny markers first; then cards present =
    "results"; then a numeric "<n> results" count: n>0 with no
    recognized cards is "suspicious" (a selector change never becomes
    clean empty success), n==0 needs known zero wording =
    "no-results"; known zero-result wording = "no-results"; anything
    else stays "suspicious" (failed, not a valid empty).
    """
    body_text = soup.get_text(" ", strip=True)
    lowered = body_text.lower()
    if any(marker in lowered for marker in DENY_MARKERS):
        return "denied", None
    cards_present = bool(soup.select(CARD_SELECTOR))
    counts_text = cast("list[str]", RESULT_COUNT_RE.findall(body_text))
    counts = [int(text) for text in counts_text]
    result_count = max(counts) if counts else None
    if cards_present:
        return "results", result_count
    if result_count is not None and result_count > 0:
        return "suspicious", result_count
    if any(marker in lowered for marker in ZERO_RESULT_MARKERS):
        return "no-results", result_count
    return "suspicious", result_count


def parse_search_page(html: str) -> ParsedPage:
    """One parse pass for layout classification and card extraction.

    Returns {layout, items, rejected, result_count}; layout rules see
    classify_layout. Cards are validated per the module docstring.
    """
    soup = BeautifulSoup(html, "lxml")
    layout, result_count = classify_layout(soup)
    items, rejected = _extract_cards(soup)
    return {
        "layout": layout,
        "items": items,
        "rejected": rejected,
        "result_count": result_count,
    }


def parse_search_html(html: str) -> tuple[list[ItemRecord], list[RejectionRecord]]:
    """Card extraction only (kept for pure-parser tests)."""
    soup = BeautifulSoup(html, "lxml")
    return _extract_cards(soup)


def _extract_cards(
    soup: "BeautifulSoup",
) -> tuple[list[ItemRecord], list[RejectionRecord]]:
    """Validate every card; reject malformed ones individually (F04).

    Duplicate IDs keep the cheapest card; ties keep the first-seen
    card, and the loser is rejected with reason "duplicate id".
    """
    items: list[ItemRecord] = []
    rejected: list[RejectionRecord] = []
    # id -> (index into items, price) for the cheapest-wins dedup.
    by_id: dict[str, int] = {}
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
        if item_id is None:
            rejected.append({"reason": cast("str", id_reason), "position": position})
            continue
        seller_tag = card.select_one(".s-item__seller-info-text")
        seller_name = sales_count = seller_rating = None
        if seller_tag:
            fields = seller_tag.get_text(" ", strip=True).split(" ")
            if len(fields) >= 3:
                seller_name, sales_count, seller_rating = fields[:3]
        canonical = canonical_listing_url(cast("str", href))
        assert canonical is not None  # extract_item_id accepted this link
        record: ItemRecord = {
            "id": item_id,
            "title": title,
            "subtitle": subtitle,
            "price_usd": price_usd,
            "seller_name": seller_name,
            "sales_count": sales_count,
            "seller_rating": seller_rating,
            "link": canonical,
        }
        if item_id in by_id:
            kept_index = by_id[item_id]
            kept = items[kept_index]
            if price_usd < kept["price_usd"]:
                # Cheaper duplicate replaces the kept card (deterministic).
                rejected.append(
                    {"reason": "duplicate id superseded", "position": kept_index}
                )
                items[kept_index] = record
            else:
                rejected.append({"reason": "duplicate id", "position": position})
            continue
        by_id[item_id] = len(items)
        items.append(record)
    return items, rejected
