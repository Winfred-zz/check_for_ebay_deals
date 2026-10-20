"""Async HTTP acquisition for one eBay search page, under a budget.

Approved contract (0003 CR02/CR11/CR16/CR22):
- aiohttp on the shared event loop: a slow fetch never starves the
  Discord gateway heartbeat, other searches, or shutdown handling.
- Every search runs inside an explicit monotonic deadline supplied by
  the caller. Each attempt bounds the request (ClientTimeout total =
  remaining budget); when the budget is spent — including a
  Retry-After whose wait exceeds the remainder — the search is
  DEFERRED (AcquireTransient), not burned through unbounded sleeping.
- Retry-After is parsed, not blindly slept: integer seconds or an
  HTTP-date (delta from the current UTC clock; an expired date means
  retry now). Unparseable values fall back to bounded exponential
  backoff.
- Exception classification is explicit: TLS/URL-shape failures are
  PERMANENT (a proxy/handshake misconfiguration is not retried as a
  transient network blip); connection, timeout, payload, and
  redirect-loop failures are TRANSIENT and bounded by max_retries.
- 429/5xx are transient with bounded retries; permanent 4xx (403,
  404, ...) is never retried as if transient (F02/F03).
- Cancellation is cooperative: cancel_event is checked before each
  attempt, and retry waits race the event, so shutdown interrupts a
  sleep immediately (no 300-second tail on SIGTERM).
- Layout classification lives in parse.py only (CR22): layout_status
  delegates; this module does not parse HTML.

TLS verification stays enabled (default connector); anti-bot
protections are not bypassed. Everything time- and network-related
is injectable so tests run offline against loopback servers only.
"""

import asyncio
import email.utils
import time
from collections.abc import Callable
from datetime import datetime, timezone

import aiohttp

import clock
import parse

SEARCH_URL = "https://www.ebay.com/sch/i.html"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# NOTE: refresh the UA string when deploying; see README troubleshooting.
MAX_REDIRECTS = 5

TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
PERMANENT_STATUSES = (403, 404, 407, 408, 414, 415, 416, 420, 422, 455)


class AcquireTransient(Exception):
    """Retryable failures exhausted the bounded policy (recoverable)."""


class AcquirePermanent(Exception):
    """Non-retryable HTTP/URL/TLS failure; caller treats as scan failure."""


def build_params(search_query: str, page: int = 1) -> dict[str, str]:
    """Query params kept from the working scraper; `pages`/`completed`
    options are dropped from the runtime path (F25: no caller used them;
    one page per search is the approved cadence boundary)."""
    return {
        "_nkw": search_query,
        "LH_Sold": "0",
        "LH_BIN": "1",
        "LH_PrefLoc": "1",
        "LH_FS": "1",
        "_sop": "15",
        "_pgn": str(page),
    }


def parse_retry_after(
    header: str | None,
    utc_now: Callable[[], datetime] = clock.utc_now,
) -> float | None:
    """Seconds to wait from a Retry-After header, or None if absent.

    Integer seconds are used directly; an HTTP-date is converted to
    the delta from the current UTC clock (an expired date = 0, retry
    now). Anything unparseable returns None so the caller falls back
    to bounded backoff (CR11).
    """
    if header is None:
        return None
    text = header.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = (parsed - utc_now()).total_seconds()
    return max(0.0, delta)


def retry_delay_seconds(
    attempt: int,
    retry_after_header: str | None,
    utc_now: Callable[[], datetime] = clock.utc_now,
) -> float:
    """Backoff for attempt (0-based); honors a valid Retry-After.

    A valid header value wins; missing/unparseable headers fall back
    to exponential backoff capped at 300 s (CR11).
    """
    retry_after = parse_retry_after(retry_after_header, utc_now)
    if retry_after is not None:
        return min(retry_after, 300.0)
    return min(5.0 * float(1 << attempt), 300.0)


async def _cancellable_sleep(
    delay: float, cancel_event: asyncio.Event
) -> None:
    """Sleep `delay`, interrupting immediately when cancel_event fires."""
    if delay <= 0:
        if cancel_event.is_set():
            raise AcquireTransient("scan cancelled during retry wait")
        return
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=delay)
    except TimeoutError:
        return  # the full delay elapsed without a cancel request
    raise AcquireTransient("scan cancelled during retry wait")


async def fetch_search_page_async(
    session: aiohttp.ClientSession,
    search_query: str,
    *,
    deadline_monotonic: float,
    cancel_event: asyncio.Event,
    url: str = SEARCH_URL,
    max_retries: int = 4,
    monotonic: Callable[[], float] = time.monotonic,
    utc_now: Callable[[], datetime] = clock.utc_now,
) -> str:
    """Fetch one search page under a monotonic deadline; return HTML text.

    `deadline_monotonic` is an absolute time.monotonic() value owned by
    the caller (per-search budget, CR02/CR11). Raises AcquireTransient
    when retries, the budget, or a cancel request ends the attempt,
    and AcquirePermanent for non-retryable 4xx/TLS/URL failures.
    Only HTTP 200 returns text; status is checked before parsing (F03).
    """
    for attempt in range(max_retries + 1):
        if cancel_event.is_set():
            raise AcquireTransient("scan cancelled before/at fetch attempt")
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise AcquireTransient(
                f"search budget exhausted before attempt {attempt}"
            )
        timeout = aiohttp.ClientTimeout(total=remaining, sock_connect=10)
        status: int | None = None
        retry_after: str | None = None
        try:
            async with session.get(
                url,
                params=build_params(search_query),
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
                allow_redirects=True,
                max_redirects=MAX_REDIRECTS,
            ) as response:
                status = response.status
                if status == 200:
                    return await response.text()
                if status in (429, 503):
                    retry_after = response.headers.get("Retry-After")
        except (aiohttp.ClientConnectorSSLError, aiohttp.InvalidURL) as exc:
            raise AcquirePermanent(
                f"permanent request failure for query {search_query!r}: {exc}"
            ) from exc
        except (
            aiohttp.ClientConnectorError,
            aiohttp.ServerTimeoutError,
            aiohttp.ClientPayloadError,
            aiohttp.TooManyRedirects,
            TimeoutError,
        ) as exc:
            status = None  # transport-level transient failure
            retry_after = None
            last_error: str | None = str(exc)
        else:
            last_error = f"HTTP {status}"
        assert status is not None or last_error is not None
        if status is not None:
            is_permanent = (
                400 <= status < 500 and status not in TRANSIENT_STATUSES
            )
            if status in PERMANENT_STATUSES or is_permanent:
                raise AcquirePermanent(
                    f"permanent HTTP {status} for query {search_query!r}"
                )
        if attempt >= max_retries:
            raise AcquireTransient(
                f"gave up after {max_retries} retries (last {last_error})"
            )
        delay = retry_delay_seconds(attempt, retry_after, utc_now)
        remaining = deadline_monotonic - monotonic()
        if delay > remaining:
            raise AcquireTransient(
                f"search deferred: retry in {delay:.0f}s exceeds remaining "
                + f"budget {max(0.0, remaining):.0f}s"
            )
        await _cancellable_sleep(delay, cancel_event)
    raise AcquireTransient("unreachable loop exit")


def layout_status(text: str) -> str:
    """Classify a page's layout via parse.py only (CR22, no second parse).

    Returns "results" | "no-results" | "denied" | "suspicious" per
    parse.classify_layout precedence (deny markers, cards, exact
    result counts, known zero wording). Callers needing cards use
    parse.parse_search_page once for layout + cards + rejections.
    """
    return parse.parse_search_page(text)["layout"]
