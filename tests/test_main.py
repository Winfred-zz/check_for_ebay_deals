"""Whole-file CLI contract: exit codes, offline scans, lock, quiet hours."""
import os
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
DISABLE_NETWORK_ENV = "CHECK_FOR_EBAY_DEALS_DISABLE_NETWORK"


def fixture_args(data_dir: Path) -> list[str]:
    return [
        "--fixture-dir",
        str(FIXTURE_DIR),
        "--csv",
        str(SMOKE_CSV),
        "--data-dir",
        str(data_dir),
    ]


def run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def pending_rows(data_dir: Path) -> list[str]:
    con = sqlite3.connect(str(data_dir / "state.sqlite"))
    return [row[0] for row in con.execute("SELECT id FROM pending")]


def sent_rows(data_dir: Path) -> list[str]:
    con = sqlite3.connect(str(data_dir / "state.sqlite"))
    return [row[0] for row in con.execute("SELECT id FROM sent")]


def test_dry_run_fixture_scan_alerts_with_absent_db(tmp_path):
    data = tmp_path / "data"
    result = run_cli(
        ["--once", "--dry-run", *fixture_args(data)],
        tmp_path,
    )
    assert result.returncode == 0
    assert "DRY-RUN ALERT:" in result.stdout
    assert "12.34" in result.stdout
    assert "no writes, no sends" in result.stderr
    assert not (data / "state.sqlite").is_file()




def test_partial_db_dry_run_refused(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "state.sqlite").touch()
    con = sqlite3.connect(str(data / "state.sqlite"))
    con.execute("CREATE TABLE half_written (n INTEGER)")
    con.close()
    result = run_cli(["--once", "--dry-run", *fixture_args(data)], tmp_path)
    assert result.returncode == 2
    assert "partial or missing tables" in result.stderr


def test_corrupt_db_scan_refused(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "state.sqlite").write_bytes(b"this is not a sqlite database")
    result = run_cli(["--once", *fixture_args(data)], tmp_path)
    assert result.returncode == 2
    assert "state database partial or missing tables" in result.stderr


def test_live_network_startup_refused(tmp_path):
    data = tmp_path / "data"
    env = dict(os.environ)
    env[DISABLE_NETWORK_ENV] = "1"
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "--once",
            "--data-dir",
            str(data),
            "--csv",
            str(SMOKE_CSV),
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    assert result.returncode == 2
    assert "network disabled for this run" in result.stderr


def test_naive_at_clock_rejected(tmp_path):
    data = tmp_path / "data"
    result = run_cli(
        [
            "--once",
            "--dry-run",
            "--at",
            "2026-10-19T13:00:00",
            *fixture_args(data),
        ],
        tmp_path,
    )
    assert result.returncode == 2
    assert "naive times rejected" in result.stderr


def test_second_live_instance_refused_until_holder_dies(tmp_path):
    data = tmp_path / "data"
    first = subprocess.Popen(
        [sys.executable, str(CLI), *fixture_args(data)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        lock = data / "bot.lock"
        deadline = time.monotonic() + 10
        while not lock.is_file():
            if time.monotonic() > deadline or first.poll() is not None:
                pytest.fail("first instance never created its lock")
            time.sleep(0.05)
        second = run_cli(fixture_args(data) + ["--once"], tmp_path)
        assert second.returncode == 3
        assert "another instance holds the lock" in second.stderr
    finally:
        first.kill()
        first.wait()
    third = run_cli(fixture_args(data) + ["--once"], tmp_path)
    assert third.returncode == 0


def test_quiet_window_stores_pending_then_window_open_delivers(tmp_path):
    data = tmp_path / "data"
    quiet = run_cli(
        ["--once", "--at", "2026-10-19T05:00:00Z", *fixture_args(data)],
        tmp_path,
    )
    assert quiet.returncode == 0
    assert "zero delivery attempts" in quiet.stderr
    assert "STUB DELIVERY" not in quiet.stdout
    stored = pending_rows(data)
    assert stored and not sent_rows(data)
    open_run = run_cli(
        ["--once", "--at", "2026-10-19T13:00:00Z", *fixture_args(data)],
        tmp_path,
    )
    assert open_run.returncode == 0
    assert "STUB DELIVERY" in open_run.stdout
    assert not pending_rows(data)
    assert set(stored) <= set(sent_rows(data))


LAYOUT_FIXTURES = ROOT / "tests" / "fixtures" / "layout"


def test_layout_fixture_classifies_each_page_shape(tmp_path):
    data = tmp_path / "data"
    result = run_cli(
        [
            "--once",
            "--fixture-dir",
            str(LAYOUT_FIXTURES),
            "--csv",
            str(LAYOUT_FIXTURES / "layout.csv"),
            "--data-dir",
            str(data),
            "--at",
            "2026-10-19T13:00:00Z",
        ],
        tmp_path,
    )
    assert result.returncode == 0
    out = result.stderr
    # 10-results page with no cards: suspicious, never a clean empty scan.
    assert "'ten query' has neither cards nor known markers" in out
    # Deny page with decoy cards: denied wins over card presence.
    assert "looks like a deny/challenge page" in out
    # Mystery page: suspicious, not treated as empty success.
    assert "'mystery query' has neither cards nor known markers" in out
    # Genuine zero page: accepted as a clean empty scan (no warning).
    assert 'zero query' not in out.replace("zero delivery attempts", "")
    # Suspicious pages produce no alerts; the zero page produces none either.
    assert "STUB DELIVERY" not in result.stdout
    assert not pending_rows(data)
