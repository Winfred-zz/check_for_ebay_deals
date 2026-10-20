"""Discord message rendering: escaping, link validation, length limits.

Approved contract (design.md + 0003 CR08/CR22):
- Discord's create-message content limit is 2,000 characters. The
  complete wrapper/price/URL budget is computed FIRST; a friendly name
  is trimmed (escaped-form safe boundaries, never splitting an escape)
  with a ".." marker. The listing URL is NEVER truncated, and a render
  that cannot fit the required content is REFUSED (ValueError) before
  anything is persisted, so oversized messages can never enter the
  durable pending queue.
- Friendly names are escaped in ONE pass (str.translate): one input
  control character produces exactly one backslash escape, so a
  backslash can never grow into a double escape, and trimming can
  never split an escape pair.
- Links are validated against parse.py's shared eBay listing rules
  (https, official item hosts, /itm/ path, ASCII, no raw Markdown
  delimiters). parse.py canonicalizes card links, so alert links are
  delimiter-free by construction; this layer is the send-side backstop.
- AllowedMentions.none() at the actual send boundary (deliver.py)
  remains the transport-level backstop.
- Oversized stored pending messages (from older versions) are
  recovered: recover_oversized() re-renders from the stored, validated
  content; a message that cannot be parsed back gets an operator-
  actionable refusal from the delivery loop and stays pending.
"""

import re

import parse
from selection import AlertRecord

MAX_MESSAGE_CHARS = 2000
# Fixed wrapper text: "[Found " + " for " + "](" + ")"
MESSAGE_FIXED_CHARS = len("[Found ") + len(" for ") + len("](") + len(")")
# The ellipsis marker is renderer text (not user-controlled), so it
# is appended raw: dots are not escaped, keeping the marker visible.
TRIM_MARKER = ".."
# Markdown control characters escaped in user-controlled text. Each
# appears exactly once: the single-pass translate maps each to
# backslash + char, so no input character is escaped twice.
MARKDOWN_SPECIALS = r"\`*_{}[]()#+-.!|>~\"'@&:%<>"
_ESCAPES = str.maketrans({char: "\\" + char for char in MARKDOWN_SPECIALS})
_UNESCAPE_RE = re.compile(r"\\(.)")
# Markdown link delimiters that must never appear raw in a URL.
_URL_DELIM_RE = re.compile(r"""[]()<>["'`\s]|[\x00-\x1f\x7f]""")

# Rendered shape of an alert line, for oversized-message recovery.
_ALERT_SHAPE_RE = re.compile(
    r"^\[Found (?P<name>.+) for \$(?P<price>[0-9,]+(?:\.[0-9]{1,2})?)"
    r"\]\((?P<url>https://[^()]+)\)$",
    re.DOTALL,
)


def is_safe_listing_url(url: str) -> bool:
    """Accept only canonical-safe https eBay item listing URLs."""
    if _URL_DELIM_RE.search(url):
        return False
    _item_id, reason = parse.extract_item_id(url)
    return reason is None


def item_is_valid(reason: str | None) -> bool:
    return reason is None


def canonical_listing_url(url: str) -> str | None:
    """Canonical https://host/itm/<id> form, or None when invalid."""
    return parse.canonical_listing_url(url)


def escape_markdown(text: str) -> str:
    """Escape every markdown control char in one pass (CR22)."""
    return text.translate(_ESCAPES)


def unescape_markdown(text: str) -> str:
    """Inverse of escape_markdown for stored-message recovery."""
    return _UNESCAPE_RE.sub(r"\1", text)


def render_alert_message(alert: AlertRecord) -> str:
    """Render one alert line: [Found <name> for $price](url).

    Raises ValueError on unsafe input — a rendering bug must be loud,
    never silently send a broken/misleading/oversized message. The
    finished message is re-validated against the 2,000-character cap.
    """
    url = alert["link"]
    canonical = canonical_listing_url(url)
    if canonical is None:
        raise ValueError(
            "listing URL rejected: not a canonical https ebay /itm/ link"
        )
    price = alert["price_usd"]
    if not price.is_finite() or price < 0:
        raise ValueError(f"listing price rejected for item {alert['id']}: {price}")
    # $x,xxx.xx display; keep the exact Decimal for comparisons elsewhere.
    price_text = f"${price:,.2f}"
    escaped_name = escape_markdown(str(alert["friendly_name"]).strip())
    overhead = MESSAGE_FIXED_CHARS + len(price_text) + len(canonical)
    marker = TRIM_MARKER  # renderer text: appended raw, never escaped
    name = escaped_name
    if overhead + len(name) > MAX_MESSAGE_CHARS:
        keep = MAX_MESSAGE_CHARS - overhead - len(marker)
        if keep < 1:
            raise ValueError(
                f"alert for item {alert['id']} cannot fit Discord's "
                + f"{MAX_MESSAGE_CHARS}-character budget with this listing "
                + "URL; refusing to truncate the URL"
            )
        # Never split an escape pair: back off over a trailing backslash.
        while keep > 0 and escaped_name[keep - 1] == "\\":
            keep -= 1
        name = escaped_name[:keep] + marker
    message = f"[Found {name} for {price_text}]({canonical})"
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(
            f"rendered alert exceeded {MAX_MESSAGE_CHARS} characters;"
            + " refusing to send"
        )
    return message


def recover_oversized(message: str) -> str | None:
    """Re-render a stored oversized alert, or None when unrecoverable.

    Recovery parses the stored "[Found <name> for $<price>](<url>)"
    shape back, unescapes the name, and re-renders it inside the
    budget (URL intact, name trimmed). A message that is not in the
    rendered shape is NOT guess-repaired: the caller reports an
    operator-actionable refusal and keeps the row pending.
    """
    match = _ALERT_SHAPE_RE.match(message)
    if not match:
        return None
    raw_name = unescape_markdown(match.group("name"))
    price_text = match.group("price")
    url = match.group("url")
    try:
        alert: AlertRecord = {
            "id": "recovered",
            "price_usd": parse.decimal_from_price_text(price_text),
            "link": url,
            "friendly_name": raw_name,
            "search_query": "stored pending alert",
        }
        return render_alert_message(alert)
    except ValueError:
        return None
