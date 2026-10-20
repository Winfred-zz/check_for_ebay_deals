"""Durable alert state: sent item IDs and pending alerts (SQLite).

Approved contract (handoff 0001 step 5, design.md, 0003 CR04/CR06/CR07/CR12):
- At-least-once delivery: alerts are persisted as pending BEFORE the
  first send; an ID moves to `sent` only after a successful Discord
  send. Failed sends stay pending and retry on a later scan/run. A
  crash between "send succeeded" and "record sent" may duplicate —
  documented and accepted small window.
- Legacy `skip_items.txt` IDs are IMPORTED into `sent` (INSERT OR
  IGNORE, idempotent) on every open. The legacy file is never modified
  or cleared by opening a database: it stays a reusable import source,
  so migrating into a new data directory leaves the source intact
  (0003 CR06). Import runs in one transaction under the instance lock.
- A corrupt, partial, or unsupported existing database is refused
  loudly (StateError); it is never silently reset or repaired,
  because losing sent history would cause duplicate alerts. One
  shared validator checks: exactly one schema row equal to
  SCHEMA_VERSION, plus the sent/pending tables with their columns.
- Persistence failures raise StateError so callers stop rather than
  "continue as if deduplication is durable".
- Single-instance enforcement: a kernel advisory lock (fcntl.flock)
  on a stable lockfile inode, held for the process lifetime. A live
  holder blocks startup (including a second acquire in the same
  process — flock is per-open-file-description); a dead holder's lock
  is released by the kernel, no PID probing or unlink races (0003 CR04).
- Dry runs open the DB through a percent-encoded read-only URI so a
  path containing '#', '?', '%' or spaces cannot redirect or create a
  file (0003 CR07).
- SQLite needs no WAL: the instance lock guarantees one writer.
"""

import fcntl
import os
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import TypedDict, cast

from selection import AlertRecord

STATE_DB = "state.sqlite"
LOCK_FILE = "bot.lock"
LEGACY_SKIP_FILE = "skip_items.txt"
ID_RE = re.compile(r"[0-9A-Za-z_-]{1,64}\Z")
SCHEMA_VERSION = 1

TABLE_COLUMNS: dict[str, list[str]] = {
    "sent": ["id", "first_sent_utc"],
    "pending": ["id", "message", "price_usd", "item_url", "found_utc"],
}


class PendingAlert(TypedDict):
    """One row of the pending table, decoded for callers."""
    id: str
    message: str
    price_usd: Decimal
    link: str
    found_utc: str


class StateError(Exception):
    """Persistence failure that must stop 'continue as if durable'."""


def _dec(text: str) -> Decimal:
    return Decimal(text)


def _enc(dec_value: Decimal) -> str:
    return str(dec_value)


