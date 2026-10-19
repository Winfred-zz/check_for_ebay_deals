from decimal import Decimal

from selection import choose_alerts


def item(id, price):
    return {"id": id, "price_usd": Decimal(price),
            "link": f"https://www.ebay.com/itm/{id}"}


def row(query="q", ceiling="30"):
    return {"search_query": query, "friendly_name": query, "max_price": ceiling}


def test_cheapest_not_card_order_and_sent_price_drop_never_realerts():
    items = {"q": [item("a", "24.74"), item("b", "19.50")]}
    assert choose_alerts([row()], items, set())[0]["id"] == "b"
    assert choose_alerts([row()], items, {"b"})[0]["id"] == "a"
    items["q"][1]["price_usd"] = Decimal("1")
    assert choose_alerts([row()], items, {"b"})[0]["id"] == "a"
    assert choose_alerts([row()], items, {"a", "b"}) == []


def test_exact_ceiling_inclusive_and_stable_tie():
    items = {"q": [item("above", "30.01"), item("equal", "30.00"),
                   item("tie", "30.00")]}
    assert choose_alerts([row()], items, set())[0]["id"] == "equal"
    assert choose_alerts([row(ceiling="29.99")], items, set()) == []


def test_shared_ids_claimed_once_in_row_order():
    items = {"q": [item("shared", "5")],
             "r": [item("shared", "5"), item("next", "6")]}
    result = choose_alerts([row(), row("r")], items, set())
    assert [alert["id"] for alert in result] == ["shared", "next"]
