"""Integration tests for acquire.py against a loopback HTTP server only.

Never touches live eBay: the server binds 127.0.0.1 (allowed by the
network guard in conftest) and sleep is injected so retry waits are
recorded, not slept. Asserts the retry policy from design.md:
Retry-After honored, permanent 4xx never retried, bounded transient
retries, cancellation stops the next request.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import requests

from acquire import AcquirePermanent, AcquireTransient, fetch_search_page


@contextmanager
def server(responses: list[tuple[int, dict[str, str], str]]):
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            idx = min(len(hits) - 1, len(responses) - 1)
            status, headers, body = responses[idx]
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


def test_rate_limit_honors_retry_after() -> None:
    sleeps: list[float] = []
    with server([(429, {"Retry-After": "7"}, ""), (200, {}, "results")]) as info:
        url, hits = info
        with requests.Session() as session:
            html = fetch_search_page(session, "query", url=url,
                                     sleep=sleeps.append)
        assert html == "results"
        assert len(hits) == 2
        assert sleeps == [7.0]


@pytest.mark.parametrize(
    ("status", "error", "count"),
    [
        (404, AcquirePermanent, 1),
        (500, AcquireTransient, 3),
    ],
)
def test_http_failures_are_classified_and_bounded(
    status: int, error: type[Exception], count: int
) -> None:
    with server([(status, {}, "failure")]) as info:
        url, hits = info
        with requests.Session() as session, pytest.raises(error):
            fetch_search_page(session, "query", url=url, max_retries=2,
                              sleep=lambda _: None)
        assert len(hits) == count


def test_cancellation_after_failure_stops_next_request() -> None:
    cancelled = False

    def sleeper(_: float) -> None:
        nonlocal cancelled
        cancelled = True

    with server([(503, {}, "failure")]) as info:
        url, hits = info
        with requests.Session() as session, pytest.raises(
            AcquireTransient, match="cancelled"
        ):
            fetch_search_page(session, "query", url=url, sleep=sleeper,
                              cancel_check=lambda: cancelled)
        assert len(hits) == 1


def test_connection_failure_exhausts_retries() -> None:
    with server([(200, {}, "unused")]) as info:
        url, _ = info
    sleeps: list[float] = []
    with requests.Session() as session, pytest.raises(AcquireTransient):
        fetch_search_page(session, "query", url=url, max_retries=1,
                          sleep=sleeps.append, timeout=0.2)
    assert sleeps == [5.0]
