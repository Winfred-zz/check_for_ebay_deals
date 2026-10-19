"""Hourly eBay deal scanner with durable alerts and a bounded shutdown.

Approved lifecycle (design.md + handoff §4):
- Every config is validated BEFORE any scan or send; config errors exit
  2 with an actionable message. The Discord token value is never echoed.
- Logging is set up once, here: console INFO + rotating file DEBUG
  (logs/bot.log, 1 MiB x 5 backups - the old code's "2 MB" claim was
  wrong, F23). Uncaught failures log CRITICAL with traceback.
- One instance at a time: a lockfile in the data dir; a second process
  refuses to start.
- Hourly scans, finish-to-start, never overlapping. Quiet hours (UTC
  02:00-12:00) suppress DELIVERY only: scans run and new alerts are
  stored pending; the window-open scan delivers all pending.
- At-least-once delivery: pending rows are written before the send
  attempt, mark sent only after a successful send. A crash between
  send and mark-sent can duplicate within a small window (approved);
  alerts are never lost.
- SIGTERM raises Shutdown inside the running code; the drain is bounded
  (~10 s worst case), releases the instance lock, and pending alerts
  are already durable.

Offline modes: --fixture-dir + manifest.json replaces network fetches
with local HTML; --dry-run prints alert identities, opens state only
read-only, and never touches Discord. With
CHECK_FOR_EBAY_DEALS_DISABLE_NETWORK=1 set, a production (non-fixture)
scan refuses to start, so an accidental network call fails loudly.
"""

from collections.abc import Awaitable, Callable

from typing import cast

from config import EnvValues, SearchRow

from parse import ItemRecord
import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sqlite3
import sys
from datetime import datetime, timezone

import colorlog
import discord
from discord.abc import Messageable
import requests

import acquire
import clock
import config
import deliver
import notify
import parse
import selection
import state

DEFAULT_CSV = os.path.join("configs", "ebay_deals_to_check.csv")
DEFAULT_DATA_DIR = "data"
LEGACY_SKIP = os.path.join("configs", state.LEGACY_SKIP_FILE)
STUB_FAIL_AFTER_ENV = "CHECK_FOR_EBAY_DEALS_STUB_FAIL_AFTER"
DISABLE_NETWORK_ENV = "CHECK_FOR_EBAY_DEALS_DISABLE_NETWORK"
SCAN_INTERVAL_SECONDS = 3600
LOG_DIR = "logs"

cancel_requested = False


class Shutdown(BaseException):
    """Raised from the SIGTERM handler; unwinds into a bounded drain."""


class LoginRejected(BaseException):
    """Discord login rejected: exit 4, the token is never echoed."""


def setup_logging() -> logging.Logger:
    """Console (colorlog, INFO) + rotating file (DEBUG); set up once."""
    logger = logging.getLogger("check_for_ebay_deals")
    logger.setLevel(logging.DEBUG)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
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
            os.path.join(LOG_DIR, "bot.log"),
            maxBytes=1_000_000,
            backupCount=5,
        )
    except OSError as exc:
        raise config.ConfigError(f"log file unusable: {exc}") from exc
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(console)
    logger.addHandler(file_handler)
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
    """Dry runs open the DB read-only; absent DB means empty state."""
    db_path = os.path.join(data_dir, state.STATE_DB)
    if not os.path.isfile(db_path):
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise state.StateError(f"dry run state DB unreadable: {exc}") from exc
    try:
        stored: list[tuple[int]] = con.execute(
            "SELECT version FROM schema"
        ).fetchall()
    except sqlite3.Error as exc:
        con.close()
        raise state.StateError(
            f"dry run state DB partial or missing tables: {exc}"
        ) from exc
    if [row[0] for row in stored] != [state.SCHEMA_VERSION]:
        con.close()
        raise state.StateError(
            "dry run state DB corrupt or partial: schema row mismatch"
        )
    return con


def load_fixture_manifest(fixture_dir: str, rows: list[SearchRow]) -> dict[str, str]:
    """Validate manifest.json (query -> fixture html) against CSV rows."""
    path = os.path.join(fixture_dir, "manifest.json")
    if not os.path.isfile(path):
        raise config.ConfigError(f"fixture dir has no manifest.json at {path}")
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise config.ConfigError("fixture manifest must map query to html file")
    manifest: dict[str, str] = cast("dict[str, str]", raw)
    missing = sorted(
        row["search_query"] for row in rows if row["search_query"] not in manifest
    )
    if missing:
        raise config.ConfigError(
            "fixture manifest lacks searches: " + ", ".join(missing)
        )
    return manifest