def _utc_stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def acquire_instance_lock(state_dir: str) -> int | None:
    """Take the single-instance kernel lock. Returns the lock file fd.

    The fd (and its flock) is held for the process lifetime; close it
    via release_instance_lock during shutdown cleanup. flock is
    released by the kernel when the holder dies, so there is no stale
    PID probing, no unlink/retry race, and no PID-namespace ambiguity
    across containers (0003 CR04). A second acquisition attempt in
    the same process fails: each open() is a new open-file
    description, and flock conflicts per description.
    """
    lock_path = os.path.join(state_dir, LOCK_FILE)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        raise StateError(f"cannot create instance lock in {state_dir}: {exc}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    try:
        _ = os.ftruncate(fd, 0)
        _ = os.write(fd, f"pid {os.getpid()} (diagnostic only)\n".encode())
    except OSError as exc:
        os.close(fd)
        raise StateError(f"cannot write instance lock: {exc}") from exc
    return fd


def release_instance_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def state_db_path(state_dir: str) -> str:
    return os.path.join(state_dir, STATE_DB)


def readonly_uri(db_path: str) -> str:
    """Read-only SQLite URI for an existing DB path (CR07).

    Path.as_uri() percent-encodes '#', '?', '%', spaces, and Unicode,
    so the mode=ro query can never be swallowed by a fragment and the
    URI can never silently point at (or create) a different file.
    """
    absolute = Path(os.path.abspath(db_path)).as_uri()
    return f"{absolute}?mode=ro"


def validate_state_schema(con: sqlite3.Connection) -> None:
    """Shared non-mutating validator used by both openers (CR12).

    Requires exactly one schema row equal to SCHEMA_VERSION and the
    sent/pending tables with their exact columns. Raises StateError.
    """
    try:
        rows = cast(
            "list[tuple[int, ...]]",
            con.execute("SELECT version FROM schema").fetchall(),
        )
    except sqlite3.Error as exc:
        raise StateError(
            f"state database partial or missing tables: schema ({exc})"
        ) from exc
    versions = [row[0] for row in rows]
    if not versions:
        raise StateError(
            "state database has no schema version row (corrupt or partial)"
        )
    if len(versions) > 1:
        raise StateError(
            f"state database has {len(versions)} schema version rows (corrupt)"
        )
    version = versions[0]
    if version > SCHEMA_VERSION:
        raise StateError(
            f"state database schema version {version} is newer than supported"
            + f" (max {SCHEMA_VERSION}); refusing to reuse state"
        )
    if version < SCHEMA_VERSION:
        raise StateError(
            f"state database schema version {version} is older than supported"
            + f" (min {SCHEMA_VERSION}); no migration available"
        )
    for table, columns in TABLE_COLUMNS.items():
        info = cast(
            "list[tuple[object, ...]]",
            con.execute(f"PRAGMA table_info({table})").fetchall(),
        )
        found = [str(row[1]) for row in info]
        if found != columns:
            raise StateError(
                f"state database partial or missing tables: {table}"
                + f" (has {found}, need {columns})"
            )


def ensure_writable_state_dir(state_dir: str) -> str:
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError as exc:
        raise StateError(f"cannot create data directory: {exc}") from exc
    if not (os.access(state_dir, os.W_OK) and os.access(state_dir, os.X_OK)):
        raise StateError(f"directory exists but is not writable: {state_dir}")
    return state_dir


def open_state(
    state_dir: str,
    legacy_skip_path: str | None,
) -> tuple[sqlite3.Connection, int]:
    """Open (creating as needed) the state database; import legacy IDs.

    Returns (sqlite connection, imported legacy ID count). The legacy
    file is read, never written; import is INSERT-OR-IGNORE so
    re-opening is idempotent and never destroys the import source.
    Raises StateError on unreadable/corrupt/partial/unsupported state.
    Must be called while holding the instance lock (0003 CR04).
    """
    _ = ensure_writable_state_dir(state_dir)
    db_path = state_db_path(state_dir)
    fresh = not os.path.isfile(db_path)
    try:
        con = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        raise StateError(f"state database unreadable: {exc}") from exc
    if fresh:
        try:
            with con:
                _ = con.execute(
                    "CREATE TABLE schema (version INTEGER PRIMARY KEY)"
                )
                _ = con.execute(
                    "INSERT INTO schema (version) VALUES (?)", (SCHEMA_VERSION,)
                )
                _ = con.execute(
                    "CREATE TABLE sent (id TEXT PRIMARY KEY, first_sent_utc TEXT)"
                )
                _ = con.execute(
                    "CREATE TABLE pending (id TEXT PRIMARY KEY, message TEXT,"
                    + " price_usd TEXT, item_url TEXT, found_utc)"
                )
        except sqlite3.Error as exc:
            con.close()
            raise StateError(
                "state database initialization failed (an existing file in the"
                + f" way, a read-only filesystem, or corruption): {exc}"
            ) from exc
    try:
        validate_state_schema(con)
    except StateError:
        con.close()
        raise
    imported = 0
    if legacy_skip_path and os.path.isfile(legacy_skip_path):
        try:
            with open(legacy_skip_path, encoding="utf-8") as handle:
                legacy_ids = [line.strip() for line in handle if line.strip()]
        except (OSError, UnicodeDecodeError) as exc:
            con.close()
            raise StateError(f"legacy state unreadable: {exc}") from exc
        invalid = [item_id for item_id in legacy_ids if not ID_RE.match(item_id)]
        if invalid:
            con.close()
            raise StateError(
                f"legacy skip file contains malformed IDs ({len(invalid)} of "
                + f"{len(legacy_ids)} lines); refusing to guess dedup history"
            )
        try:
            with con:
                for item_id in legacy_ids:
                    _ = con.execute(
                        "INSERT OR IGNORE INTO sent (id, first_sent_utc)"
                        + " VALUES (?, ?)",
                        (item_id, "migrated"),
                    )
        except sqlite3.Error as exc:
            con.close()
            raise StateError(f"legacy import transaction failed: {exc}") from exc
        imported = len(legacy_ids)
    return con, imported


def sent_ids(con: sqlite3.Connection) -> frozenset[str]:
    rows = cast("list[tuple[str, ...]]", con.execute("SELECT id FROM sent").fetchall())
    return frozenset(row[0] for row in rows)


def pending_ids(con: sqlite3.Connection) -> frozenset[str]:
    """Pending IDs without decoding full messages (0003 CR22)."""
    rows = cast(
        "list[tuple[str, ...]]", con.execute("SELECT id FROM pending").fetchall()
    )
    return frozenset(row[0] for row in rows)


def pending_alerts(con: sqlite3.Connection) -> list[PendingAlert]:
    rows = cast(
        "list[tuple[str, ...]]",
        con.execute(
            "SELECT id, message, price_usd, item_url, found_utc FROM pending"
            + " ORDER BY found_utc, id"
        ).fetchall(),
    )
    return [
        {
            "id": row[0],
            "message": row[1],
            "price_usd": _dec(row[2]),
            "link": row[3],
            "found_utc": row[4],
        }
        for row in rows
    ]


def add_pending(con: sqlite3.Connection, alert: AlertRecord, message: str) -> None:
    try:
        with con:
            _ = con.execute(
                "INSERT OR IGNORE INTO pending (id, message, price_usd, item_url,"
                + " found_utc) VALUES (?, ?, ?, ?, ?)",
                (alert["id"], message, _enc(alert["price_usd"]), alert["link"],
                 _utc_stamp()),
            )
    except sqlite3.Error as exc:
        raise StateError(f"pending persistence failed: {exc}") from exc


def drop_pending(con: sqlite3.Connection, item_id: str) -> None:
    try:
        with con:
            _ = con.execute("DELETE FROM pending WHERE id = ?", (item_id,))
    except sqlite3.Error as exc:
        raise StateError(f"pending cleanup failed: {exc}") from exc

def repair_pending(con: sqlite3.Connection, item_id: str, message: str) -> None:
    """Replace a stored pending message (oversized-row recovery, CR08)."""
    try:
        with con:
            _ = con.execute(
                "UPDATE pending SET message = ? WHERE id = ?", (message, item_id)
            )
    except sqlite3.Error as exc:
        raise StateError(f"pending repair failed: {exc}") from exc


def mark_sent(con: sqlite3.Connection, item_id: str) -> None:
    """Record delivery success: sent row + pending row in ONE transaction."""
    try:
        with con:
            _ = con.execute(
                "INSERT OR IGNORE INTO sent (id, first_sent_utc) VALUES (?, ?)",
                (item_id, _utc_stamp()),
            )
            _ = con.execute("DELETE FROM pending WHERE id = ?", (item_id,))
    except sqlite3.Error as exc:
        raise StateError(
            f"mark-sent persistence failed (alert stays pending): {exc}"
        ) from exc


def forget_sent(con: sqlite3.Connection, item_id: str) -> None:
    """Remove a sent mark; offline test helper for crash-window exercises."""
    with con:
        _ = con.execute("DELETE FROM sent WHERE id = ?", (item_id,))
