"""Durable alert state: sent item IDs and pending alerts (SQLite).

Approved contract (handoff 0001, step 5 / design.md):
- At-least-once delivery: alerts are persisted as pending BEFORE the
  first send; an ID moves to `sent` only after a successful Discord
  send. Failed sends stay pending and retry on a later scan/run. A
  crash between "send succeeded" and "record sent" may duplicate —
  documented and accepted small window.
- Legacy `skip_items.txt` IDs migrate into `sent` on first open. The
  legacy file is copied to `skip_items.txt.migrated-<utc-stamp>.bak`
  (retained, git-ignored), then replaced by an empty file at the same
  path so later scans never double-count it.
- A corrupt existing database is refused loudly (StateError); it is
  never silently reset, because losing sent history would cause
  duplicate alerts (F17).
- Persistence failures raise StateError so callers stop rather than
  "continue as if deduplication is durable".
- Single-instance enforcement: an O_EXCL lockfile recording the PID.
  A live holder blocks startup; a dead holder's lock is reclaimed.
- SQLite needs no WAL: the instance lock guarantees one writer.
"""

import os
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import TypedDict, cast

from selection import AlertRecord

STATE_DB = "state.sqlite"
LOCK_FILE = "bot.lock"
LEGACY_SKIP_FILE = "skip_items.txt"
ID_RE = re.compile(r"[0-9A-Za-z_-]{1,64}\Z")
SCHEMA_VERSION = 1


class PendingAlert(TypedDict):
    """One row of the pending table, decoded for callers."""
    id: str
    message: str
    price_usd: Decimal
    link: str
    found_utc: str


class StateError(Exception):
    """Persistence failure that must stop 'continue as if durable'."""


def _atomic_write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        _ = handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _dec(text: str) -> Decimal:
    return Decimal(text)


def _enc(dec_value: Decimal) -> str:
    return str(dec_value)


def _utc_stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def acquire_instance_lock(state_dir: str) -> str | None:
    """Create the single-instance lock.

    Returns the lock path on success, or None when another live process
    already holds it (second instance must refuse to start). A lock left
    by a dead PID is reclaimed (unlink then retry once).
    """
    lock_path = os.path.join(state_dir, LOCK_FILE)
    for _attempt in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = None
            try:
                with open(lock_path, encoding="utf-8") as handle:
                    holder = int(handle.read().strip())
            except (OSError, ValueError):
                holder = None
            alive = False
            if holder is not None and holder != os.getpid():
                try:
                    os.kill(holder, 0)
                    alive = os.path.exists(f"/proc/{holder}")
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    alive = True
            if alive:
                return None
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass
            continue
        _ = os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
        return lock_path
    return None


def release_instance_lock(lock_path: str) -> None:
    try:
        os.unlink(lock_path)
    except FileNotFoundError:
        pass


def open_state(
    state_dir: str,
    legacy_skip_path: str | None,
) -> tuple[sqlite3.Connection, str | None]:
    """Open (create/migrate as needed) the state database.

    Returns (sqlite connection, legacy backup path or None).
    Raises StateError on unreadable/corrupt existing state or a schema
    newer than this code supports.
    """
    os.makedirs(state_dir, exist_ok=True)
    db_path = os.path.join(state_dir, STATE_DB)
    fresh = not os.path.isfile(db_path)
    try:
        con = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        raise StateError(f"state database unreadable: {exc}") from None
    cur = con.cursor()
    if fresh:
        try:
            _ = cur.execute("CREATE TABLE schema (version INTEGER PRIMARY KEY)")
            _ = cur.execute(
                "INSERT INTO schema (version) VALUES (?)", (SCHEMA_VERSION,)
            )
            _ = cur.execute(
                "CREATE TABLE sent (id TEXT PRIMARY KEY, first_sent_utc TEXT)"
            )
            _ = cur.execute(
                "CREATE TABLE pending (id TEXT PRIMARY KEY, message TEXT,"
                + " price_usd TEXT, item_url TEXT, found_utc TEXT)"
            )
            con.commit()
        except sqlite3.OperationalError as exc:
            con.close()
            raise StateError(f"state database corrupt or unsupported: {exc}") from None
    try:
        ver_row = cast(
            "tuple[int, ...] | None",
            cur.execute("SELECT version FROM schema LIMIT 1").fetchone(),
        )
    except sqlite3.Error as exc:
        con.close()
        raise StateError(f"state database corrupt or unsupported: {exc}") from None
    if ver_row is None:
        con.close()
        raise StateError(
            "state database has no schema version row (corrupt or partial)"
        )
    version = ver_row[0]
    if version > SCHEMA_VERSION:
        con.close()
        raise StateError(
                f"state database schema version {version} is newer than supported"
                + f" (max {SCHEMA_VERSION}); refusing to reuse state"
        )
    backup_path = None
    if legacy_skip_path and os.path.isfile(legacy_skip_path):
        try:
            with open(legacy_skip_path, encoding="utf-8") as handle:
                legacy_ids = [line.strip() for line in handle if line.strip()]
        except (OSError, UnicodeDecodeError) as exc:
            raise StateError(f"legacy state unreadable: {exc}") from None
        invalid = [item_id for item_id in legacy_ids if not ID_RE.match(item_id)]
        if invalid:
            raise StateError(
                f"legacy skip file contains malformed IDs ({len(invalid)} of "
                + f"{len(legacy_ids)} lines); refusing to guess dedup history"
            )
        with con:
            for item_id in legacy_ids:
                _ = cur.execute(
                    "INSERT OR IGNORE INTO sent (id, first_sent_utc) VALUES (?, ?)",
                    (item_id, "migrated"),
                )
        if legacy_ids:
            backup_path = os.path.join(
                state_dir, f"{LEGACY_SKIP_FILE}.migrated-{_utc_stamp()}.bak"
            )
            with open(legacy_skip_path, encoding="utf-8") as src:
                _atomic_write_text(backup_path, src.read())
            _atomic_write_text(legacy_skip_path, "")
    return con, backup_path


def sent_ids(con: sqlite3.Connection) -> frozenset[str]:
    rows = cast("list[tuple[str, ...]]", con.execute("SELECT id FROM sent").fetchall())
    return frozenset(row[0] for row in rows)


def pending_alerts(con: sqlite3.Connection) -> list[PendingAlert]:
    rows = cast(
        "list[tuple[str, ...]]",
        con.execute(
            "SELECT id, message, price_usd, item_url, found_utc FROM pending"
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
    with con:
        _ = con.execute(
            "INSERT INTO pending (id, message, price_usd, item_url, found_utc)"
            + " VALUES (?, ?, ?, ?, ?)",
            (alert["id"], message, _enc(alert["price_usd"]), alert["link"],
             _utc_stamp()),
        )


def drop_pending(con: sqlite3.Connection, item_id: str) -> None:
    with con:
        _ = con.execute("DELETE FROM pending WHERE id = ?", (item_id,))


def mark_sent(con: sqlite3.Connection, item_id: str) -> None:
    with con:
        _ = con.execute(
            "INSERT INTO sent (id, first_sent_utc) VALUES (?, ?)",
            (item_id, _utc_stamp()),
        )
        _ = con.execute("DELETE FROM pending WHERE id = ?", (item_id,))


def forget_sent(con: sqlite3.Connection, item_id: str) -> None:
    """Remove a sent mark; test-only helper for crash-window exercises."""
    with con:
        _ = con.execute("DELETE FROM sent WHERE id = ?", (item_id,))