async def scan_pass(
    rows: list[SearchRow],
    con: sqlite3.Connection | None,
    fixture_dir: str | None,
    manifest: dict[str, str],
    session: requests.Session | None,
    client: discord.Client | None,
    sender: Callable[[str], Awaitable[None]] | None,
    channel_id: int | None,
    dry: bool,
    now: datetime,
    logger: logging.Logger,
) -> None:
    """One scan: acquire (fixture or live), parse, select, persist, deliver."""
    items_by_query: dict[str, list[ItemRecord]] = {}
    for row in rows:
        query = row["search_query"]
        try:
            if fixture_dir is not None:
                with open(
                    os.path.join(fixture_dir, manifest[query]),
                    encoding="utf-8",
                ) as handle:
                    html = handle.read()
            else:
                assert session is not None  # live scan: async_main provides one
                html = acquire.fetch_search_page(
                    session,
                    query,
                    cancel_check=lambda: cancel_requested,
                )
        except acquire.AcquireTransient as exc:
            logger.error(
                "transient scan failure for %r (monitoring continues): %s",
                query,
                exc,
            )
            continue
        except acquire.AcquirePermanent as exc:
            logger.error(
                "permanent scan failure for %r (monitoring continues): %s",
                query,
                exc,
            )
            continue
        layout = acquire.layout_status(html)
        if layout == "denied":
            logger.warning(
                "search page for %r looks like a deny/challenge page; skipped",
                query,
            )
            continue
        if layout == "suspicious":
            logger.warning(
                "search page for %r has neither cards nor known markers;"
                + " not treated as a clean empty scan",
                query,
            )
            continue
        items, rejected = parse.parse_search_html(html)
        for card in rejected:
            logger.info(
                "rejected card for %r: %s at position %d",
                query,
                card["reason"],
                card["position"],
            )
        items_by_query[query] = items
    excluded: set[str] = set()
    if con is not None:
        excluded |= set(state.sent_ids(con))
        excluded |= {pending["id"] for pending in state.pending_alerts(con)}
    alerts = selection.choose_alerts(rows, items_by_query, excluded)
    for alert in alerts:
        try:
            message = notify.render_alert_message(alert)
        except ValueError as exc:
            logger.error("alert render refused (id %s): %s", alert["id"], exc)
            continue
        if dry:
            print(
                f"DRY-RUN ALERT: id={alert['id']} "
                + f"price_usd={alert['price_usd']} query={alert['search_query']}",
                flush=True,
            )
            continue
        assert con is not None  # non-dry scan: main provides a writable DB
        state.add_pending(con, alert, message)
    if dry:
        logger.info("dry run: state DB opened read-only; no writes, no sends")
        return
    assert con is not None  # non-dry: open_state guarantees writable state
    quiet = clock.is_quiet(now)
    if quiet:
        logger.info(
            "quiet hours (UTC 02:00-12:00): %d new pending stored,"
            + " zero delivery attempts",
            len(alerts),
        )
        return
    if client is not None:
        assert channel_id is not None
        try:
            channel = await client.fetch_channel(channel_id)
        except (discord.errors.Forbidden, discord.errors.NotFound):
            channel = None
        if channel is None:
            logger.error(
                "channel %d not fetchable: permission state error;"
                + " pending alerts stay pending",
                channel_id,
            )
            return
        if not hasattr(channel, "send"):
            logger.error(
                "channel %d is not messageable (e.g. voice); alerts stay"
                + " pending",
                channel_id,
            )
            return
        sender = deliver.make_discord_deliverer(cast("Messageable", channel))
    assert sender is not None
    for pending in state.pending_alerts(con):
        try:
            await sender(pending["message"])
        except (deliver.DeliverFailed, discord.errors.HTTPException) as exc:
            logger.error(
                "delivery attempt failed for %s; alert stays pending: %s",
                pending["id"],
                exc,
            )
            continue
        state.mark_sent(con, pending["id"])
        logger.info("alert delivered and marked sent: %s", pending["id"])


