"""Behavior tests for durable state: SQLite DB, legacy import, flock lock.

Covers 0003 CR04 (kernel flock lock semantics), CR06 (non-destructive
idempotent legacy import), CR07 (encoded read-only URI), CR12 (complete
schema validation), CR20 (reaped child processes, no test-only helpers).
"""

import os
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

import state

ROOT = Path(__file__).resolve().parents[1]


def _start_holder(tmp_path: Path) -> subprocess.Popen[str]:
    """Spawn a real process that holds the flock, waiting for proof."""
    code = (
        "import sys, time;"
        "import state;"
        "fd = state.acquire_instance_lock(sys.argv[1]);"
        "print('HELD', fd, flush=True);"
        "time.sleep(120)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    first = proc.stdout.readline() if proc.stdout is not None else ""
    if "HELD" not in first:
        proc.kill()
        proc.wait()
        pytest.fail(f"holder never acquired the lock: {first!r}")
    return proc


def test_fresh_state_db_is_queryable(tmp_path):
    con, imported = state.open_state(str(tmp_path), None)
    assert imported == 0
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
    assert state.pending_ids(con) == frozenset({"167111111111"})
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


def test_mark_sent_failure_keeps_alert_pending(tmp_path):
    """Persistence failure raises StateError; the alert stays pending."""
    con, _ = state.open_state(str(tmp_path), None)
    alert = {"id": "400000111222", "price_usd": Decimal("1"), "link": "x"}
    state.add_pending(con, alert, "msg")
    con.close()  # simulate the state connection dying under the caller
    with pytest.raises(state.StateError, match="stays pending"):
        state.mark_sent(con, "400000111222")
    con2, _ = state.open_state(str(tmp_path), None)
    assert [row["id"] for row in state.pending_alerts(con2)] == ["400000111222"]
    assert "400000111222" not in state.sent_ids(con2)
    con2.close()


def test_migration_imports_ids_leaves_source_intact(tmp_path):
    legacy = tmp_path / "skip_items.txt"
    original = "111000222000\n222000111000\n\n"
    legacy.write_text(original, encoding="utf-8")
    con, imported = state.open_state(str(tmp_path), str(legacy))
    assert imported == 2
    assert state.sent_ids(con) == frozenset({"111000222000", "222000111000"})
    con.close()
    assert legacy.read_text(encoding="utf-8") == original


def test_migration_is_idempotent_across_reopens(tmp_path):
    legacy = tmp_path / "skip_items.txt"
    legacy.write_text("333444555000\n333444555000\n", encoding="utf-8")
    con, first = state.open_state(str(tmp_path), str(legacy))
    con.close()
    con2, second = state.open_state(str(tmp_path), str(legacy))
    assert first == 2 and second == 2
    assert state.sent_ids(con2) == frozenset({"333444555000"})
    con2.close()


def test_migration_into_new_directory_still_imports(tmp_path):
    """0003 CR06 probe: directory B must import from the intact source."""
    legacy = tmp_path / "skip_items.txt"
    original = "111000222000\n222000111000\n"
    legacy.write_text(original, encoding="utf-8")
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    con_a, _ = state.open_state(str(dir_a), str(legacy))
    con_a.close()
    con_b, imported_b = state.open_state(str(dir_b), str(legacy))
    assert imported_b == 2
    assert state.sent_ids(con_b) == frozenset({"111000222000", "222000111000"})
    con_b.close()
    assert legacy.read_text(encoding="utf-8") == original


def test_malformed_legacy_ids_refused(tmp_path):
    legacy = tmp_path / "skip_items.txt"
    legacy.write_text("ok123\nbad id!\n", encoding="utf-8")
    with pytest.raises(state.StateError, match="malformed IDs"):
        state.open_state(str(tmp_path), str(legacy))
    # Refusal must not destroy the source data.
    assert legacy.read_text(encoding="utf-8") == "ok123\nbad id!\n"


def test_empty_legacy_file_no_import(tmp_path):
    legacy = tmp_path / "skip_items.txt"
    legacy.write_text("", encoding="utf-8")
    con, imported = state.open_state(str(tmp_path), str(legacy))
    assert imported == 0
    con.close()
    assert legacy.read_text(encoding="utf-8") == ""


def test_empty_db_file_refused(tmp_path):
    db = tmp_path / "state.sqlite"
    db.write_bytes(b"")
    with pytest.raises(state.StateError, match="partial or missing tables: schema"):
        state.open_state(str(tmp_path), None)


def test_schema_table_without_row_refused(tmp_path):
    db = tmp_path / "state.sqlite"
    seed = sqlite3.connect(db)
    with seed:
        seed.execute("CREATE TABLE schema (version INTEGER PRIMARY KEY)")
    seed.close()
    with pytest.raises(state.StateError, match="no schema version row"):
        state.open_state(str(tmp_path), None)


def test_partial_tables_refused(tmp_path):
    """schema row present, sent table present, pending missing (CR12 probe)."""
    db = tmp_path / "state.sqlite"
    seed = sqlite3.connect(db)
    with seed:
        seed.execute("CREATE TABLE schema (version INTEGER PRIMARY KEY)")
        seed.execute("INSERT INTO schema (version) VALUES (1)")
        seed.execute("CREATE TABLE sent (id TEXT PRIMARY KEY, first_sent_utc TEXT)")
    seed.close()
    with pytest.raises(state.StateError, match="partial or missing tables: pending"):
        state.open_state(str(tmp_path), None)


def test_missing_column_refused(tmp_path):
    db = tmp_path / "state.sqlite"
    seed = sqlite3.connect(db)
    with seed:
        seed.execute("CREATE TABLE schema (version INTEGER PRIMARY KEY)")
        seed.execute("INSERT INTO schema (version) VALUES (1)")
        seed.execute("CREATE TABLE sent (id TEXT PRIMARY KEY)")
        seed.execute(
            "CREATE TABLE pending (id TEXT PRIMARY KEY, message TEXT,"
            + " price_usd TEXT, item_url TEXT, found_utc)"
        )
    seed.close()
    with pytest.raises(state.StateError, match="partial or missing tables: sent"):
        state.open_state(str(tmp_path), None)


def test_older_schema_refused(tmp_path):
    db = tmp_path / "state.sqlite"
    seed = sqlite3.connect(db)
    with seed:
        seed.execute("CREATE TABLE schema (version INTEGER PRIMARY KEY)")
        seed.execute("INSERT INTO schema (version) VALUES (0)")
    seed.close()
    with pytest.raises(state.StateError, match="older than supported"):
        state.open_state(str(tmp_path), None)


def test_multiple_schema_rows_refused(tmp_path):
    db = tmp_path / "state.sqlite"
    seed = sqlite3.connect(db)
    with seed:
        seed.execute("CREATE TABLE schema (version INTEGER PRIMARY KEY)")
        seed.execute("INSERT INTO schema (version) VALUES (1)")
        seed.execute("INSERT INTO schema (version) VALUES (101)")
    seed.close()
    with pytest.raises(state.StateError, match="2 schema version rows"):
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


def test_corrupt_db_refused(tmp_path):
    db = tmp_path / "state.sqlite"
    db.write_bytes(b"\x00garbage\xff" * 500)
    with pytest.raises(state.StateError):
        state.open_state(str(tmp_path), None)


def test_validator_rejects_and_accepts_connections(tmp_path):
    """The shared validator works on an open connection (CR12, CR07 path)."""
    con, _ = state.open_state(str(tmp_path), None)
    assert state.validate_state_schema(con) is None
    con.close()
    broken = sqlite3.connect(":memory:")
    with broken:
        broken.execute("CREATE TABLE schema (version INTEGER PRIMARY KEY)")
        broken.execute("INSERT INTO schema (version) VALUES (1)")
    with pytest.raises(state.StateError, match="partial or missing tables"):
        state.validate_state_schema(broken)
    broken.close()


def test_readonly_uri_percent_encodes_hazards(tmp_path):
    nasty = tmp_path / "we#ird?q%20dir"
    nasty.mkdir()
    con, _ = state.open_state(str(nasty), None)
    con.close()
    db = state.state_db_path(str(nasty))
    uri = state.readonly_uri(db)
    assert "%23" in uri and uri.endswith("?mode=ro")
    tree_before = sorted(p.name for p in nasty.iterdir())
    ro = sqlite3.connect(uri, uri=True)
    assert ro.execute("SELECT version FROM schema").fetchone() == (1,)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO sent (id, first_sent_utc) VALUES ('x', 'y')")
    ro.close()
    tree_after = sorted(p.name for p in nasty.iterdir())
    assert tree_after == tree_before


def test_readonly_uri_rejects_writes_on_normal_paths(tmp_path):
    con, _ = state.open_state(str(tmp_path), None)
    con.close()
    ro = sqlite3.connect(state.readonly_uri(state.state_db_path(str(tmp_path))),
                         uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("CREATE TABLE extra (n INTEGER)")
    ro.close()


def test_lock_fd_held_with_diagnostics(tmp_path):
    fd = state.acquire_instance_lock(str(tmp_path))
    assert isinstance(fd, int)
    text = Path(tmp_path / "bot.lock").read_text(encoding="utf-8")
    assert f"pid {os.getpid()}" in text
    assert "diagnostic" in text
    state.release_instance_lock(fd)


def test_same_process_second_acquire_refused(tmp_path):
    fd = state.acquire_instance_lock(str(tmp_path))
    assert fd is not None
    assert state.acquire_instance_lock(str(tmp_path)) is None
    state.release_instance_lock(fd)


def test_second_instance_refused_until_holder_reaped(tmp_path):
    holder = _start_holder(tmp_path)
    try:
        assert state.acquire_instance_lock(str(tmp_path)) is None
    finally:
        holder.kill()
        holder.wait()
    fd = state.acquire_instance_lock(str(tmp_path))
    assert fd is not None
    state.release_instance_lock(fd)


def test_dead_holder_lock_reacquirable(tmp_path):
    """Kernel releases flock when the holder exits; PID text is stale."""
    holder = _start_holder(tmp_path)
    holder.kill()
    holder.wait()
    lock = Path(tmp_path) / "bot.lock"
    assert f"pid {holder.pid}" in lock.read_text(encoding="utf-8")
    fd = state.acquire_instance_lock(str(tmp_path))
    assert fd is not None
    assert f"pid {os.getpid()}" in lock.read_text(encoding="utf-8")
    state.release_instance_lock(fd)


def test_garbage_lock_reacquirable(tmp_path):
    lock = tmp_path / "bot.lock"
    lock.write_text("not-a-pid\n", encoding="utf-8")
    fd = state.acquire_instance_lock(str(tmp_path))
    assert fd is not None
    state.release_instance_lock(fd)


def test_lock_released_then_reacquirable(tmp_path):
    fd = state.acquire_instance_lock(str(tmp_path))
    assert fd is not None
    state.release_instance_lock(fd)
    fd2 = state.acquire_instance_lock(str(tmp_path))
    assert fd2 is not None
    state.release_instance_lock(fd2)


def test_lock_io_failure_normalized(tmp_path):
    readonly_root = tmp_path / "ro"
    readonly_root.mkdir()
    os.chmod(readonly_root, 0o500)
    with pytest.raises(state.StateError, match="cannot create instance lock"):
        _ = state.acquire_instance_lock(str(readonly_root))
    os.chmod(readonly_root, 0o700)


def test_open_state_requires_existing_usable_dir(tmp_path):
    missing_parent = tmp_path / "no" / "such"
    os.chmod(tmp_path, 0o500)
    try:
        with pytest.raises(state.StateError, match="cannot create data directory"):
            state.open_state(str(missing_parent), None)
    finally:
        os.chmod(tmp_path, 0o700)


def test_import_side_effect_free_import():
    """Importing state must not create files (F26 boundary kept)."""
    code = (
        "import os, sys, tempfile;"
        "sys.path.insert(0, sys.argv[1]);"
        "os.chdir(tempfile.mkdtemp());"
        "import state;"
        "print(sorted(os.listdir('.')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, str(ROOT)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert out.stdout.strip() == "[]"
