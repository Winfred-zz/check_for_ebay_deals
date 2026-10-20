"""Integration tests for acquire.py against a loopback HTTP server only.

Never touches live eBay: the server binds 127.0.0.1 (allowed by the
network guard in conftest). Covers the CR02/CR11/CR16 contract:
aiohttp fetches run on the event loop without starving sibling
tasks, every fetch is bounded by an explicit monotonic budget,
Retry-After (integer seconds AND HTTP-date) is parsed and honored —
waits longer than the remaining budget defer the search instead of
sleeping through them, TLS/URL failures are PERMANENT while
connection/timeout failures stay transient, permanent 4xx is never
retried, and cooperative cancellation interrupts a retry wait
immediately.
"""

import asyncio
import time
from collections.abc import Callable
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import aiohttp
import pytest

import acquire
import clock
from acquire import AcquirePermanent, AcquireTransient, parse_retry_after

NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_now() -> datetime:
    return NOW


@contextmanager
def server(
    responses: list[tuple[int, dict[str, str], str]],
    delays: dict[int, float] | None = None,
):
    hits: list[str] = []
    wait_by_index = delays or {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            idx = min(len(hits) - 1, len(responses) - 1)
            status, headers, body = responses[idx]
            delay = wait_by_index.get(idx, 0.0)
            if delay:
                time.sleep(delay)
            payload = body.encode()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args) -> None:
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}/", hits
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def _run_fetch(
    url: str,
    *,
    budget: float = 30.0,
    deadline: float | None = None,
    max_retries: int = 4,
    monotonic: Callable[[], float] = time.monotonic,
    utc_now: Callable[[], datetime] = clock.utc_now,
) -> str:
    async def go() -> str:
        async with aiohttp.ClientSession() as session:
            return await acquire.fetch_search_page_async(
                session,
                "query",
                url=url,
                deadline_monotonic=(
                    time.monotonic() + budget if deadline is None else deadline
                ),
                cancel_event=asyncio.Event(),
                max_retries=max_retries,
                monotonic=monotonic,
                utc_now=utc_now,
            )

    return asyncio.run(go())


def _stepping_clock(step: float):
    state = {"t": 0.0}

    def tick() -> float:
        current = state["t"]
        state["t"] = current + step
        return current

    return tick


def test_rate_limit_honors_retry_after() -> None:
    with server([(429, {"Retry-After": "1"}, ""), (200, {}, "results")]) as info:
        url, hits = info
        html = _run_fetch(url)
        assert html == "results"
        assert len(hits) == 2


def test_redirect_chain_is_followed_to_success() -> None:
    with server(
        [(302, {"Location": "/next-page"}, ""), (200, {}, "results")]
    ) as info:
        url, hits = info
        html = _run_fetch(url)
        assert html == "results"
        assert len(hits) == 2
        assert hits[1].startswith("/next-page")


@pytest.mark.parametrize(
    ("status", "error", "count"),
    [
        (404, AcquirePermanent, 1),
        (403, AcquirePermanent, 1),
    ],
)
def test_permanent_4xx_never_retried(
    status: int, error: type[Exception], count: int
) -> None:
    with server([(status, {}, "failure")]) as info:
        url, hits = info
        with pytest.raises(error, match=f"HTTP {status}"):
            _run_fetch(url)
        assert len(hits) == count


def test_transient_5xx_retries_are_bounded() -> None:
    with server([(500, {}, "failure")]) as info:
        url, hits = info
        with pytest.raises(AcquireTransient, match="gave up after 1 retries"):
            _run_fetch(url, max_retries=1)
        assert len(hits) == 2


def test_budget_exhausted_before_attempt_defers_search() -> None:
    with server([(200, {}, "unused")]) as info:
        url, hits = info
        with pytest.raises(AcquireTransient, match="budget exhausted"):
            _run_fetch(url, deadline=0.0, monotonic=_stepping_clock(0.0))
        assert len(hits) == 0


