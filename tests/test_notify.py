"""Alert rendering: escaping, URL rules, price guards, 2000-char budget.

Consumer-visible invariants only (0003 CR08/CR20/CR22): what the
Discord transport and the recipient see — rendered length, intact URL,
escape integrity, refusals — not internal formulas.
"""

from decimal import Decimal

import re

import pytest

import notify
from selection import AlertRecord

BASE_URL = "https://www.ebay.com/itm/167111111111"


def _alert(name: str, price: str, url: str = BASE_URL) -> AlertRecord:
    record: AlertRecord = {
        "id": "167111111111",
        "friendly_name": name,
        "price_usd": Decimal(price),
        "link": url,
        "search_query": "q",
    }
    return record


def test_ordinary_name_renders_friendly_line():
    message = notify.render_alert_message(_alert("Gundam GM Cannon", "24.74"))
    assert message == f"[Found Gundam GM Cannon for $24.74]({BASE_URL})"


def test_price_gets_thousands_separator():
    rendered = notify.render_alert_message(_alert("thing", "1234.56"))
    assert "for $1,234.56]" in rendered


def test_markdown_control_chars_escaped_exactly_once():
    raw = r"a\b*c*[d]{e}(f)#g-h!i|j>k~l\"m'n@o&p:q%r"
    escaped = notify.escape_markdown(raw)
    # Single pass (CR22): each control char gains exactly one escape.
    specials = sum(1 for char in raw if char in notify.MARKDOWN_SPECIALS)
    assert len(escaped) == len(raw) + specials
    assert notify.unescape_markdown(escaped) == raw
    for char in notify.MARKDOWN_SPECIALS:
        if char == "\\":
            continue
        assert escaped.count(char) == raw.count(char)
    # No lone escape left in the kept prefix: pairs are complete.
    pairs = re.findall(r"\\.", escaped)
    assert len(pairs) == specials


def test_mention_like_name_cannot_ping_or_link():
    raw_name = "@admin **[buy now](https://evil.example)** #h <tag>"
    rendered = notify.render_alert_message(_alert(raw_name, "1.00"))
    assert "\\@admin" in rendered
    for token in ("**", "](https://evil.example)", "<tag>"):
        assert token not in rendered
    assert rendered.count("](") == 1
    assert rendered.endswith(f"]({BASE_URL})")


def test_link_like_name_cannot_add_extra_hyperlink():
    url = "https://www.ebay.com/itm/9"
    rendered = notify.render_alert_message(
        _alert("[click](https://evil.example/x)", "5", url)
    )
    assert rendered.count("](") == 1
    assert "](" + url + ")" in rendered
    assert "evil" in rendered  # visible as escaped text, not a link


def test_url_validation_rejects_lookalikes():
    bad_urls = [
        "http://www.ebay.com/itm/1",
        "https://ebay.com/itm/1",
        "https://www.ebay.com.itm/1",
        "https://evil.com/itm/123456",
        "https://www.ebay.com/str/123",
        "https://www.ebay.com/itm/",
        "https://www.ebay.com/itm/abc123",
        "file:///etc/passwd",
        "not a url",
        "https://www.ebay.com@evil.invalid/itm/123456",
    ]
    for url in bad_urls:
        with pytest.raises(ValueError, match="listing URL rejected"):
            _ = notify.render_alert_message(_alert("x", "1", url))


def test_accepted_hosts_render_intact():
    for host in ("com", "co.uk", "com.au", "de", "ca", "it", "fr", "es", "nl"):
        url = f"https://www.ebay.{host}/itm/123456"
        message = notify.render_alert_message(_alert("x", "1", url))
        assert message.endswith(f"({url})")


def test_canonicalization_drops_query_fragment_and_title():
    cases = {
        "https://www.ebay.com/itm/167111111111?mkc=1&hash=2": BASE_URL,
        "https://www.ebay.com/itm/167111111111#footer": BASE_URL,
        "https://www.ebay.com/itm/nice-title-slug/167111111111": BASE_URL,
        "https://WWW.EBAY.COM/itm/167111111111": BASE_URL,
    }
    for raw, canonical in cases.items():
        message = notify.render_alert_message(_alert("x", "1", raw))
        assert message.endswith(f"]({canonical})")
        assert "?" not in message and "#" not in message


