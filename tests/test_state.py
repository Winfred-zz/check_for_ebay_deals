"""Behavior tests for durable state: SQLite DB, legacy migration, lock."""

import os

import pytest

import sqlite3
import subprocess
from decimal import Decimal

import state


def _dead_pid() -> int:
    """A PID no live process owns."""
    for pid in range(2, 1000):
        if pid != os.getpid() and not os.path.exists(f"/proc/{pid}"):
            return pid
    raise AssertionError("no free PID found in probe range")


def test_fresh_state_db_is_queryable(tmp_path):
    con, backup = state.open_state(str(tmp_path), None)
    assert backup is None
    assert con.execute("SELECT version FROM schema").fetchone() == (
        state.SCHEMA_VERSION,
    )
    assert state.sent_ids(con) == frozenset()
    assert state.pending_alerts(con) == []
    con.close()


def test_pending_rows_round_trip(tmp_path):
    con, _ = state.open_state(str(tmp_path), None)
    alert = {
        "id": "167111111111",
        "price_usd": Decimal("24.74"),
        "link": "https://www.ebay.com/itm/167111111111",
    }
    message = "[Found test](https://www.ebay.com/itm/167111111111)"
    state.add_pending(con, alert, message)
    pending = state.pending_alerts(con)
    assert len(pending) == 1
    row = pending[0]
    assert row["id"] == "167111111111"
    assert row["message"] == message
    assert row["price_usd"] == Decimal("24.74")
    assert row["link"] == alert["link"]
    assert row["found_utc"]
    con.close()
    # Durability: reopening the DB file keeps the pending row.
    con2, _ = state.open_state(str(tmp_path), None)
    assert len(state.pending_alerts(con2)) == 1
    con2.close()


def test_mark_sent_moves_row(tmp_path):
    con, _ = state.open_state(str(tmp_path), None)
    alert = {"id": "999000111222", "price_usd": Decimal("1"), "link": "x"}
    state.add_pending(con, alert, "msg")
    state.mark_sent(con, "999000111222")
    assert state.pending_alerts(con) == []
    assert "999000111222" in state.sent_ids(con)
    con.close()


def test_drop_pending_keeps_sent_clean(tmp_path):
    con, _ = state.open_state(str(tmp_path), None)
    alert = {"id": "555111222333", "price_usd": Decimal("1"), "link": "x"}
    state.add_pending(con, alert, "msg")
    state.drop_pending(con, "555111222333")
    assert state.pending_alerts(con) == []
    assert "555111222333" not in state.sent_ids(con)
    con.close()


def test_crash_window_forgets_sent(tmp_path):
    con, _ = state.open_state(str(tmp_path), None)
    state.mark_sent(con, "777000111000")
    state.forget_sent(con, "777000111000")
    assert "777000111000" not in state.sent_ids(con)
    con.close()


def test_migration_moves_legacy_skip_ids(tmp_path):
    legacy = tmp_path / "skip_items.txt"
    legacy.write_text("111000222000\n222000111000\n\n", encoding="utf-8")
    con, backup = state.open_state(str(tmp_path), str(legacy))
    assert state.sent_ids(con) == frozenset(
        {"111000222000", "222000111000"}
    )
    assert backup is not None
    assert backup.startswith(str(tmp_path))
    assert ".migrated-" in backup and backup.endswith(".bak")
    with open(backup, encoding="utf-8") as handle:
        assert handle.read() == "111000222000\n222000111000\n\n"
    assert legacy.read_text(encoding="utf-8") == ""
    con.close()


def test_migrated_ids_survive_reopen(tmp_path):
    legacy = tmp_path / "skip_items.txt"
    legacy.write_text("333444555000\n", encoding="utf-8")
    con, _ = state.open_state(str(tmp_path), str(legacy))
    con.close()
    con2, backup2 = state.open_state(str(tmp_path), str(legacy))
    assert backup2 is None
    assert "333444555000" in state.sent_ids(con2)
    con2.close()


