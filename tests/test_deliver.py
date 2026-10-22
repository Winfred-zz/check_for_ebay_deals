"""Delivery boundary behavior (CR20/CR10/CR08): scan_pass at the real seams.

Every test drives scan_pass (the production scan body) with injected
transports and clocks: sent marks only appear after a successful send;
a crash between send and mark-sent replays on reopen (at-least-once,
never forget_sent); channel-fetch failures are recoverable; oversized
stored rows self-repair; quiet status is re-evaluated per send.
"""

import asyncio
import logging
import re
import types
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, cast

import discord
import pytest

import config
import deliver
import notify
import state
from check_for_ebay_deals import ShutdownState, load_fixture_manifest, scan_pass

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "offline-scan"
SMOKE_CSV = FIXTURE_DIR / "smoke.csv"
OPEN = datetime(2026, 10, 19, 13, 0, 0, tzinfo=timezone.utc)
QUIET = datetime(2026, 10, 19, 5, 0, 0, tzinfo=timezone.utc)
ALERT_IDS = [
    "167222222222",
    "33444555566",
    "111000222000",
    "222000222000",
]
URL_TAIL_RE = re.compile(r"\]\(https://www\.ebay\.com/itm/([0-9]+)\)")


def make_logger() -> logging.Logger:
    logger = logging.getLogger("test_deliver")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def delivered_ids(messages: list[str]) -> list[str]:
    """Ids in send order, read back from the rendered URLs."""
    ids = [URL_TAIL_RE.search(message) for message in messages]
    return [cast("str", hit.group(1)) for hit in ids if hit is not None]


def seed_pending(con: state.sqlite3.Connection, item_id: str, message: str,
                 price: str) -> None:
    alert: notify.AlertRecord = {
        "id": item_id,
        "price_usd": Decimal(price),
        "link": f"https://www.ebay.com/itm/{item_id}",
        "friendly_name": "seed",
        "search_query": "empty search",
    }
    state.add_pending(con, alert, message)


def run_scan(rows, con, sender, clock_fn, logger, manifest, fixture_dir):
    return asyncio.run(
        scan_pass(
            rows=rows,
            con=con,
            fixture_dir=fixture_dir,
            manifest=manifest,
            browse_client=None,
            client=None,
            sender=sender,
            channel_id=None,
            dry=False,
            clock_fn=clock_fn,
            monotonic=lambda: 0.0,
            scan_sleep_seconds=0.0,
            shutdown=ShutdownState(),
            logger=logger,
        )
    )


def step_clock(values: list[datetime]) -> "Callable[[], datetime]":
    """Clock returning values in order; repeats the final value forever."""

    def tick() -> datetime:
        if len(values) > 1:
            return values.pop(0)
        return values[0]

    return tick


class RecordingSender:
    def __init__(self, fail_after: int | None = None) -> None:
        self.messages: list[str] = []
        self.fail_after = fail_after

    async def __call__(self, message: str) -> None:
        self.messages.append(message)
        if self.fail_after is not None and len(self.messages) > self.fail_after:
            raise deliver.DeliverFailed("stub scripted failure")


def rows_and_manifest(csv_path=SMOKE_CSV, fixture_dir=FIXTURE_DIR):
    rows = config.load_search_rows(str(csv_path))
    manifest = load_fixture_manifest(str(fixture_dir), rows)
    return rows, manifest


def test_partial_send_partition_marks_only_delivered_ids(tmp_path):
    data = tmp_path / "data"
    con, _ = state.open_state(str(data), None)
    rows, manifest = rows_and_manifest()
    sender = RecordingSender(fail_after=2)
    outcome = run_scan(rows, con, sender, step_clock([OPEN]),
                       make_logger(), manifest, str(FIXTURE_DIR))
    assert outcome["sent"] == 2
    assert outcome["send_failures"] == 2
    con.close()
    con2, _ = state.open_state(str(data), None)
    delivered = delivered_ids(sender.messages)
    assert len(delivered) == 4
    # Only the ids actually sent left pending; the rest stay durable.
    assert set(state.sent_ids(con2)) == set(delivered[:2])
    assert set(state.pending_ids(con2)) == set(delivered[2:])
    con2.close()


