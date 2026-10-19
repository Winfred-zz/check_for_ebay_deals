"""Alert rendering: escaping, URL validation, price guards, length caps."""

from decimal import Decimal

import pytest

import notify


def test_ordinary_name_renders_friendly_line():
    name = "Gundam GM Cannon"
    url = "https://www.ebay.com/itm/167111111111"
    alert = {
        "id": "167111111111",
        "friendly_name": name,
        "price_usd": Decimal("24.74"),
        "link": url,
    }
    message = notify.render_alert_message(alert)
    assert message == f"[Found {name} for $24.74]({url})"


def test_price_gets_thousands_separator():
    alert = {
        "id": "1",
        "friendly_name": "thing",
        "price_usd": Decimal("1234.56"),
        "link": "https://www.ebay.com/itm/1",
    }
    message = notify.render_alert_message(alert)
    assert "for $1,234.56]" in message


def test_markdown_and_mention_like_names_are_escaped():
    raw_name = "@admin **[buy now](https://evil.example)** #h <tag>"
    escaped = notify.escape_markdown(raw_name)
    alert = {
        "id": "1",
        "friendly_name": raw_name,
        "price_usd": Decimal("1.00"),
        "link": "https://www.ebay.com/itm/1",
    }
    rendered = notify.render_alert_message(alert)
    assert escaped in rendered
    assert "\\@admin" in rendered
    for token in ("**", "](https://evil.example)"):
        assert token not in rendered


def test_link_like_name_cannot_add_extra_hyperlink():
    url = "https://www.ebay.com/itm/9"
    alert = {
        "id": "9",
        "friendly_name": "[click](https://evil.example/x)",
        "price_usd": Decimal("5"),
        "link": url,
    }
    rendered = notify.render_alert_message(alert)
    assert rendered.count("](") == 1
    assert "](" + url + ")" in rendered
    assert "](" + "https://evil" not in rendered


def test_url_validation_rejects_lookalikes():
    bad_urls = [
        "http://www.ebay.com/itm/1",
        "https://ebay.com/itm/1",
        "https://www.ebay.com.itm/1",
        "https://evil.com/itm/1",
        "https://www.ebay.com/str/123",
        "file:///etc/passwd",
        "not a url",
    ]
    for url in bad_urls:
        alert = {
            "id": "1",
            "friendly_name": "x",
            "price_usd": Decimal("1"),
            "link": url,
        }
        with pytest.raises(ValueError, match="listing URL rejected"):
            _ = notify.render_alert_message(alert)


def test_accepted_hosts():
    for host in ("com", "co.uk", "com.au", "de", "ca", "it", "fr", "es", "nl"):
        url = f"https://www.ebay.{host}/itm/123456"
        alert = {
            "id": "7",
            "friendly_name": "x",
            "price_usd": Decimal("1"),
            "link": url,
        }
        message = notify.render_alert_message(alert)
        assert message.endswith(f"({url})")


def test_nan_and_negative_prices_rejected():
    for price in (Decimal("NaN"), Decimal("-5")):
        alert = {
            "id": "it-42",
            "friendly_name": "x",
            "price_usd": price,
            "link": "https://www.ebay.com/itm/1",
        }
        with pytest.raises(
            ValueError, match="price rejected for item it-42"
        ):
            _ = notify.render_alert_message(alert)


def test_huge_name_is_trimmed_url_never_is():
    url = "https://www.ebay.com/itm/" + "1" * 256
    price_text = "$9.99"
    alert = {
        "id": "1",
        "friendly_name": "A" * 5000,
        "price_usd": Decimal("9.99"),
        "link": url,
    }
    message = notify.render_alert_message(alert)
    assert len(message) <= notify.MAX_MESSAGE_CHARS
    assert message.endswith(f"({url})")
    assert "A.." in message  # trim marker appended after the kept run
    keep = (
        notify.MAX_MESSAGE_CHARS
        - len(url)
        - notify.MESSAGE_FIXED_CHARS
        - len(price_text)
        - len(notify.TRIM_MARKER)
    )
    assert message.count("A") == keep


def test_exact_budget_keeps_full_name_untrimmed():
    url = "https://www.ebay.com/itm/12345"
    price_text = "$9.00"
    keep = (
        notify.MAX_MESSAGE_CHARS - len(url) - notify.MESSAGE_FIXED_CHARS
        - len(price_text)
    )
    name = "b" * keep
    alert = {
        "id": "1",
        "friendly_name": name,
        "price_usd": Decimal("9.00"),
        "link": url,
    }
    message = notify.render_alert_message(alert)
    expected = f"[Found {name} for {price_text}]({url})"
    assert message == expected
    assert len(message) == notify.MAX_MESSAGE_CHARS
