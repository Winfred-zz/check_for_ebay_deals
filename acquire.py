"""Bounded async eBay Browse API acquisition; no live HTML scraping.

OAuth application tokens stay in memory, are refreshed before expiry and once
on a search 401. Credentials and response bodies never enter diagnostics.
Every search includes authentication, body reads and retry waits in its budget.
HTML parsing remains available only for offline fixtures in parse.py.
"""

import asyncio
import email.utils
import json
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import cast

import aiohttp

import clock
import parse
from config import EbayCredentials

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
MAX_BODY_BYTES = 8 * 1024 * 1024
TRANSIENT_STATUSES = (429, 500, 502, 503, 504)


class AcquireTransient(Exception):
    """Retryable failure or cancellation under the bounded search budget."""


class AcquirePermanent(Exception):
    """Nonretryable API, authorization or response-contract failure."""


class _Unauthorized(Exception):
    pass


def parse_retry_after(
    header: str | None,
    utc_now: Callable[[], datetime] = clock.utc_now,
) -> float | None:
    if header is None:
        return None
    try:
        return max(0.0, float(header.strip()))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(header.strip())
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - utc_now()).total_seconds())


def _object(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _array(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _money(value: object) -> Decimal | None:
    obj = _object(value)
    raw = obj.get("value")
    if obj.get("currency") != "USD" or not isinstance(raw, str):
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def parse_results(payload: dict[str, object]) -> parse.ParsedPage:
    """Normalize API summaries without changing legacy numeric dedup identity."""
    if payload.get("errors") or payload.get("warnings"):
        raise AcquirePermanent(
            "eBay API reported errors or ignored/unsupported search parameters"
        )
    total = payload.get("total")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise AcquirePermanent("eBay API response lacks a valid result total")
    summaries = payload.get("itemSummaries", [] if total == 0 else None)
    if not isinstance(summaries, list) or (total > 0 and not summaries):
        raise AcquirePermanent("eBay API response lacks result summaries")
    items: dict[str, parse.ItemRecord] = {}
    rejected: list[parse.RejectionRecord] = []
    for position, raw in enumerate(cast("list[object]", summaries)):
        obj = _object(raw)
        link = obj.get("itemWebUrl")
        item_id, link_error = parse.extract_item_id(link)
        legacy_id = obj.get("legacyItemId")
        price = _money(obj.get("price"))
        title = obj.get("title")
        shipping = _array(obj.get("shippingOptions"))
        free_shipping = any(
            _money(_object(option).get("shippingCost")) == Decimal(0)
            for option in shipping
        )
        reason = None
        if item_id is None:
            reason = link_error or "invalid listing URL"
        elif legacy_id != item_id:
            reason = "legacy listing ID does not match item URL"
        elif not isinstance(title, str) or not title.strip():
            reason = "missing listing title"
        elif price is None:
            reason = "price is not an exact nonnegative USD amount"
        elif "FIXED_PRICE" not in _array(obj.get("buyingOptions")):
            reason = "not fixed-price"
        elif _object(obj.get("itemLocation")).get("country") != "US":
            reason = "item location is not US"
        elif not free_shipping:
            reason = "free USD shipping not confirmed"
        elif obj.get("conditionId") == "7000":
            reason = "parts-only condition"
        elif item_id in items and items[item_id]["price_usd"] <= price:
            reason = "duplicate listing is not cheaper"
        if reason is not None:
            rejected.append({"reason": reason, "position": position})
            continue
        assert item_id is not None and price is not None and isinstance(title, str)
        # extract_item_id validated the host. Canonical US URL preserves the
        # numeric listing identity and drops tracking/variation delimiters.
        seller = _object(obj.get("seller")).get("username")
        items[item_id] = {
            "id": item_id,
            "title": title,
            "subtitle": None,
            "price_usd": price,
            "seller_name": seller if isinstance(seller, str) else None,
            "sales_count": None,
            "seller_rating": None,
            "link": f"https://www.ebay.com/itm/{item_id}",
        }
    return {
        "layout": "no-results" if total == 0 else "results",
        "items": list(items.values()),
        "rejected": rejected,
        "result_count": total,
    }


class BrowseClient:
    """One lifecycle's API client and in-memory application-token cache."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        credentials: EbayCredentials,
        *,
        token_url: str = TOKEN_URL,
        search_url: str = SEARCH_URL,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = clock.utc_now,
        max_retries: int = 4,
    ) -> None:
        self._session = session
        self._credentials = credentials
        self._token_url = token_url
        self._search_url = search_url
        self._monotonic = monotonic
        self._utc_now = utc_now
        self._max_retries = max_retries
        self._token: str | None = None
        self._expires = 0.0

    async def _exchange(
        self, method: str, url: str, remaining: float,
        headers: dict[str, str], params: dict[str, str] | None,
        data: dict[str, str] | None,
    ) -> tuple[int, str | None, dict[str, object]]:
        timeout = aiohttp.ClientTimeout(
            total=remaining, sock_connect=min(10, remaining),
        )
        async with self._session.request(
            method, url, headers=headers, params=params, data=data,
            timeout=timeout, allow_redirects=False,
        ) as response:
            status = response.status
            retry_after = response.headers.get("Retry-After")
            if status != 200:
                return status, retry_after, {}
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                if len(body) + len(chunk) > MAX_BODY_BYTES:
                    raise AcquirePermanent("eBay API response exceeds body size limit")
                body.extend(chunk)
            try:
                raw: object = json.loads(body)
            except (ValueError, UnicodeError):
                raise AcquirePermanent("eBay API returned invalid JSON") from None
            if not isinstance(raw, dict):
                raise AcquirePermanent("eBay API response must be a JSON object")
            return status, retry_after, cast("dict[str, object]", raw)

    async def _request(
        self, method: str, url: str, *, deadline: float,
        cancel_event: asyncio.Event, headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> dict[str, object]:
        for attempt in range(self._max_retries + 1):
            if cancel_event.is_set():
                raise AcquireTransient("search cancelled before request")
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise AcquireTransient("search budget exhausted")
            request = asyncio.create_task(
                self._exchange(
                    method, url, remaining, headers or {}, params, data,
                )
            )
            cancelled = asyncio.create_task(cancel_event.wait())
            retry_after = None
            try:
                done, _ = await asyncio.wait(
                    (request, cancelled), return_when=asyncio.FIRST_COMPLETED,
                )
                if cancelled in done:
                    raise AcquireTransient("search cancelled during request")
                status, retry_after, payload = await request
                if status == 200:
                    return payload
                if status == 401:
                    raise _Unauthorized
                if status not in TRANSIENT_STATUSES:
                    raise AcquirePermanent(f"eBay API request failed: HTTP {status}")
                failure = f"HTTP {status}"
            except (aiohttp.ClientConnectorSSLError, aiohttp.InvalidURL):
                raise AcquirePermanent("permanent eBay API TLS/URL failure") from None
            except (aiohttp.ClientError, TimeoutError):
                # aiohttp exception text may contain headers/URLs: never log it.
                failure = "network/timeout failure"
            finally:
                for task in (request, cancelled):
                    if task.done():
                        if not task.cancelled():
                            _ = task.exception()
                    else:
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
            if attempt == self._max_retries:
                raise AcquireTransient(
                    f"eBay API gave up after {self._max_retries} retries: {failure}"
                )
            delay = parse_retry_after(retry_after, self._utc_now)
            if delay is None:
                delay = min(5.0 * (1 << attempt), 300.0)
            remaining = deadline - self._monotonic()
            if remaining <= 0 or delay >= remaining:
                raise AcquireTransient("retry wait exceeds remaining search budget")
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=delay)
            except TimeoutError:
                continue
            raise AcquireTransient("search cancelled during retry wait")
        raise AssertionError("unreachable")

    async def _application_token(
        self, deadline: float, cancel_event: asyncio.Event,
    ) -> str:
        if self._token is not None and self._monotonic() < self._expires:
            return self._token
        try:
            payload = await self._request(
                "POST", self._token_url, deadline=deadline,
                cancel_event=cancel_event,
                headers={
                    "Authorization": aiohttp.encode_basic_auth(
                        self._credentials["client_id"],
                        self._credentials["client_secret"],
                    ),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials", "scope": SCOPE},
            )
        except _Unauthorized:
            raise AcquirePermanent(
                "eBay OAuth credentials rejected (HTTP 401)"
            ) from None
        token = payload.get("access_token")
        expires = payload.get("expires_in")
        if (not isinstance(token, str) or not token
                or not isinstance(expires, int) or isinstance(expires, bool)
                or expires <= 0):
            raise AcquirePermanent("eBay OAuth response lacks a valid token/expiry")
        self._token = token
        self._expires = self._monotonic() + expires - min(30.0, expires / 10)
        return token

    async def search(
        self, query: str, *, deadline_monotonic: float,
        cancel_event: asyncio.Event,
    ) -> parse.ParsedPage:
        for refresh in range(2):
            token = await self._application_token(deadline_monotonic, cancel_event)
            try:
                payload = await self._request(
                    "GET", self._search_url, deadline=deadline_monotonic,
                    cancel_event=cancel_event,
                    headers={"Authorization": f"Bearer {token}",
                             "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
                    params={"q": query, "limit": "200", "sort": "price", "filter":
                            "buyingOptions:{FIXED_PRICE},itemLocationCountry:US,"
                            "maxDeliveryCost:0"},
                )
            except _Unauthorized:
                self._token = None
                self._expires = 0.0
                if refresh == 0:
                    continue
                raise AcquirePermanent(
                    "eBay Browse authorization rejected after token refresh (HTTP 401)"
                ) from None
            return parse_results(payload)
        raise AssertionError("unreachable")
