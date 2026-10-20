"""Process-level SIGTERM drain tests (CR03 / handoff L3).

Real CLI subprocesses with signals injected at real seams: the flock
holder is the child's own pid (no stale-PID probing), SIGTERM drains
cooperatively within the bounded window and exits 0, durable rows stay
durable mid-send (at-least-once, nothing dropped), a second SIGTERM is
idempotent, and the lock is reacquirable immediately after exit.

Readiness is gated on a lifecycle log line, never on the lock file: the
lock is written before the SIGTERM handler is installed, so a signal in
that window would be fatal — the readiness marker proves the handler is
armed before any signal is sent. Send-phase tests gate further on the
pending table: the signal must land mid-send (after alerts are durably
persisted), never mid-scan, where a correct cancel legitimately leaves
zero rows — a clock-only gate raced the scan on slow runners.
"""

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "check_for_ebay_deals.py"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "offline-scan"
SMOKE_CSV = FIXTURE_DIR / "smoke.csv"
SCAN_SLEEP_ENV = "CHECK_FOR_EBAY_DEALS_SCAN_SLEEP_SECONDS"
BLOCK_SECONDS_ENV = "CHECK_FOR_EBAY_DEALS_STUB_BLOCK_SECONDS"
OPEN_AT = "2026-10-19T13:00:00Z"
LOCK_PID_MARKER = "pid "
HANDLER_READY = "offline stub transport"


def fixture_args(data: Path) -> list[str]:
    return [
        "--fixture-dir", str(FIXTURE_DIR),
        "--csv", str(SMOKE_CSV),
        "--data-dir", str(data),
    ]


def start_cli(data: Path, log_path: Path, extra: list[str],
              env_extra: dict[str, str]) -> "subprocess.Popen[str]":
    """Spawn the CLI and return once its SIGTERM handler is armed."""
    env = dict(os.environ)
    env.update(env_extra)
    with log_path.open("wb") as log_file:
        proc = subprocess.Popen(
            [sys.executable, str(CLI), "--once", *extra, *fixture_args(data)],
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            cwd=str(data.parent),
            env=env,
        )
    deadline = time.monotonic() + 20
    while True:
        # The stub marker is logged inside the async lifecycle, which
        # starts only after signal.signal(SIGTERM, ...) has installed the
        # cooperative handler: seeing it proves SIGTERM is catchable.
        if HANDLER_READY in log_path.read_text(encoding="utf-8", errors="replace"):
            return proc
        if time.monotonic() > deadline or proc.poll() is not None:
            proc.kill()
            proc.wait()
            pytest.fail(f"CLI never armed its SIGTERM handler: {log_path.read_text()}")
        time.sleep(0.05)


def wait_for_delivery_phase(data: Path, proc: "subprocess.Popen[str]") -> None:
    """Block until the scan finished and alerts are durably persisted.

    deliver() begins only after every discovered alert is in the
    pending table, and the stub's per-send block keeps the first send
    in flight for seconds: signaling the moment a row appears lands
    mid-send. Polling the table, not the clock, is what makes the
    seam deterministic on slow runners.
    """
    deadline = time.monotonic() + 20
    while True:
        count = 0
        try:
            con = sqlite3.connect(str(data / "state.sqlite"), timeout=1)
            try:
                count = con.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
            finally:
                con.close()
        except sqlite3.Error:
            pass  # file mid-creation; the app owns it and will settle
        if count >= 1:
            return
        if time.monotonic() > deadline or proc.poll() is not None:
            proc.kill()
            proc.wait()
            pytest.fail("CLI never reached the delivery phase")
        time.sleep(0.05)


def lock_pid(data: Path) -> int:
    parts = (data / "bot.lock").read_text(encoding="utf-8").split()
    return int(parts[1])


def sent_and_pending(data: Path) -> "tuple[set[str], set[str]]":
    con = sqlite3.connect(str(data / "state.sqlite"))
    try:
        sent = {row[0] for row in con.execute("SELECT id FROM sent")}
        pending = {row[0] for row in con.execute("SELECT id FROM pending")}
    finally:
        con.close()
    return sent, pending


def reacquire_clean(data: Path) -> int:
    return subprocess.run(
        [sys.executable, str(CLI), *fixture_args(data), "--once", "--dry-run"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    ).returncode


def test_sigterm_during_scan_sleep_drains_clean(tmp_path):
    data = tmp_path / "data"
    proc = start_cli(data, tmp_path / "scan.log", [], {SCAN_SLEEP_ENV: "5"})
    assert lock_pid(data) == proc.pid  # flock holder is this child pid
    start = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    rc = proc.wait(timeout=15)
    elapsed = time.monotonic() - start
    assert rc == 0
    assert elapsed < 12.0  # bounded cooperative drain, never a hang
    # Cancelled mid-scan: nothing discovered or persisted; schema intact.
    sent, pending = sent_and_pending(data)
    assert sent == set()
    assert pending == set()
    assert "scan cancelled during scan sleep; draining" in (
        tmp_path / "scan.log").read_text(encoding="utf-8")
    assert reacquire_clean(data) == 0  # lock released, fresh run proceeds


def test_sigterm_during_send_keeps_every_alert_durable(tmp_path):
    data = tmp_path / "data"
    proc = start_cli(data, tmp_path / "send.log", ["--at", OPEN_AT],
                     {BLOCK_SECONDS_ENV: "5"})
    assert lock_pid(data) == proc.pid
    wait_for_delivery_phase(data, proc)
    start = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    rc = proc.wait(timeout=15)
    elapsed = time.monotonic() - start
    assert rc == 0
    assert elapsed < 12.0
    sent, pending = sent_and_pending(data)
    # The in-flight send completed (at-least-once); the rest stay pending.
    assert len(sent) >= 1
    assert len(sent | pending) == 4  # every alert is durable, none lost
    assert sent.isdisjoint(pending)  # no id both sent and pending
    assert reacquire_clean(data) == 0


def test_second_sigterm_is_idempotent_no_corruption(tmp_path):
    data = tmp_path / "data"
    proc = start_cli(data, tmp_path / "twice.log", ["--at", OPEN_AT],
                     {BLOCK_SECONDS_ENV: "5"})
    wait_for_delivery_phase(data, proc)
    proc.send_signal(signal.SIGTERM)
    proc.send_signal(signal.SIGTERM)  # repeated cancel: must be a no-op
    rc = proc.wait(timeout=15)
    assert rc == 0
    sent, pending = sent_and_pending(data)
    assert (sent | pending)
    assert sent.isdisjoint(pending)
    assert reacquire_clean(data) == 0
