"""HTTP acquisition for one eBay search page, with bounded retries.

Separates acquisition from parsing so fixtures exercise the real
parser without network access (step 3). TLS verification is left at
requests' default (enabled) and never disabled; anti-bot protections
are not bypassed. Retry policy (approved): only transient conditions
are retried — connection/timeout errors, 429 (honor a valid Retry-After
within the overall budget), and 5xx. Permanent 4xx (404, 403) is NOT
retried as if transient. Retry count is bounded (F02): exhaustion
raises AcquireTransient, distinct from "no matches" (F03) which the
caller determines from parse results.

Everything sleep- and network-related is injectable so tests can run
fast offline or against a local loopback server only; production code
path is the same code under test.
"""

import time
import requests
from collections.abc import Callable
from parse import CARD_SELECTOR  # shares the card-selector convention

SEARCH_URL = "https://www.ebay.com/sch/i.html"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# NOTE: refresh the UA string when deploying; see README troubleshooting.
# The old "whatismybrowser" comment pointed at a stale copy (F24).

TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
PERMANENT_STATUSES = (403, 404, 407, 408, 414, 415, 416, 420, 422, 455)


class AcquireTransient(Exception):
    """Retryable failures exhausted the bounded policy (recoverable)."""


class AcquirePermanent(Exception):
    """Non-retryable HTTP/URL failure (4xx); caller must treat as scan failure."""


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


def retry_delay_seconds(attempt: int, retry_after_header: str | None) -> float:
    """Backoff for attempt (0-based); honors Retry-After when sensible.

    Retry-After is clamped to [0, 300]s; invalid/HTTP-date forms fall
    back to exponential backoff (5s * 2^attempt, capped 300s).
    """
    if retry_after_header:
        try:
            seconds = float(int(retry_after_header))
        except ValueError:
            seconds = None
        if seconds is not None and 0 <= seconds <= 300:
            return max(seconds, 1.0)
    return min(5.0 * float(1 << attempt), 300.0)


def fetch_search_page(
    session: requests.Session,
    search_query: str,
    *,
    url: str = SEARCH_URL,
    max_retries: int = 4,
    timeout: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    cancel_check: Callable[[], bool] = lambda: False,
) -> str:
    """Fetch one search page; return response HTML text.

    session: a requests.Session (production app passes one configured
    in main; tests pass loopback or mocked transports).
    sleep/cancel_check injected so retry logic can be exercised without
    real waits and with cooperative cancellation checkpoints (step 6).
    Raises AcquireTransient/AcquirePermanent; returns page text only
    for HTTP 200-class success (status checked before parsing, F03).
    """
    for attempt in range(max_retries + 1):
        if cancel_check():
            raise AcquireTransient("scan cancelled before/at fetch attempt")
        try:
            response = session.get(
                url,
                params=build_params(search_query),
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            if attempt >= max_retries:
                raise AcquireTransient(
                    f"transient failure after {max_retries} retries: {exc}"
                ) from exc
            sleep(retry_delay_seconds(attempt, None))
            continue
        status = response.status_code
        if status == 200:
            return response.text
        is_permanent = 400 <= status < 500 and status not in TRANSIENT_STATUSES
        if status in PERMANENT_STATUSES or is_permanent:
            raise AcquirePermanent(
                f"permanent HTTP {status} for query {search_query!r}"
            )
        # 429 + 5xx: bounded transient retry; honor Retry-After on 429/503.
        if attempt >= max_retries:
            raise AcquireTransient(
                f"gave up after {max_retries} retries (last status {status})"
            )
        retry_after = None
        if status in (429, 503):
            retry_after = response.headers.get("Retry-After")
        sleep(retry_delay_seconds(attempt, retry_after))
    raise AcquireTransient("unreachable loop exit")


def layout_status(text: str) -> str:
    """Classify a 200 page for the parser/caller (F03).

    Returns "results" when search-result cards exist, "no-results" for a
    clean zero-result page, "denied" for recognizable challenge/deny
    pages, and "suspicious" when a 200 page has neither cards nor any
    known marker — the caller logs suspicious pages and never treats
    them as a clean empty scan. Only recognizable deny markers are
    detected; no claim that every possible layout failure is caught.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(text, "lxml")
    if soup.select(CARD_SELECTOR):
        return "results"
    body_text = soup.get_text(" ", strip=True)
    lowered = body_text.lower()
    if any(marker in lowered for marker in ("access denied", "captcha")):
        return "denied"
    no_results_markers = ("no exact matches", "0 results", "no results")
    if any(marker in lowered for marker in no_results_markers):
        return "no-results"
    return "suspicious"
