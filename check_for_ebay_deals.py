"""Hourly eBay deal scanner with durable alerts and a bounded shutdown.

Approved lifecycle (design.md + handoff §4 + 0003 CR01-CR22):
- Every config is validated BEFORE any state open, scan, or send;
  config/state errors exit 2 with an actionable message and no
  traceback. The Discord token value is never echoed.
- Single instance: a kernel flock on bot.lock inside the state dir,
  acquired BEFORE any state work; a second process exits 3. The lock
  fd is held for the process lifetime and released on exit.
- Logging lives under the state root (--data-dir/logs): console INFO
  via colorlog + rotating file DEBUG (bot.log, 1 MiB x 5). setup_logging
  is invocation-idempotent (it removes its own handlers first).
- Live mode runs ONE asyncio loop: the Discord gateway is a supervised
  background task started with reconnect=True (CR01); scanners and the
  gateway coexist, so a connected gateway never blocks scans and
  disconnects self-heal. Login rejection (bad token) exits 4; gateway
  startup failure is a fatal startup failure (exit 1), not an auth
  failure.
- Scans are hourly, finish-to-start, never overlapping; fetches use
  aiohttp with an explicit per-search budget (CR02). Pending alerts
  are delivered before fresh fetches (pending-first, CR02): durable
  work always outranks new discovery.
- At-least-once delivery: pending rows are written before the send
  attempt; mark sent only after a successful send; a failed send keeps
  the row pending. Quiet-window state is evaluated at the delivery
  boundary with the live clock (CR10), before every single send.
- SIGTERM/SIGINT set a cooperative cancel flag (no exception is
  injected into running code, CR03). Fetches, retry waits, scan
  sleeps, and the interval sleep all race the cancel event; shutdown
  then drains with a 10-second bounded cleanup of the gateway task,
  HTTP session, Discord client, DB, lock, and log handlers.
- Exit codes: 0 clean; 2 config/state; 3 lock held elsewhere; 4
  Discord login rejected; 5 required --once scan work failed
  (failed searches or persistent send failures; a shutdown cancel is
  not a failure); 1 uncaught failure.

Offline modes: --fixture-dir + manifest.json replaces network fetches
with local HTML and deliveries print through the stub transport (never
contacts Discord); fixture non-dry scans require an explicit --data-dir
so they cannot touch production state, and never import the legacy
skip list. --dry-run prints alert identities, opens state read-only,
never sends, never writes. CHECK_FOR_EBAY_DEALS_DISABLE_NETWORK=1
refuses ANY non-fixture run — dry-run included (CR15). --at fixes the
scan clock for deterministic offline runs; live non-dry delivery on an
--at clock requires CHECK_FOR_EBAY_DEALS_ALLOW_AT_CLOCK=1 (CR10).
"""

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TypedDict, cast

import aiohttp
import colorlog
import discord
from discord.abc import Messageable

import acquire
import clock
import config
import deliver
import notify
import parse
import selection
import state
from config import EnvValues, SearchRow
from parse import ParsedPage

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CSV = os.path.join(PROJECT_ROOT, "configs", "ebay_deals_to_check.csv")
DEFAULT_DATA_DIR = os.path.join(PROJECT_ROOT, "data")
LEGACY_SKIP = os.path.join(PROJECT_ROOT, "configs", state.LEGACY_SKIP_FILE)
STUB_FAIL_AFTER_ENV = "CHECK_FOR_EBAY_DEALS_STUB_FAIL_AFTER"
STUB_BLOCK_SECONDS_ENV = "CHECK_FOR_EBAY_DEALS_STUB_BLOCK_SECONDS"
SCAN_SLEEP_SECONDS_ENV = "CHECK_FOR_EBAY_DEALS_SCAN_SLEEP_SECONDS"
ALLOW_AT_CLOCK_ENV = "CHECK_FOR_EBAY_DEALS_ALLOW_AT_CLOCK"
DISABLE_NETWORK_ENV = "CHECK_FOR_EBAY_DEALS_DISABLE_NETWORK"
SCAN_INTERVAL_SECONDS = 3600
SCAN_BUDGET_SECONDS = 120.0
READY_TIMEOUT_SECONDS = 30.0
SHUTDOWN_DRAIN_SECONDS = 10.0
LOG_DIR = "logs"
APP_HANDLER_TAG = "_check_for_ebay_deals_handler"