def test_markdown_delimiters_in_url_cannot_open_second_link():
    evil = "https://www.ebay.com/itm/123456?x=)[other](https://evil.invalid)"
    rendered = notify.render_alert_message(_alert("x", "1", evil))
    assert rendered.count("](") == 1
    assert "evil.invalid" not in rendered
    assert rendered.endswith(f"]({BASE_URL.replace('167111111111', '123456')})")


def test_nan_and_negative_prices_rejected():
    for price in (Decimal("NaN"), Decimal("-5")):
        alert: AlertRecord = {
            "id": "it-42",
            "friendly_name": "x",
            "price_usd": price,
            "link": BASE_URL,
            "search_query": "q",
        }
        with pytest.raises(ValueError, match="price rejected for item"):
            _ = notify.render_alert_message(alert)


def test_budget_is_discord_transport_limit():
    assert notify.MAX_MESSAGE_CHARS == 2000


def test_oversized_name_trims_never_the_url():
    url = "https://www.ebay.com/itm/" + "1" * 256
    alert = _alert("A" * 5000, "9.99", url)
    message = notify.render_alert_message(alert)
    assert len(message) <= notify.MAX_MESSAGE_CHARS
    assert message.endswith(f"({url})")
    assert notify.TRIM_MARKER in message
    assert "A" * 5000 not in message


def test_url_consuming_the_budget_is_refused_not_truncated():
    url = "https://www.ebay.com/itm/" + "9" * (notify.MAX_MESSAGE_CHARS - 40)
    with pytest.raises(ValueError, match="cannot fit"):
        _ = notify.render_alert_message(_alert("any name", "9.99", url))


def test_message_exactly_at_budget_survives():
    # A name trimmed exactly to fit must render under the cap, intact URL.
    url = "https://www.ebay.com/itm/12345"
    alert = _alert("b" * 4000, "9.00", url)
    message = notify.render_alert_message(alert)
    assert len(message) == notify.MAX_MESSAGE_CHARS
    assert message.endswith(f"]({url})")


def test_trim_never_splits_an_escape_pair():
    # Force the cut to land between "\" and its escaped char: the
    # renderer must back off so no lone backslash ends the kept run.
    url = "https://www.ebay.com/itm/12345"
    overhead = (
        notify.MESSAGE_FIXED_CHARS
        + len("$9.00")
        + len(url)
        + len(notify.TRIM_MARKER)
    )
    keep = notify.MAX_MESSAGE_CHARS - overhead
    name = "x" * (keep - 1) + "*tail"
    message = notify.render_alert_message(_alert(name, "9.00", url))
    assert len(message) <= notify.MAX_MESSAGE_CHARS
    assert message.endswith(f"]({url})")
    match = re.match(
        r"^\[Found (?P<name>.+) for \$9\.00\]\(" + re.escape(url) + r"\)$",
        message,
        re.DOTALL,
    )
    assert match is not None
    kept = match.group("name")
    assert kept.endswith(notify.TRIM_MARKER)
    assert not kept[: -len(notify.TRIM_MARKER)].endswith("\\")


def test_unicode_names_render_within_budget():
    name = "Ünïcødé — “quoté” ✓ " * 300
    alert = _alert(name, "9.00", "https://www.ebay.com/itm/12345")
    message = notify.render_alert_message(alert)
    assert len(message) <= notify.MAX_MESSAGE_CHARS
    assert message.endswith("](https://www.ebay.com/itm/12345)")


def test_oversized_stored_message_is_recoverable():
    url = "https://www.ebay.com/itm/12345"
    oversized = (
        "[Found " + "n" * 5000 + f" for $9.00]({url})"
    )
    recovered = notify.recover_oversized(oversized)
    assert recovered is not None
    assert len(recovered) <= notify.MAX_MESSAGE_CHARS
    assert recovered.endswith(f"]({url})")


def test_unrecoverable_stored_message_reported():
    assert notify.recover_oversized("total garbage without shape") is None


def test_recovery_preserves_escaped_names():
    url = "https://www.ebay.com/itm/12345"
    stored = "[Found " + notify.escape_markdown("@evil" * 2000) + f" for $1.00]({url})"
    recovered = notify.recover_oversized(stored)
    assert recovered is not None
    assert len(recovered) <= notify.MAX_MESSAGE_CHARS
    assert "\\@evil" in recovered
    assert recovered.endswith(f"]({url})")
