"""Discord message rendering: escaping, link validation, length limits.

Approved contract (design.md): friendly names are escaped so
mention-like or markdown-like content cannot inject mentions/formatting;
only validated https eBay listing URLs are accepted; message bodies
stay within Discord's 4000-character limit without ever truncating the
listing URL into an unusable link.
"""

import re
import urllib.parse

from selection import AlertRecord

MAX_MESSAGE_CHARS = 4000
# Fixed wrapper text: "[Found " + " for " + "](" + ")"
MESSAGE_FIXED_CHARS = len("[Found ") + len(" for ") + len("](") + len(")")
TRIM_MARKER = ".."
MARKDOWN_SPECIALS = r"\`*_{}[]()#+-.!|>~\"'@&:%"
EBAY_ITEM_HOSTS_RE = re.compile(
    r"^www\.ebay\.(com|co\.uk|com\.au|de|ca|it|fr|es|nl)$",
    re.IGNORECASE,
)
EBAY_ITEM_PATH_RE = re.compile(r"^/itm/", re.IGNORECASE)


def is_safe_listing_url(url: str) -> bool:
    """Accept only https eBay listing URLs with a sane path."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    if not EBAY_ITEM_HOSTS_RE.match(parsed.netloc):
        return False
    return bool(EBAY_ITEM_PATH_RE.match(parsed.path))


def escape_markdown(text: str) -> str:
    """Escape every markdown control char in user-controlled text."""
    escaped = text
    for char in MARKDOWN_SPECIALS:
        escaped = escaped.replace(char, "\\" + char)
    return escaped


def render_alert_message(alert: AlertRecord) -> str:
    """Render one alert line: [Found <name> for $price](url).

    Raises ValueError on unsafe input — a rendering bug must be loud,
    never silently send a broken/misleading message.
    """
    url = alert["link"]
    if not is_safe_listing_url(url):
        raise ValueError("listing URL rejected: not a validated https ebay.itm link")
    price = alert["price_usd"]
    if not price.is_finite() or price < 0:
        raise ValueError(f"listing price rejected for item {alert['id']}: {price}")
    # $x,xxx.xx display; keep the exact Decimal for comparisons elsewhere.
    price_text = f"${price:,.2f}"
    name = escape_markdown(str(alert["friendly_name"]).strip())
    # Body budget: 4000 total. URL is never truncated; the name is
    # trimmed with an ellipsis marker instead.
    fixed = MESSAGE_FIXED_CHARS
    if fixed + len(price_text) + len(name) + len(url) > MAX_MESSAGE_CHARS:
        keep = (
            MAX_MESSAGE_CHARS
            - len(url)
            - fixed
            - len(price_text)
            - len(TRIM_MARKER)
        )
        name = name[: max(0, keep)] + TRIM_MARKER
    return f"[Found {name} for {price_text}]({url})"