def test_crash_between_send_and_mark_sent_replays_all(tmp_path, monkeypatch):
    data = tmp_path / "data"
    con, _ = state.open_state(str(data), None)
    rows, manifest = rows_and_manifest()

    def crashy_mark_sent(con_arg, item_id):
        del con_arg, item_id
        raise state.StateError("simulated crash before mark-sent")

    monkeypatch.setattr(state, "mark_sent", crashy_mark_sent)
    with pytest.raises(state.StateError, match="simulated crash"):
        run_scan(rows, con, RecordingSender(), step_clock([OPEN]),
                 make_logger(), manifest, str(FIXTURE_DIR))
    monkeypatch.undo()
    con.close()  # process death: the mark-sent never reached disk
    con2, _ = state.open_state(str(data), None)
    # Nothing was marked sent; every alert is durable pending.
    assert not state.sent_ids(con2)
    assert set(state.pending_ids(con2)) == set(ALERT_IDS)
    clean = RecordingSender()
    outcome = run_scan(rows, con2, clean, step_clock([OPEN]),
                       make_logger(), manifest, str(FIXTURE_DIR))
    assert outcome["sent"] >= len(ALERT_IDS)
    replayed = delivered_ids(clean.messages[: len(ALERT_IDS)])
    assert set(replayed) == set(ALERT_IDS)  # at-least-once: all replayed
    assert set(state.sent_ids(con2)) >= set(ALERT_IDS)
    assert not state.pending_ids(con2)
    con2.close()


def test_channel_fetch_http_failure_is_recoverable(tmp_path):
    data = tmp_path / "data"
    con, _ = state.open_state(str(data), None)
    seed_pending(con, "123123123123",
                 "[Found seed for $5.00](https://www.ebay.com/itm/123123123123)",
                 "5.00")

    response = types.SimpleNamespace(status=500, reason="Server Error")

    class BrokenChannelClient:
        async def fetch_channel(self, channel_id):
            del channel_id
            raise discord.errors.HTTPException(response, "boom")

    rows, manifest = rows_and_manifest()
    outcome = asyncio.run(
        scan_pass(
            rows=rows,
            con=con,
            fixture_dir=str(FIXTURE_DIR),
            manifest=manifest,
            browse_client=None,
            client=BrokenChannelClient(),
            sender=None,
            channel_id=123,
            dry=False,
            clock_fn=step_clock([OPEN]),
            monotonic=lambda: 0.0,
            scan_sleep_seconds=0.0,
            shutdown=ShutdownState(),
            logger=make_logger(),
        )
    )
    assert outcome["sent"] == 0
    assert "123123123123" in state.pending_ids(con)
    assert not state.sent_ids(con)


def test_oversized_rows_repair_or_refuse_never_clobber(tmp_path):
    data = tmp_path / "data"
    con, _ = state.open_state(str(data), None)
    huge = f"[Found {'x' * 3000} for $19.50]" \
           "(https://www.ebay.com/itm/111222333444)"
    garbage = "Z" * 2500
    seed_pending(con, "111222333444", huge, "19.50")
    seed_pending(con, "999000111222", garbage, "7.00")
    rows, manifest = rows_and_manifest()
    sender = RecordingSender()
    outcome = run_scan(rows, con, sender, step_clock([OPEN]),
                       make_logger(), manifest, str(FIXTURE_DIR))
    # The repaired seed row (pending-first) plus the four fresh alerts.
    assert outcome["sent"] == 1 + len(ALERT_IDS)
    sent_message = sender.messages[0]
    assert len(sent_message) <= notify.MAX_MESSAGE_CHARS
    # Trimmed name, URL byte-intact at the tail.
    assert sent_message.endswith("](https://www.ebay.com/itm/111222333444)")
    assert ".. for $19.50]" in sent_message
    assert "111222333444" in state.sent_ids(con)
    # Unrecoverable row: stays pending, byte-identical, operator-flagged.
    assert "999000111222" in state.pending_ids(con)
    stored = state.pending_alerts(con)
    assert [row["message"] for row in stored] == [garbage]


def test_quiet_status_is_reevaluated_per_send(tmp_path):
    data = tmp_path / "data"
    con, _ = state.open_state(str(data), None)
    for n in (1, 2, 3):
        item = f"11111111111{n}"
        seed_pending(
            con, item,
            f"[Found seed{n} for $5.00](https://www.ebay.com/itm/{item})",
            "5.00",
        )
    rows, manifest = rows_and_manifest()
    sender = RecordingSender()
    # Clock flips quiet->open mid-delivery: the deferred row must be sent
    # later in the same pass, not skipped for the whole run.
    outcome = run_scan(rows, con, sender, step_clock([QUIET, OPEN]),
                       make_logger(), manifest, str(FIXTURE_DIR))
    assert outcome["deferred_quiet"] >= 1
    assert outcome["sent"] >= 2
    assert len(sender.messages) == outcome["sent"]
