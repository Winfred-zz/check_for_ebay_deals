"""Deterministic alert selection: cheapest unalerted qualifying listing.

Approved contract (design.md): per scan, per search row, select the
cheapest previously-unalerted valid listing whose USD price is <= the
row ceiling. Do not trust scraped card order; select from validated
data. Ties break by first-seen order. Items that already alerted (sent
set) never re-alert, so a price drop on an alerted item is ignored.
Duplicate item IDs across search rows resolve through shared sent/pending
state: at most one alert per item per scan, claimed in row order.
"""

from decimal import Decimal

from config import SearchRow
from parse import ItemRecord
from typing import TypedDict


class AlertRecord(TypedDict):
    """One selected alert: exactly what notify consumes downstream."""
    id: str
    price_usd: Decimal
    link: str
    friendly_name: str
    search_query: str


def choose_alerts(
    search_rows: list[SearchRow],
    items_by_query: dict[str, list[ItemRecord]],
    excluded_ids: set[str],
) -> list[AlertRecord]:
    """Select at most one alert per search row, deterministically.

    search_rows: validated dicts with keys ``search_query``,
    ``friendly_name``, ``max_price`` (string parseable to a nonnegative
    finite Decimal, enforced by config.py).
    items_by_query: parse.py output keyed by query. Each item has at
    least ``id``, ``price_usd`` (Decimal), ``link``.
    excluded_ids: ids already sent or already claimed pending in this
    scan; they can never alert again.

    Returns a list of alert dicts ``{id, price_usd, link, friendly_name,
    search_query}`` in search-row order. Quiet-window suppression is the
    caller's decision (clock.is_quiet); this function stays pure so tests
    and dry runs can use it directly.
    """
    claimed: set[str] = set(excluded_ids)
    alerts: list[AlertRecord] = []
    for row in search_rows:
        query = row["search_query"]
        ceiling = Decimal(row["max_price"])
        if ceiling < 0 or not ceiling.is_finite():
            # config.py rejects these at startup; defend the boundary.
            continue
        candidates = [
            item
            for item in items_by_query.get(query, [])
            if item["id"] not in claimed and item["price_usd"] <= ceiling
        ]
        if not candidates:
            continue
        cheapest = min(candidates, key=lambda item: item["price_usd"])
        claimed.add(cheapest["id"])
        alerts.append(
            {
                "id": cheapest["id"],
                "price_usd": cheapest["price_usd"],
                "link": cheapest["link"],
                "friendly_name": row["friendly_name"],
                "search_query": query,
            }
        )
    return alerts