class LoginRejected(BaseException):
    """Discord login rejected: exit 4, the token is never echoed."""


class PassOutcome(TypedDict):
    """Structured outcome of one scan pass (CR16)."""
    cancelled: bool
    failed_searches: int
    denied: int
    suspicious: int
    empty: int
    pending_added: int
    sent: int
    send_failures: int
    deferred_quiet: int


class ShutdownState:
    """Invocation-local cooperative cancellation for one main() run."""

    def __init__(self) -> None:
        self.cancel_requested = False
        self.cancel_event = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop | None = None

    def request(self) -> None:
        """Signal-safe cancel request (sets flag + wakes the loop)."""
        self.cancel_requested = True
        loop = self.loop
        if loop is not None:
            loop.call_soon_threadsafe(self.cancel_event.set)
        else:
            self.cancel_event.set()


def _normalize_env(raw: str, name: str) -> str:
    """Strip dotenv quoting so strict parsers accept the value (F15)."""
    return raw.strip().strip("'\"")


def setup_logging(data_dir: str) -> logging.Logger:
    """Console (colorlog, INFO) + rotating file (DEBUG) under data_dir.

    Idempotent for repeated main() calls: this function's own handlers
    are removed and closed before new ones attach, so handler counts
    stay stable across invocations. The log root lives under the state
    root so each --data-dir owns its logs (CR14).
    """
    logger = logging.getLogger("check_for_ebay_deals")
    logger.setLevel(logging.DEBUG)
    for handler in list(logger.handlers):
        if getattr(handler, APP_HANDLER_TAG, False):
            logger.removeHandler(handler)
            handler.close()
    log_dir = os.path.join(data_dir, LOG_DIR)
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError as exc:
        raise config.ConfigError(f"log directory unusable: {exc}") from exc
    console = colorlog.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(levelname)s %(message)s%(reset)s",
            log_colors={
                "DEBUG": "blue",
                "INFO": "cyan",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "red,bg_white",
            },
        )
    )
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "bot.log"),
            maxBytes=1_000_000,
            backupCount=5,
        )
    except OSError as exc:
        raise config.ConfigError(f"log file unusable: {exc}") from exc
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    for handler in (console, file_handler):
        setattr(handler, APP_HANDLER_TAG, True)
        logger.addHandler(handler)
    return logger