def test_retry_after_beyond_budget_defers_instead_of_sleeping() -> None:
    header = formatdate((NOW + timedelta(seconds=3600)).timestamp(), usegmt=True)
    with server([(429, {"Retry-After": header}, "")]) as info:
        url, hits = info
        with pytest.raises(AcquireTransient, match="exceeds remaining"):
            _run_fetch(
                url,
                deadline=10.0,
                monotonic=_stepping_clock(6.0),
                utc_now=_fixed_now,
            )
        assert len(hits) == 1


def test_expired_http_date_retry_after_retries_immediately() -> None:
    header = formatdate((NOW - timedelta(seconds=3600)).timestamp(), usegmt=True)
    with server([(429, {"Retry-After": header}, ""), (200, {}, "results")]) as info:
        url, hits = info
        html = _run_fetch(url, utc_now=_fixed_now)
        assert html == "results"
        assert len(hits) == 2


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("7", 7.0),
        ("0", 0.0),
        ("-5", 0.0),
        ("not a date", None),
        (None, None),
    ],
)
def test_parse_retry_after_seconds(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header, _fixed_now) == expected


def test_parse_retry_after_http_date_delta() -> None:
    future = formatdate((NOW + timedelta(seconds=90)).timestamp(), usegmt=True)
    assert parse_retry_after(future, _fixed_now) == 90.0
    past = formatdate((NOW - timedelta(seconds=90)).timestamp(), usegmt=True)
    assert parse_retry_after(past, _fixed_now) == 0.0
    naive = "Tue, 20 Oct 2026 12:01:00"
    assert parse_retry_after(naive, _fixed_now) == 60.0


def test_cancellation_interrupts_retry_wait_immediately() -> None:
    with server([(503, {}, "failure")]) as info:
        url, hits = info

        async def go() -> float:
            event = asyncio.Event()
            started = time.monotonic()
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    acquire.fetch_search_page_async(
                        session,
                        "query",
                        url=url,
                        deadline_monotonic=time.monotonic() + 60.0,
                        cancel_event=event,
                    )
                )
                await asyncio.sleep(0.3)
                event.set()
                with pytest.raises(AcquireTransient, match="cancelled"):
                    await task
            return time.monotonic() - started

        elapsed = asyncio.run(go())
        # Backoff wanted 5 s; cancellation must cut the wait short (CR03).
        assert elapsed < 3.0
        assert len(hits) == 1


def test_slow_fetch_does_not_starve_sibling_tasks() -> None:
    """CR02: a 1.5 s slow fetch must not block the event loop."""
    with server([(200, {}, "results")], delays={0: 1.5}) as info:
        url, _ = info

        async def go() -> tuple[str, int]:
            ticks = 0

            async def heartbeat() -> None:
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.1)
                    ticks += 1

            async with aiohttp.ClientSession() as session:
                hb = asyncio.create_task(heartbeat())
                html = await acquire.fetch_search_page_async(
                    session,
                    "query",
                    url=url,
                    deadline_monotonic=time.monotonic() + 30.0,
                    cancel_event=asyncio.Event(),
                )
                hb.cancel()
                with suppress(asyncio.CancelledError):
                    await hb
            return html, ticks

        html, ticks = asyncio.run(go())
        assert html == "results"
        assert ticks >= 5


def test_connection_failure_is_transient_and_bounded() -> None:
    with server([(200, {}, "unused")]) as info:
        url, _ = info
    with pytest.raises(AcquireTransient, match="gave up after 0 retries"):
        _run_fetch(url, max_retries=0, budget=5.0)


def test_tls_failure_to_plain_server_is_permanent() -> None:
    """TLS handshake against a plain-HTTP server: config failure, CR11."""
    with server([(200, {}, "unused")]) as info:
        port = info[0].rsplit(":", 1)[1]
        with pytest.raises(AcquirePermanent, match="permanent request failure"):
            _run_fetch(f"https://127.0.0.1:{port}/")


def test_layout_status_delegates_to_parse_single_pass() -> None:
    # CR16 probe regression: "10 results" is NOT a clean empty page.
    assert acquire.layout_status("<h1>10 results</h1>") == "suspicious"
    assert acquire.layout_status("<div>0 results</div>") == "no-results"
    assert acquire.layout_status("<div>Access Denied</div>") == "denied"