async def async_main(
    dry: bool,
    fixture_dir: str | None,
    once: bool,
    rows: list[SearchRow],
    con: sqlite3.Connection | None,
    env: EnvValues | None,
    manifest: dict[str, str] | None,
    start_at: datetime | None,
    logger: logging.Logger,
) -> None:
    """Login once (production path), then loop scans hourly, finish-to-start."""
    client: discord.Client | None = None
    session: requests.Session | None = None
    sender: Callable[[str], Awaitable[None]] | None = None
    channel_id: int | None = None
    if dry:
        sender = None
        if fixture_dir is None:
            session = requests.Session()  # dry-live scans still fetch pages
    elif fixture_dir:
        raw = os.environ.get(STUB_FAIL_AFTER_ENV)
        stub = deliver.StubTransport(fail_after=int(raw) if raw else None)
        sender = stub.deliver
    else:
        assert env is not None  # live run: main loaded env before login
        client = discord.Client(intents=discord.Intents.default())
        try:
            await client.login(env["discord_token"])
            await client.connect(reconnect=False)
            await client.wait_until_ready()
        except (discord.LoginFailure, discord.errors.HTTPException) as exc:
            raise LoginRejected(str(exc)) from exc
        channel_id = env["channel_id"]
        logger.info(
            "Discord login complete (banner); monitoring channel %d",
            channel_id,
        )
        session = requests.Session()
    first = True
    manifest_map: dict[str, str] = manifest or {}
    while True:
        if cancel_requested:
            logger.warning("cancellation requested; leaving scan loop")
            break
        now = start_at if first and start_at else clock.utc_now()
        first = False
        await scan_pass(
            rows,
            con,
            fixture_dir,
            manifest_map,
            session,
            client,
            sender,
            channel_id,
            dry,
            now,
            logger,
        )
        if once:
            break
        finish = clock.utc_now()
        delay = SCAN_INTERVAL_SECONDS - (clock.utc_now() - finish).total_seconds()
        while delay > 0 and not cancel_requested:
            await asyncio.sleep(min(delay, 60.0))
            delay = SCAN_INTERVAL_SECONDS - (clock.utc_now() - finish).total_seconds()
    if client is not None:
        await client.close()


def sigterm_handler(signum: int, frame: object) -> None:
    """Cancel the running scan; Shutdown unwinds through blocking calls."""
    global cancel_requested
    del signum, frame
    cancel_requested = True
    raise Shutdown()


def main(argv: list[str] | None = None) -> int:
    """Validate config, guard single instance, run the async lifecycle."""
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
        help="state dir holding state.sqlite and the instance lock",
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

    logger = setup_logging()
    env: EnvValues | None = None
    start_at: datetime | None = None
    manifest: dict[str, str] | None = None
    con: sqlite3.Connection | None = None
    lock_path: str | None = None
    try:
        rows = config.load_search_rows(csv_path)
        if at_raw:
            start_at = parse_at(at_raw)
        if fixture_dir:
            manifest = load_fixture_manifest(fixture_dir, rows)
        if not dry_run and not fixture_dir:
            if os.environ.get(DISABLE_NETWORK_ENV) == "1":
                raise config.ConfigError(
                    "network disabled for this run; production scans need live"
                    + " eBay access (use --fixture-dir offline)"
                )
            env = config.load_env(env_file)
        if dry_run:
            con = open_state_readonly(data_dir)
        else:
            _ = config.ensure_writable_dir(data_dir)
            con, backup = state.open_state(data_dir, LEGACY_SKIP)
            if backup:
                logger.info(
                    "legacy skip list migrated into state DB: %s", backup
                )
    except (config.ConfigError, state.StateError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        logger.critical("startup validation failed: %s", exc)
        return 2
    if not dry_run:
        lock_path = state.acquire_instance_lock(data_dir)
        if lock_path is None:
            logger.critical(
                "another instance holds the lock in %s: refusing to start a"
                + " second scanner",
                data_dir,
            )
            print(
                "config error: another instance holds the lock in "
                + f"{data_dir}",
                file=sys.stderr,
            )
            return 3
    _ = signal.signal(signal.SIGTERM, sigterm_handler)
    exit_code = 0
    try:
        asyncio.run(
            async_main(
                dry_run,
                fixture_dir,
                once,
                rows,
                con,
                env,
                manifest,
                start_at,
                logger,
            )
        )
    except Shutdown:
        logger.info("SIGTERM drain complete; pending alerts stay durable")
    except LoginRejected as exc:
        logger.critical("Discord login rejected: %s", exc)
        exit_code = 4
    except KeyboardInterrupt:
        logger.warning("interrupted by SIGINT")
    except Exception:  # last-resort: log, never swallow silently
        logger.critical("uncaught failure:", exc_info=True)
        exit_code = 1
    finally:
        if con is not None:
            con.close()
        if lock_path is not None:
            state.release_instance_lock(lock_path)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