def test_malformed_legacy_ids_refused(tmp_path):
    legacy = tmp_path / "skip_items.txt"
    legacy.write_text("ok123\nbad id!\n", encoding="utf-8")
    with pytest.raises(state.StateError, match="malformed IDs"):
        state.open_state(str(tmp_path), str(legacy))


def test_empty_legacy_file_no_backup(tmp_path):
    legacy = tmp_path / "skip_items.txt"
    legacy.write_text("", encoding="utf-8")
    con, backup = state.open_state(str(tmp_path), str(legacy))
    assert backup is None
    con.close()


def test_empty_db_file_refused(tmp_path):
    db = tmp_path / "state.sqlite"
    db.write_bytes(b"")
    with pytest.raises(state.StateError, match="corrupt or unsupported"):
        state.open_state(str(tmp_path), None)


def test_schema_table_without_row_refused(tmp_path):
    db = tmp_path / "state.sqlite"
    seed = sqlite3.connect(db)
    with seed:
        seed.execute("CREATE TABLE schema (version INTEGER PRIMARY KEY)")
    seed.close()
    with pytest.raises(state.StateError, match="no schema version row"):
        state.open_state(str(tmp_path), None)


def test_corrupt_db_refused(tmp_path):
    db = tmp_path / "state.sqlite"
    db.write_bytes(b"\x00garbage\xff" * 500)
    with pytest.raises(state.StateError):
        state.open_state(str(tmp_path), None)


def test_newer_schema_refused(tmp_path):
    db = tmp_path / "state.sqlite"
    seed = sqlite3.connect(db)
    with seed:
        seed.execute("CREATE TABLE schema (version INTEGER PRIMARY KEY)")
        seed.execute("INSERT INTO schema (version) VALUES (2)")
    seed.close()
    with pytest.raises(state.StateError, match="newer than supported"):
        state.open_state(str(tmp_path), None)


def test_unreadable_legacy_refused(tmp_path):
    legacy = tmp_path / "skip_items.txt"
    legacy.write_bytes(b"\xff\xfe\x00bad bytes")
    with pytest.raises(state.StateError, match="legacy state unreadable"):
        state.open_state(str(tmp_path), str(legacy))


def test_lock_written_with_own_pid(tmp_path):
    path = state.acquire_instance_lock(str(tmp_path))
    assert path is not None
    assert path == str(tmp_path / "bot.lock")
    assert int(open(path, encoding="utf-8").read().strip()) == os.getpid()
    state.release_instance_lock(path)


def test_second_instance_refused(tmp_path):
    holder = subprocess.Popen(["sleep", "30"], stdin=None)
    try:
        lock = str(tmp_path / "bot.lock")
        with open(lock, "w", encoding="utf-8") as handle:
            handle.write(f"{holder.pid}\n")
        assert state.acquire_instance_lock(str(tmp_path)) is None
    finally:
        holder.kill()


def test_dead_pid_lock_reclaimed(tmp_path):
    lock = str(tmp_path / "bot.lock")
    with open(lock, "w", encoding="utf-8") as handle:
        handle.write(f"{_dead_pid()}\n")
    acquired = state.acquire_instance_lock(str(tmp_path))
    assert acquired == lock
    assert int(open(lock, encoding="utf-8").read().strip()) == os.getpid()
    state.release_instance_lock(lock)


def test_garbage_lock_reclaimed(tmp_path):
    lock = str(tmp_path / "bot.lock")
    with open(lock, "w", encoding="utf-8") as handle:
        handle.write("not-a-pid")
    acquired = state.acquire_instance_lock(str(tmp_path))
    assert acquired == lock
    state.release_instance_lock(lock)


def test_lock_released_then_reacquirable(tmp_path):
    path = state.acquire_instance_lock(str(tmp_path))
    assert path is not None
    state.release_instance_lock(path)
    assert state.acquire_instance_lock(str(tmp_path)) == str(
        tmp_path / "bot.lock"
    )
    state.release_instance_lock(str(tmp_path / "bot.lock"))
