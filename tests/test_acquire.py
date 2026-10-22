"""Official API acquisition exercised through a loopback HTTP server only."""

import asyncio
import copy
import json
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

import acquire

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
TOKEN = {"access_token": "fake-application-token", "expires_in": 7200}
CREDS = {"client_id": "fake-client-id", "client_secret": "fake-client-secret"}
SUMMARY = {
    "legacyItemId": "123456789012",
    "itemWebUrl": "https://www.ebay.com/itm/title/123456789012?tracking=x",
    "title": "Used drive", "price": {"value": "19.50", "currency": "USD"},
    "buyingOptions": ["FIXED_PRICE"], "itemLocation": {"country": "US"},
    "shippingOptions": [{"shippingCost": {"value": "0.00", "currency": "USD"}}],
    "conditionId": "3000", "seller": {"username": "example"},
}
PAGE = {"total": 1, "itemSummaries": [SUMMARY]}


@contextmanager
def server(*, tokens=None, searches=None, delay=0.0):
    token_responses = tokens or [(200, {}, TOKEN)]
    search_responses = searches or [(200, {}, PAGE)]
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            route = urlsplit(self.path).path
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            prior = sum(r[0] == route for r in requests)
            requests.append((route, self.path, dict(self.headers), body))
            responses = token_responses if route == "/token" else search_responses
            status, headers, data = responses[min(prior, len(responses) - 1)]
            payload = (
                data.encode() if isinstance(data, str) else json.dumps(data).encode()
            )
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if route != "/token":
                time.sleep(delay)
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(payload)

        do_GET = handle_request
        do_POST = handle_request

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", requests
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def client(session, root, **kwargs):
    return acquire.BrowseClient(
        session, CREDS, token_url=root + "/token", search_url=root + "/search",
        utc_now=lambda: NOW, **kwargs,
    )


async def search(api, *, budget=10.0, event=None):
    return await api.search(
        "drive -parts", deadline_monotonic=time.monotonic() + budget,
        cancel_event=event or asyncio.Event(),
    )


def fetch(root, **kwargs):
    async def go():
        async with aiohttp.ClientSession() as session:
            return await search(client(session, root, **kwargs))
    return asyncio.run(go())


def test_authenticated_search_retains_legacy_identity_and_caches_token():
    with server() as (root, requests):
        async def go():
            async with aiohttp.ClientSession() as session:
                api = client(session, root)
                a = await search(api)
                b = await search(api)
                return a, b
        a, b = asyncio.run(go())
    assert a == b
    assert a["items"][0]["id"] == "123456789012"
    assert a["items"][0]["link"] == "https://www.ebay.com/itm/123456789012"
    assert str(a["items"][0]["price_usd"]) == "19.50"
    assert [r[0] for r in requests] == ["/token", "/search", "/search"]
    token_request, search_request, _ = requests
    assert parse_qs(token_request[3].decode()) == {
        "grant_type": ["client_credentials"], "scope": [acquire.SCOPE],
    }
    assert search_request[2]["Authorization"] == "Bearer fake-application-token"
    params = parse_qs(urlsplit(search_request[1]).query)
    assert params["q"] == ["drive -parts"]
    assert params["filter"] == [
        "buyingOptions:{FIXED_PRICE},itemLocationCountry:US,maxDeliveryCost:0"
    ]


def test_expired_token_is_refreshed_before_second_search():
    with server() as (root, requests):
        async def go():
            now = [0.0]
            async with aiohttp.ClientSession() as session:
                api = client(session, root, monotonic=lambda: now[0])
                for moment in (0.0, 7201.0):
                    now[0] = moment
                    await api.search("drive", deadline_monotonic=moment + 10,
                                     cancel_event=asyncio.Event())
        asyncio.run(go())
    assert [r[0] for r in requests] == ["/token", "/search", "/token", "/search"]


def test_search_401_refreshes_once_then_succeeds():
    with server(searches=[(401, {}, {}), (200, {}, PAGE)]) as (root, requests):
        page = fetch(root)
    assert page["items"][0]["id"] == "123456789012"
    assert [r[0] for r in requests] == ["/token", "/search", "/token", "/search"]


def test_persistent_401_is_bounded_and_never_exposes_credentials():
    with server(searches=[(401, {}, {"secret": CREDS})]) as (root, requests):
        with pytest.raises(
            acquire.AcquirePermanent, match="after token refresh"
        ) as exc:
            fetch(root)
    assert len(requests) == 4
    assert CREDS["client_secret"] not in str(exc.value)


def test_token_rejection_is_not_retried_as_a_network_error():
    with server(tokens=[(401, {}, CREDS)]) as (root, requests):
        with pytest.raises(acquire.AcquirePermanent, match="OAuth credentials"):
            fetch(root)
    assert [r[0] for r in requests] == ["/token"]


@pytest.mark.parametrize("status", [302, 403, 418])
def test_permanent_status_and_redirect_never_forward_secrets(status):
    with server(searches=[(status, {"Location": "/other"}, {})]) as (root, requests):
        with pytest.raises(acquire.AcquirePermanent, match=f"HTTP {status}"):
            fetch(root)
    assert [r[0] for r in requests] == ["/token", "/search"]