def parse_at(value: str) -> datetime:
    """--at: ISO timestamp with UTC offset or Z; deterministic clock."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise config.ConfigError(f"--at must be ISO-UTC: {exc}") from exc
    if moment.tzinfo is None:
        raise config.ConfigError(
            "--at must carry a UTC offset or Z suffix (naive times rejected)"
        )
    return moment.astimezone(timezone.utc)


def open_state_readonly(data_dir: str) -> sqlite3.Connection | None:
    """Dry runs open the DB read-only; absent DB means empty state.

    The URI is percent-encoded via state.readonly_uri (CR07), so '#',
    '?', '%', spaces in the path cannot truncate the query string, and
    the shared schema validator runs before any read (CR12).
    """
    db_path = state.state_db_path(data_dir)
    if not os.path.isfile(db_path):
        return None
    try:
        con = sqlite3.connect(state.readonly_uri(db_path), uri=True)
    except sqlite3.Error as exc:
        raise state.StateError(f"dry run state DB unreadable: {exc}") from exc
    try:
        state.validate_state_schema(con)
    except state.StateError:
        con.close()
        raise
    return con


def load_fixture_manifest(fixture_dir: str, rows: list[SearchRow]) -> dict[str, str]:
    """Validate manifest.json against CSV rows (CR13/CR17).

    Accepts only: a JSON object of string -> string; every target
    resolves inside the fixture root (no traversal/symlink escape);
    every target is an existing readable UTF-8 file. Anything else is
    a startup ConfigError with a path in the message.
    """
    path = os.path.join(fixture_dir, "manifest.json")
    if not os.path.isfile(path):
        raise config.ConfigError(f"fixture dir has no manifest.json at {path}")
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise config.ConfigError(f"fixture manifest is not valid JSON: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise config.ConfigError(f"fixture manifest unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise config.ConfigError("fixture manifest must map query to html file")
    root = os.path.realpath(fixture_dir)
    manifest: dict[str, str] = {}
    for query, target in raw.items():
        if not isinstance(query, str) or not isinstance(target, str):
            raise config.ConfigError(
                "fixture manifest must map string queries to string files"
            )
        resolved = os.path.realpath(os.path.join(fixture_dir, target))
        if not resolved.startswith(root + os.sep):
            raise config.ConfigError(
                f"fixture manifest target escapes the fixture dir: {target!r}"
            )
        if not os.path.isfile(resolved):
            raise config.ConfigError(
                f"fixture manifest target is not a file: {target!r}"
            )
        try:
            with open(resolved, encoding="utf-8") as handle:
                _ = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise config.ConfigError(
                f"fixture file unreadable as UTF-8: {target!r}: {exc}"
            ) from exc
        manifest[query] = target
    missing = sorted(
        row["search_query"] for row in rows if row["search_query"] not in manifest
    )
    if missing:
        raise config.ConfigError(
            "fixture manifest lacks searches: " + ", ".join(missing)
        )
    return manifest


def _validate_env_int(raw: str, env_name: str) -> int:
    value = _normalize_env(raw, env_name)
    try:
        number = int(value)
    except ValueError as exc:
        raise config.ConfigError(
            f"{env_name} must be a nonnegative integer, got {value!r}"
        ) from exc
    if number < 0:
        raise config.ConfigError(
            f"{env_name} must be a nonnegative integer, got {value!r}"
        )
    return number


def _validate_env_seconds(raw: str, env_name: str) -> float:
    value = _normalize_env(raw, env_name)
    try:
        number = float(value)
    except ValueError as exc:
        raise config.ConfigError(
            f"{env_name} must be a nonnegative number of seconds, got {value!r}"
        ) from exc
    if number < 0:
        raise config.ConfigError(
            f"{env_name} must be a nonnegative number of seconds, got {value!r}"
        )
    return number


async def _cancellable_wait(seconds: float, shutdown: ShutdownState) -> bool:
    """Sleep up to `seconds`, racing the cancel event. True if cancelled."""
    if seconds <= 0:
        return shutdown.cancel_requested
    try:
        await asyncio.wait_for(shutdown.cancel_event.wait(), timeout=seconds)
        return True
    except TimeoutError:
        return False


async def scan_pass(
    *,
    rows: list[SearchRow],
    con: sqlite3.Connection | None,
    fixture_dir: str | None,
    manifest: dict[str, str],
    session: aiohttp.ClientSession | None,
    client: discord.Client | None,
    sender: Callable[[str], Awaitable[None]] | None,
    channel_id: int | None,
    dry: bool,
    clock_fn: Callable[[], datetime],
    monotonic: Callable[[], float],
    scan_sleep_seconds: float,
    shutdown: ShutdownState,
    logger: logging.Logger,
) -> PassOutcome:
    """One scan: deliver stored pending first, then fetch/parse/select/persist."""
    outcome: PassOutcome = {
        "cancelled": False,
        "failed_searches": 0,
        "denied": 0,
        "suspicious": 0,
        "empty": 0,
        "pending_added": 0,
        "sent": 0,
        "send_failures": 0,
        "deferred_quiet": 0,
    }
    active_sender = sender
    if dry:
        await _deliver_dry(con, rows, fixture_dir, manifest, session, monotonic,
                           shutdown, logger, outcome)
        return outcome
    if not dry and con is not None:
        await _deliver_pending(con, client, channel_id, active_sender,
                               clock_fn, shutdown, logger, outcome)
    items_by_query: dict[str, list[parse.ItemRecord]] = {}
    seen_queries: set[str] = set()
    unique_rows: list[SearchRow] = []
    for row in rows:  # CR22: each search query is fetched once per pass
        if row["search_query"] in seen_queries:
            logger.info(
                "duplicate CSV row for query %r: claimed in row order, fetched once",
                row["search_query"],
            )
            continue
        seen_queries.add(row["search_query"])
        unique_rows.append(row)
    if scan_sleep_seconds > 0:
        if await _cancellable_wait(scan_sleep_seconds, shutdown):
            outcome["cancelled"] = True
            logger.info("scan cancelled during scan sleep; draining")
            return outcome
    for row in unique_rows:
        if shutdown.cancel_requested:
            outcome["cancelled"] = True
            break
        query = row["search_query"]
        html: str | None = None
        try:
            if fixture_dir is not None:
                with open(
                    os.path.join(fixture_dir, manifest[query]),
                    encoding="utf-8",
                ) as handle:
                    html = handle.read()
            else:
                assert session is not None  # live scan: lifecycle provides one
                html = await acquire.fetch_search_page_async(
                    session,
                    query,
                    deadline_monotonic=monotonic() + SCAN_BUDGET_SECONDS,
                    cancel_event=shutdown.cancel_event,
                )
        except acquire.AcquireTransient as exc:
            if "cancelled" in str(exc):
                outcome["cancelled"] = True
                logger.info(
                    "search %r cancelled mid-fetch; draining (monitoring continues)",
                    query,
                )
                break
            outcome["failed_searches"] += 1
            logger.error(
                "transient scan failure for %r (monitoring continues): %s",
                query,
                exc,
            )
            continue
        except acquire.AcquirePermanent as exc:
            outcome["failed_searches"] += 1
            logger.error(
                "permanent scan failure for %r (monitoring continues): %s",
                query,
                exc,
            )
            continue
        assert html is not None
        page: ParsedPage = parse.parse_search_page(html)  # single pass (CR22)
        for card in page["rejected"]:
            logger.info(
                "rejected card for %r: %s at position %d",
                query,
                card["reason"],
                card["position"],
            )
        layout = page["layout"]
        if layout == "denied":
            outcome["denied"] += 1
            logger.warning(
                "search page for %r looks like a deny/challenge page; skipped",
                query,
            )
            continue
        if layout == "suspicious":
            outcome["suspicious"] += 1
            logger.warning(
                "search page for %r has neither cards nor known markers"
                " (result count %s); not treated as a clean empty scan",
                query,
                page["result_count"],
            )
            continue
        if layout == "no-results":
            outcome["empty"] += 1
        items_by_query[query] = page["items"]
    excluded: set[str] = set()
    if con is not None:
        excluded |= set(state.sent_ids(con))
        excluded |= set(state.pending_ids(con))
    alerts = selection.choose_alerts(unique_rows, items_by_query, excluded)
    for alert in alerts:
        if shutdown.cancel_requested:
            outcome["cancelled"] = True
            break
        try:
            message = notify.render_alert_message(alert)
        except ValueError as exc:
            logger.error("alert render refused (id %s): %s", alert["id"], exc)
            continue
        assert con is not None  # non-dry: open_state guarantees writable state
        state.add_pending(con, alert, message)
        outcome["pending_added"] += 1
    if not dry and con is not None and not outcome["cancelled"]:
        await _deliver_pending(con, client, channel_id, active_sender,
                               clock_fn, shutdown, logger, outcome)
    return outcome


async def _deliver_dry(
    con: sqlite3.Connection | None,
    rows: list[SearchRow],
    fixture_dir: str | None,
    manifest: dict[str, str],
    session: aiohttp.ClientSession | None,
    monotonic: Callable[[], float],
    shutdown: ShutdownState,
    logger: logging.Logger,
    outcome: PassOutcome,
) -> None:
    """Dry run: fetch, parse, select; print identities; never write, never send."""
    items_by_query: dict[str, list[parse.ItemRecord]] = {}
    seen_queries: set[str] = set()
    unique_rows: list[SearchRow] = []
    for row in rows:
        if row["search_query"] in seen_queries:
            continue
        seen_queries.add(row["search_query"])
        unique_rows.append(row)
    for row in unique_rows:
        if shutdown.cancel_requested:
            outcome["cancelled"] = True
            break
        query = row["search_query"]
        try:
            if fixture_dir is not None:
                with open(
                    os.path.join(fixture_dir, manifest[query]),
                    encoding="utf-8",
                ) as handle:
                    html = handle.read()
            else:
                assert session is not None
                html = await acquire.fetch_search_page_async(
                    session,
                    query,
                    deadline_monotonic=monotonic() + SCAN_BUDGET_SECONDS,
                    cancel_event=shutdown.cancel_event,
                )
        except acquire.AcquireTransient as exc:
            outcome["failed_searches"] += 1
            logger.error("transient scan failure for %r: %s", query, exc)
            continue
        except acquire.AcquirePermanent as exc:
            outcome["failed_searches"] += 1
            logger.error("permanent scan failure for %r: %s", query, exc)
            continue
        page = parse.parse_search_page(html)
        items_by_query[query] = page["items"]
    excluded: set[str] = set()
    if con is not None:
        excluded |= set(state.sent_ids(con))
        excluded |= set(state.pending_ids(con))
    for alert in selection.choose_alerts(unique_rows, items_by_query, excluded):
        try:
            notify.render_alert_message(alert)
        except ValueError as exc:
            logger.error("alert render refused (id %s): %s", alert["id"], exc)
            continue
        print(
            f"DRY-RUN ALERT: id={alert['id']} "
            + f"price_usd={alert['price_usd']} query={alert['search_query']}",
            flush=True,
        )
    logger.info("dry run: state DB opened read-only; no writes, no sends")


async def _deliver_pending(
    con: sqlite3.Connection,
    client: discord.Client | None,
    channel_id: int | None,
    sender: Callable[[str], Awaitable[None]] | None,
    clock_fn: Callable[[], datetime],
    shutdown: ShutdownState,
    logger: logging.Logger,
    outcome: PassOutcome,
) -> None:
    """Send every stored pending alert at-least-once; mark sent after success.

    Quiet-window state is re-evaluated with the live clock before every
    single send (CR10), so a window opening mid-delivery defers the
    rest. A failed send keeps the row pending (at-least-once).
    """
    active = sender
    if client is not None:
        assert channel_id is not None
        try:
            channel = await client.fetch_channel(channel_id)
        except (
            discord.errors.Forbidden,
            discord.errors.NotFound,
            discord.errors.HTTPException,
        ) as exc:
            logger.error(
                "channel %d not fetchable right now (%s); alerts stay"
                " pending, next pass retries",
                channel_id,
                exc,
            )
            return  # fetch failure: alerts stay pending; next pass retries
        if not hasattr(channel, "send"):
            logger.error(
                "channel %d is not messageable (e.g. voice); alerts stay pending",
                channel_id,
            )
            return
        active = deliver.make_discord_deliverer(cast("Messageable", channel))
    if active is None:
        logger.error("no delivery transport available; alerts stay pending")
        return
    pending_rows = state.pending_alerts(con)
    if not pending_rows:
        return
    attempted = 0
    for pending in pending_rows:
        if shutdown.cancel_requested:
            outcome["cancelled"] = True
            break
        # CR10: the delivery-boundary clock decides quiet status per send.
        if clock.is_quiet(clock_fn()):
            outcome["deferred_quiet"] += 1
            if attempted == 0:
                logger.info(
                    "quiet hours (UTC 02:00-12:00): zero delivery attempts;"
                    " pending alerts stay pending",
                )
            continue
        attempted += 1
        message = pending["message"]
        if len(message) > notify.MAX_MESSAGE_CHARS:  # legacy oversized row (CR08)
            recovered = notify.recover_oversized(message)
            if recovered is None:
                logger.error(
                    "stored alert for %s is oversized and unrecoverable;"
                    " staying pending — operator: review and drop the row",
                    pending["id"],
                )
                continue
            message = recovered
            state.repair_pending(con, pending["id"], message)
        try:
            await active(message)
        except (deliver.DeliverFailed, discord.errors.HTTPException) as exc:
            outcome["send_failures"] += 1
            logger.error(
                "delivery attempt failed for %s; alert stays pending: %s",
                pending["id"],
                exc,
            )
            continue
        state.mark_sent(con, pending["id"])
        outcome["sent"] += 1
        logger.info("alert delivered and marked sent: %s", pending["id"])
    if outcome["deferred_quiet"] > 0 and attempted == 0:
        logger.info(
            "quiet hours (UTC 02:00-12:00): %d pending deferred,"
            " zero delivery attempts",
            len(pending_rows),
        )


async def lifecycle(
    *,
    dry: bool,
    fixture_dir: str | None,
    once: bool,
    rows: list[SearchRow],
    con: sqlite3.Connection | None,
    env: EnvValues | None,
    manifest: dict[str, str] | None,
    stub_fail_after: int | None,
    stub_block_seconds: float,
    scan_sleep_seconds: float,
    clock_fn: Callable[[], datetime],
    shutdown: ShutdownState,
    logger: logging.Logger,
) -> int:
    """Supervised gateway + scan loop on one event loop; returns exit code."""
    shutdown.loop = asyncio.get_running_loop()
    client: discord.Client | None = None
    session: aiohttp.ClientSession | None = None
    gateway_task: asyncio.Task[None] | None = None
    sender: Callable[[str], Awaitable[None]] | None = None
    channel_id: int | None = None
    exit_code = 0
    try:
        if dry:
            if fixture_dir is None:
                session = aiohttp.ClientSession()  # dry-live scans still fetch
        elif fixture_dir:
            stub = deliver.StubTransport(
                fail_after=stub_fail_after, block_seconds=stub_block_seconds
            )
            sender = stub.deliver
            logger.info(
                "offline stub transport: deliveries are printed, never sent to Discord"
            )
        else:
            assert env is not None  # live run: main loaded env before login
            client = discord.Client(intents=discord.Intents.default())
            try:
                await client.login(env["discord_token"])
            except discord.LoginFailure as exc:
                raise LoginRejected(str(exc)) from exc
            # CR01: gateway as a supervised background task with reconnect;
            # a live gateway must never block the scan loop.
            gateway_task = asyncio.create_task(client.connect(reconnect=True))
            try:
                await asyncio.wait_for(
                    client.wait_until_ready(), READY_TIMEOUT_SECONDS
                )
            except TimeoutError as exc:
                raise RuntimeError(
                    f"Discord gateway did not become ready within"
                    f" {READY_TIMEOUT_SECONDS:.0f}s (network/firewall?)"
                ) from exc
            if gateway_task.done():  # connect crashed during readiness
                raise RuntimeError(
                    f"Discord gateway startup failed: {gateway_task.exception()}"
                )
            channel_id = env["channel_id"]
            logger.info(
                "Discord gateway connected (supervised reconnect); monitoring"
                " channel %d",
                channel_id,
            )
            session = aiohttp.ClientSession()
        manifest_map: dict[str, str] = manifest or {}
        failed_searches = 0
        send_failures = 0
        while True:
            if shutdown.cancel_requested:
                logger.warning("cancellation requested; leaving scan loop")
                break
            outcome = await scan_pass(
                rows=rows,
                con=con,
                fixture_dir=fixture_dir,
                manifest=manifest_map,
                session=session,
                client=client,
                sender=sender,
                channel_id=channel_id,
                dry=dry,
                clock_fn=clock_fn,
                monotonic=time.monotonic,
                scan_sleep_seconds=scan_sleep_seconds,
                shutdown=shutdown,
                logger=logger,
            )
            failed_searches += outcome["failed_searches"]
            send_failures += outcome["send_failures"]
            if once:
                break
            finish = clock_fn()
            delay = SCAN_INTERVAL_SECONDS - (clock_fn() - finish).total_seconds()
            if delay > 0 and await _cancellable_wait(delay, shutdown):
                logger.warning("cancellation requested during interval; draining")
                break
        if once and not shutdown.cancel_requested:
            if failed_searches > 0 or send_failures > 0:
                logger.error(
                    "required scan work failed this run: %d failed searches,"
                    " %d send failures",
                    failed_searches,
                    send_failures,
                )
                exit_code = 5
    finally:
        # CR03: bounded cleanup of every owned resource, max 10 s.
        cleanup_coro = _close_resources(
            gateway_task, client, session, logger
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(cleanup_coro), SHUTDOWN_DRAIN_SECONDS
            )
        except TimeoutError:
            logger.error(
                "cleanup exceeded %.0fs drain; abandoning graceful close",
                SHUTDOWN_DRAIN_SECONDS,
            )
        except Exception:
            logger.error("cleanup failed:", exc_info=True)
    if shutdown.cancel_requested:
        logger.info("shutdown drain complete; pending alerts stay durable")
    return exit_code


async def _close_resources(
    gateway_task: "asyncio.Task[None] | None",
    client: "discord.Client | None",
    session: "aiohttp.ClientSession | None",
    logger: logging.Logger,
) -> None:
    """Close gateway task, Discord client, HTTP session in order."""
    if client is not None:
        try:
            await asyncio.wait_for(client.close(), 5.0)
        except TimeoutError:
            logger.warning("Discord client close timed out; cancelling gateway")
    if gateway_task is not None:
        gateway_task.cancel()
        try:
            await gateway_task
        except (asyncio.CancelledError, Exception):
            pass
    if session is not None and not session.closed:
        await session.close()


def main(argv: list[str] | None = None) -> int:
    """Validate config, take the flock lock, run the async lifecycle."""
    parser = argparse.ArgumentParser(
        description="Scan eBay searches hourly and alert one Discord channel."
    )
    _ = parser.add_argument(
        "--once",
        action="store_true",
        help="run a single scan and exit (for cron/manual runs)",
    )
    _ = parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print alert identities; never send and never write state",
    )
    _ = parser.add_argument(
        "--fixture-dir",
        metavar="DIR",
        help="offline fixture dir with manifest.json (never contacts eBay)",
    )
    _ = parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help="state root: state.sqlite, bot.lock, and logs/ live under it",
    )
    _ = parser.add_argument(
        "--at",
        metavar="ISO-UTC",
        help="deterministic scan clock (e.g. 2026-10-19T13:00:00Z)",
    )
    _ = parser.add_argument(
        "--env-file",
        help="optional .env file; process environment values win",
    )
    _ = parser.add_argument("--csv", default=DEFAULT_CSV, help="search CSV path")
    args = parser.parse_args(argv)

    dry_run = cast("bool", args.dry_run)
    once = cast("bool", args.once)
    csv_path = cast("str", args.csv)
    data_dir = cast("str", args.data_dir)
    env_file = cast("str | None", args.env_file)
    at_raw = cast("str | None", args.at)
    fixture_dir = cast("str | None", args.fixture_dir)

    shutdown = ShutdownState()
    env: EnvValues | None = None
    start_at: datetime | None = None
    manifest: dict[str, str] | None = None
    con: sqlite3.Connection | None = None
    lock_fd: int | None = None
    stub_fail_after: int | None = None
    stub_block_seconds = 0.0
    scan_sleep_seconds = 0.0
    logger: logging.Logger | None = None
    try:
        logger = setup_logging(data_dir)
        rows = config.load_search_rows(csv_path)
        if at_raw:
            start_at = parse_at(at_raw)
            if not dry_run and not fixture_dir:
                if os.environ.get(ALLOW_AT_CLOCK_ENV) != "1":
                    raise config.ConfigError(
                        "--at on a live delivery run requires"
                        f" {ALLOW_AT_CLOCK_ENV}=1 (offline testing guard; live"
                        " runs use the real clock)"
                    )
        if fixture_dir:
            if not dry_run and data_dir == DEFAULT_DATA_DIR:
                raise config.ConfigError(
                    "fixture runs must not write the default data dir (the"
                    " real state root); pass an explicit --data-dir"
                )
            manifest = load_fixture_manifest(fixture_dir, rows)
            raw_fail = os.environ.get(STUB_FAIL_AFTER_ENV)
            if raw_fail is not None:
                stub_fail_after = _validate_env_int(raw_fail, STUB_FAIL_AFTER_ENV)
            raw_block = os.environ.get(STUB_BLOCK_SECONDS_ENV)
            if raw_block is not None:
                stub_block_seconds = _validate_env_seconds(
                    raw_block, STUB_BLOCK_SECONDS_ENV
                )
            raw_scan = os.environ.get(SCAN_SLEEP_SECONDS_ENV)
            if raw_scan is not None:
                scan_sleep_seconds = _validate_env_seconds(
                    raw_scan, SCAN_SLEEP_SECONDS_ENV
                )
        else:
            for name in (STUB_FAIL_AFTER_ENV, STUB_BLOCK_SECONDS_ENV,
                         SCAN_SLEEP_SECONDS_ENV):
                if os.environ.get(name) is not None:
                    raise config.ConfigError(
                        f"{name} only applies to --fixture-dir offline runs"
                    )
        # CR15: any non-fixture run needs real network access; dry-run
        # included, since --dry-live scans still fetch pages.
        if fixture_dir is None and os.environ.get(DISABLE_NETWORK_ENV) == "1":
            raise config.ConfigError(
                "network disabled for this run; non-fixture scans (dry-run"
                " included) need live eBay access (use --fixture-dir offline)"
            )
        if not dry_run and not fixture_dir:
            env = config.load_env(env_file)
        # CR04: kernel lock BEFORE any state work, so a refused
        # second process can never read, write, or migrate state.
        if not dry_run:
            lock_fd = state.acquire_instance_lock(data_dir)
            if lock_fd is None:
                message = (
                    f"config error: another instance holds the lock in {data_dir}"
                )
                print(message, file=sys.stderr)
                logger.critical(
                    "another instance holds the lock in %s: refusing to start"
                    " a second scanner",
                    data_dir,
                )
                return 3
        if dry_run:
            con = open_state_readonly(data_dir)
        else:
            _ = config.ensure_writable_dir(data_dir)
            legacy = None if fixture_dir else LEGACY_SKIP
            con, imported = state.open_state(data_dir, legacy)
            if imported:
                logger.info("imported into state DB: %d ids", imported)
    except (config.ConfigError, state.StateError) as exc:
        # CR13: actionable startup failure, never a traceback.
        print(f"config error: {exc}", file=sys.stderr)
        if logger is not None:
            logger.critical("startup validation failed: %s", exc)
        return 2
    clock_fn: Callable[[], datetime] = (
        (lambda: start_at) if start_at else clock.utc_now
    )

    def _sigterm_handler(signum: int, frame: object) -> None:
        del signum, frame
        shutdown.request()

    old_sigterm = signal.signal(signal.SIGTERM, _sigterm_handler)
    exit_code = 0
    try:
        exit_code = asyncio.run(
            lifecycle(
                dry=dry_run,
                fixture_dir=fixture_dir,
                once=once,
                rows=rows,
                con=con,
                env=env,
                manifest=manifest,
                stub_fail_after=stub_fail_after,
                stub_block_seconds=stub_block_seconds,
                scan_sleep_seconds=scan_sleep_seconds,
                clock_fn=clock_fn,
                shutdown=shutdown,
                logger=logger,
            )
        )
    except LoginRejected as exc:
        logger.critical("Discord login rejected: %s", exc)
        exit_code = 4
    except KeyboardInterrupt:
        logger.warning("interrupted by SIGINT; draining")
        exit_code = 0
    except Exception:  # last-resort: log, never swallow silently
        logger.critical("uncaught failure:", exc_info=True)
        exit_code = 1
    finally:
        _ = signal.signal(signal.SIGTERM, old_sigterm)
        if con is not None:
            con.close()
        if lock_fd is not None:
            state.release_instance_lock(lock_fd)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