def test_retry_after_http_date_then_search_success():
    expired = formatdate((NOW - timedelta(seconds=5)).timestamp(), usegmt=True)
    with server(searches=[(429, {"Retry-After": expired}, {}),
                          (200, {}, PAGE)]) as (root, requests):
        page = fetch(root)
    assert page["items"][0]["id"] == "123456789012"
    assert len(requests) == 3


def test_large_retry_after_does_not_sleep_past_search_budget():
    with server(searches=[(429, {"Retry-After": "3600"}, {})]) as (root, requests):
        with pytest.raises(acquire.AcquireTransient, match="exceeds remaining"):
            fetch(root)
    assert len(requests) == 2


def test_transient_failure_retries_are_bounded():
    with server(searches=[(503, {"Retry-After": "0"}, {})]) as (root, requests):
        with pytest.raises(acquire.AcquireTransient, match="after 1 retries"):
            fetch(root, max_retries=1)
    assert len(requests) == 3


def test_cancel_during_body_read_does_not_starve_heartbeat():
    with server(delay=1.0) as (root, requests):
        async def go():
            event = asyncio.Event()
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(search(client(session, root), event=event))
                for _ in range(100):
                    if any(r[0] == "/search" for r in requests):
                        break
                    await asyncio.sleep(0.01)
                assert any(r[0] == "/search" for r in requests)
                start = time.monotonic()
                event.set()
                with pytest.raises(acquire.AcquireTransient, match="cancelled"):
                    await task
                assert time.monotonic() - start < 0.5
        asyncio.run(go())


def test_slow_body_deadline_includes_auth_and_response_read():
    with server(delay=0.5) as (root, _):
        async def go():
            async with aiohttp.ClientSession() as session:
                with pytest.raises(acquire.AcquireTransient):
                    await search(client(session, root, max_retries=0), budget=0.1)
        asyncio.run(go())


@pytest.mark.parametrize("response", ["not JSON", [], {"total": 3},
                                      {"total": 1, "itemSummaries": []},
                                      {"errors": [{"message": "private"}]}])
def test_malformed_response_is_not_clean_empty_search(response):
    with server(searches=[(200, {}, response)]) as (root, _):
        with pytest.raises(acquire.AcquirePermanent):
            fetch(root)


def test_response_size_is_bounded(monkeypatch):
    monkeypatch.setattr(acquire, "MAX_BODY_BYTES", 1024)
    with server(searches=[(200, {}, " " * 1025)]) as (root, _):
        with pytest.raises(acquire.AcquirePermanent, match="body size limit"):
            fetch(root)


def test_api_warning_cannot_silently_broaden_search():
    page = {**PAGE, "warnings": [{"message": "filter ignored"}]}
    with pytest.raises(acquire.AcquirePermanent, match="unsupported search"):
        acquire.parse_results(page)


def test_valid_zero_results_are_clean_empty():
    page = acquire.parse_results({"total": 0})
    assert page == {"layout": "no-results", "items": [], "rejected": [],
                    "result_count": 0}


@pytest.mark.parametrize("patch", [
    {"price": {"value": "19", "currency": "EUR"}},
    {"price": {"value": "NaN", "currency": "USD"}},
    {"price": {"value": "-1", "currency": "USD"}},
    {"itemWebUrl": "https://evil.example/itm/123456789012"},
    {"legacyItemId": "222222222222"},
    {"conditionId": "7000"},
    {"buyingOptions": ["AUCTION"]},
    {"itemLocation": {"country": "GB"}},
    {"shippingOptions": []},
    {"shippingOptions": [{"shippingCost": {"value": "5", "currency": "USD"}}]},
])
def test_invalid_cheapest_listing_cannot_displace_valid_sibling(patch):
    bad = {**copy.deepcopy(SUMMARY), **patch}
    good = {**copy.deepcopy(SUMMARY), "price": {"value": "25", "currency": "USD"}}
    result = acquire.parse_results({"total": 2, "itemSummaries": [bad, good]})
    assert [(x["id"], str(x["price_usd"])) for x in result["items"]] == [
        ("123456789012", "25")
    ]
    assert len(result["rejected"]) == 1


def test_cheapest_duplicate_keeps_legacy_numeric_identity():
    high = {**SUMMARY, "price": {"value": "99", "currency": "USD"}}
    result = acquire.parse_results({"total": 3, "itemSummaries": [high, SUMMARY, high]})
    assert [(x["id"], str(x["price_usd"])) for x in result["items"]] == [
        ("123456789012", "19.50")
    ]


@pytest.mark.parametrize(("header", "expected"), [
    ("7", 7.0), ("0", 0.0), ("-5", 0.0), ("bad", None), (None, None),
])
def test_retry_after_seconds(header, expected):
    assert acquire.parse_retry_after(header, lambda: NOW) == expected


def test_retry_after_future_date():
    future = formatdate((NOW + timedelta(seconds=90)).timestamp(), usegmt=True)
    assert acquire.parse_retry_after(future, lambda: NOW) == 90
