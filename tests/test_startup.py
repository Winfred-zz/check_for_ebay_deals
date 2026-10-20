"""Startup validation contract (CR13/CR15/CR17/CR05/CR10 guard/CR16 exit 5).

Every case drives the real CLI: a config/state problem exits 2 with an
actionable message and no traceback; fixture runs can never touch the
default state root; stub knobs are fixture-only; required --once work
that fails exits 5 with durable rows left behind.
"""

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "check_for_ebay_deals.py"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "offline-scan"
SMOKE_CSV = FIXTURE_DIR / "smoke.csv"
STUB_FAIL_AFTER_ENV = "CHECK_FOR_EBAY_DEALS_STUB_FAIL_AFTER"
ALLOW_AT_CLOCK_ENV = "CHECK_FOR_EBAY_DEALS_ALLOW_AT_CLOCK"
DISABLE_NETWORK_ENV = "CHECK_FOR_EBAY_DEALS_DISABLE_NETWORK"
CSV_ONE = "max_price,friendly_name,search_query\n10,one,q one\n"


def run_cli(
    args: list[str], cwd: Path, env: "dict[str, str] | None" = None
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env if env is not None else dict(os.environ),
    )


def fixture_csv(tmp: Path) -> Path:
    path = tmp / "one.csv"
    path.write_text(CSV_ONE, encoding="utf-8")
    return path


def make_fixture(
    tmp: Path, manifest_text: str, files: "dict[str, bytes] | None" = None
) -> Path:
    fx = tmp / "fx"
    fx.mkdir()
    (fx / "manifest.json").write_text(manifest_text, encoding="utf-8")
    for name, blob in (files or {}).items():
        (fx / name).write_bytes(blob)
    return fx


def fixture_run_args(tmp: Path, fixture_dir: Path, csv_path: Path) -> list[str]:
    data = tmp / "data"
    return [
        "--once",
        "--fixture-dir",
        str(fixture_dir),
        "--csv",
        str(csv_path),
        "--data-dir",
        str(data),
    ]


def test_header_only_csv_is_config_error_not_empty_success(tmp_path):
    csv = tmp_path / "heads.csv"
    csv.write_text("max_price,friendly_name,search_query\n", encoding="utf-8")
    result = run_cli(
        ["--once", "--dry-run", "--csv", str(csv), "--data-dir", str(tmp_path / "d")],
        tmp_path,
    )
    assert result.returncode == 2
    assert "headers but no search rows" in result.stderr
    assert "Traceback" not in result.stderr


def test_duplicate_csv_headers_rejected(tmp_path):
    csv = tmp_path / "dup.csv"
    csv.write_text(
        "max_price,friendly_name,search_query,search_query\n", encoding="utf-8"
    )
    result = run_cli(
        ["--once", "--dry-run", "--csv", str(csv), "--data-dir", str(tmp_path / "d")],
        tmp_path,
    )
    assert result.returncode == 2
    assert "duplicate headers" in result.stderr


def test_unwritable_data_dir_exits_2_without_traceback(tmp_path):
    data = tmp_path / "locked"
    data.mkdir()
    os.chmod(data, 0o555)
    result = run_cli(
        [
            "--once",
            "--fixture-dir",
            str(FIXTURE_DIR),
            "--csv",
            str(SMOKE_CSV),
            "--data-dir",
            str(data),
        ],
        tmp_path,
    )
    assert result.returncode == 2
    assert "log directory unusable" in result.stderr
    assert "Traceback" not in result.stderr


def test_data_dir_path_is_file_exits_2(tmp_path):
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory", encoding="utf-8")
    result = run_cli(
        [
            "--once",
            "--fixture-dir",
            str(FIXTURE_DIR),
            "--csv",
            str(SMOKE_CSV),
            "--data-dir",
            str(blocked),
        ],
        tmp_path,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_logs_dir_is_file_exits_2_without_traceback(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "logs").write_text("not a directory", encoding="utf-8")
    result = run_cli(
        [
            "--once",
            "--dry-run",
            "--fixture-dir",
            str(FIXTURE_DIR),
            "--csv",
            str(SMOKE_CSV),
            "--data-dir",
            str(data),
        ],
        tmp_path,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_malformed_manifest_json_exits_2(tmp_path):
    fx = make_fixture(tmp_path, '{"q one": ')
    result = run_cli(
        fixture_run_args(tmp_path, fx, fixture_csv(tmp_path)), tmp_path
    )
    assert result.returncode == 2
    assert "not valid JSON" in result.stderr


def test_manifest_non_string_values_rejected(tmp_path):
    fx = make_fixture(
        tmp_path, '{"q one": 7}', {"ok.html": b"<html><body>ok</body></html>"}
    )
    result = run_cli(
        fixture_run_args(tmp_path, fx, fixture_csv(tmp_path)), tmp_path
    )
    assert result.returncode == 2
    assert "string queries" in result.stderr


def test_manifest_traversal_rejected(tmp_path):
    (tmp_path / "secret.html").write_text("<html>secret</html>", encoding="utf-8")
    fx = make_fixture(
        tmp_path, '{"q one": "../secret.html"}', {"ok.html": b"<html>ok</html>"}
    )
    result = run_cli(
        fixture_run_args(tmp_path, fx, fixture_csv(tmp_path)), tmp_path
    )
    assert result.returncode == 2
    assert "escapes the fixture dir" in result.stderr


def test_manifest_symlink_escape_rejected(tmp_path):
    (tmp_path / "secret.html").write_text("<html>secret</html>", encoding="utf-8")
    fx = make_fixture(tmp_path, '{"q one": "link.html"}')
    os.symlink(tmp_path / "secret.html", fx / "link.html")
    result = run_cli(
        fixture_run_args(tmp_path, fx, fixture_csv(tmp_path)), tmp_path
    )
    assert result.returncode == 2
    assert "escapes the fixture dir" in result.stderr


def test_manifest_absolute_path_escape_rejected(tmp_path):
    fx = make_fixture(tmp_path, '{"q one": "/etc/hostname"}')
    result = run_cli(
        fixture_run_args(tmp_path, fx, fixture_csv(tmp_path)), tmp_path
    )
    assert result.returncode == 2
    assert "escapes the fixture dir" in result.stderr


def test_manifest_missing_target_file_rejected(tmp_path):
    fx = make_fixture(tmp_path, '{"q one": "gone.html"}')
    result = run_cli(
        fixture_run_args(tmp_path, fx, fixture_csv(tmp_path)), tmp_path
    )
    assert result.returncode == 2
    assert "not a file" in result.stderr


def test_manifest_non_utf8_target_rejected(tmp_path):
    fx = make_fixture(
        tmp_path, '{"q one": "bin.html"}', {"bin.html": b"\xff\xfe\x00garbage"}
    )
    result = run_cli(
        fixture_run_args(tmp_path, fx, fixture_csv(tmp_path)), tmp_path
    )
    assert result.returncode == 2
    assert "unreadable as UTF-8" in result.stderr


def test_manifest_missing_csv_query_rejected(tmp_path):
    fx = make_fixture(
        tmp_path, '{"other query": "ok.html"}', {"ok.html": b"<html>ok</html>"}
    )
    result = run_cli(
        fixture_run_args(tmp_path, fx, fixture_csv(tmp_path)), tmp_path
    )
    assert result.returncode == 2
    assert "manifest lacks searches" in result.stderr


def test_stub_env_outside_fixture_run_rejected(tmp_path):
    env = dict(os.environ)
    env[STUB_FAIL_AFTER_ENV] = "1"
    csv = fixture_csv(tmp_path)
    result = run_cli(
        ["--once", "--dry-run", "--csv", str(csv), "--data-dir", str(tmp_path / "d")],
        tmp_path,
        env,
    )
    assert result.returncode == 2
    assert "only applies to --fixture-dir" in result.stderr


def test_negative_stub_seconds_rejected(tmp_path):
    env = dict(os.environ)
    env["CHECK_FOR_EBAY_DEALS_STUB_BLOCK_SECONDS"] = "-5"
    result = run_cli(
        fixture_run_args(tmp_path, FIXTURE_DIR, SMOKE_CSV), tmp_path, env
    )
    assert result.returncode == 2
    assert "nonnegative" in result.stderr


def test_dry_run_without_network_refused_even_with_live_csv(tmp_path):
    env = dict(os.environ)
    env[DISABLE_NETWORK_ENV] = "1"
    result = run_cli(
        [
            "--once",
            "--dry-run",
            "--csv",
            str(SMOKE_CSV),
            "--data-dir",
            str(tmp_path / "d"),
        ],
        tmp_path,
        env,
    )
    assert result.returncode == 2
    assert "network disabled for this run" in result.stderr


def test_live_at_clock_delivery_requires_opt_in(tmp_path):
    env = dict(os.environ)
    env[DISABLE_NETWORK_ENV] = "1"
    env.pop(ALLOW_AT_CLOCK_ENV, None)
    result = run_cli(
        [
            "--once",
            "--at",
            "2026-10-19T13:00:00Z",
            "--csv",
            str(SMOKE_CSV),
            "--data-dir",
            str(tmp_path / "d"),
        ],
        tmp_path,
        env,
    )
    assert result.returncode == 2
    assert "requires CHECK_FOR_EBAY_DEALS_ALLOW_AT_CLOCK=1" in result.stderr


def test_live_at_clock_proceeds_when_opted_in(tmp_path):
    env = dict(os.environ)
    env[DISABLE_NETWORK_ENV] = "1"
    env[ALLOW_AT_CLOCK_ENV] = "1"
    result = run_cli(
        [
            "--once",
            "--at",
            "2026-10-19T13:00:00Z",
            "--csv",
            str(SMOKE_CSV),
            "--data-dir",
            str(tmp_path / "d"),
        ],
        tmp_path,
        env,
    )
    assert result.returncode == 2
    assert "network disabled for this run" in result.stderr


def test_fixture_nondry_refuses_default_data_dir(tmp_path):
    result = run_cli(
        [
            "--once",
            "--fixture-dir",
            str(FIXTURE_DIR),
            "--csv",
            str(SMOKE_CSV),
        ],
        tmp_path,
    )
    assert result.returncode == 2
    assert "default data dir" in result.stderr


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_fixture_run_leaves_seeded_state_bytes_unchanged(tmp_path):
    seeded = tmp_path / "seed"
    seeded.mkdir()
    (seeded / "state.sqlite").write_bytes(b"SENTINEL PROD STATE")
    (seeded / "skip_items.txt").write_text("999888777666\n", encoding="utf-8")
    before_db = digest(seeded / "state.sqlite")
    before_skip = digest(seeded / "skip_items.txt")
    data = tmp_path / "data"
    result = run_cli(
        [
            "--once",
            "--fixture-dir",
            str(FIXTURE_DIR),
            "--csv",
            str(SMOKE_CSV),
            "--data-dir",
            str(data),
        ],
        tmp_path,
    )
    assert result.returncode == 0
    assert digest(seeded / "state.sqlite") == before_db
    assert digest(seeded / "skip_items.txt") == before_skip
    assert not (seeded / "state.sqlite").is_symlink()
    assert (data / "state.sqlite").is_file()


def test_required_once_work_fails_exits_5_with_rows_durable(tmp_path):
    env = dict(os.environ)
    env[STUB_FAIL_AFTER_ENV] = "1"
    data = tmp_path / "data"
    result = run_cli(
        [
            "--once",
            "--fixture-dir",
            str(FIXTURE_DIR),
            "--csv",
            str(SMOKE_CSV),
            "--data-dir",
            str(data),
            "--at",
            "2026-10-19T13:00:00Z",
        ],
        tmp_path,
        env,
    )
    assert result.returncode == 5
    assert "required scan work failed this run" in result.stderr
    con = sqlite3.connect(str(data / "state.sqlite"))
    sent = {row[0] for row in con.execute("SELECT id FROM sent")}
    pending = {row[0] for row in con.execute("SELECT id FROM pending")}
    con.close()
    assert len(sent) == 1
    assert len(pending) == 3
    assert sent.isdisjoint(pending)


DUP_CSV = (
    "max_price,friendly_name,search_query\n"
    "30,grigri plush,grigri plush\n"
    "60,grigri plush alt,grigri plush\n"
)


def test_duplicate_csv_rows_fetch_query_once(tmp_path):
    csv = tmp_path / "twice.csv"
    csv.write_text(DUP_CSV, encoding="utf-8")
    data = tmp_path / "data"
    result = run_cli(
        [
            "--once",
            "--dry-run",
            "--fixture-dir",
            str(FIXTURE_DIR),
            "--csv",
            str(csv),
            "--data-dir",
            str(data),
        ],
        tmp_path,
    )
    assert result.returncode == 0
    # Same query on two rows: one search fetched, one alert emitted.
    assert result.stdout.count("DRY-RUN ALERT:") == 1


def test_duplicate_csv_rows_log_fetch_once_in_scan(tmp_path):
    env = dict(os.environ)
    csv = tmp_path / "twice.csv"
    csv.write_text(DUP_CSV, encoding="utf-8")
    data = tmp_path / "data"
    result = run_cli(
        [
            "--once",
            "--fixture-dir",
            str(FIXTURE_DIR),
            "--csv",
            str(csv),
            "--data-dir",
            str(data),
            "--at",
            "2026-10-19T13:00:00Z",
        ],
        tmp_path,
        env,
    )
    assert result.returncode == 0
    assert "duplicate CSV row for query 'grigri plush'" in result.stderr
    assert "fetched once" in result.stderr
    con = sqlite3.connect(str(data / "state.sqlite"))
    sent = {row[0] for row in con.execute("SELECT id FROM sent")}
    con.close()
    assert len(sent) == 1


def test_each_data_root_owns_its_logs(tmp_path):
    for name in ("root-a", "root-b"):
        data = tmp_path / name
        result = run_cli(
            [
                "--once",
                "--dry-run",
                "--fixture-dir",
                str(FIXTURE_DIR),
                "--csv",
                str(SMOKE_CSV),
                "--data-dir",
                str(data),
            ],
            tmp_path,
        )
        assert result.returncode == 0
    first = tmp_path / "root-a"
    second = tmp_path / "root-b"
    # Logs live under each --data-dir root, never cwd-relative (CR14).
    assert (first / "logs" / "bot.log").is_file()
    assert (second / "logs" / "bot.log").is_file()
    assert not (tmp_path / "logs").exists()
    log_text = (first / "logs" / "bot.log").read_text(encoding="utf-8")
    assert "no writes, no sends" in log_text


def test_setup_logging_is_handler_idempotent(tmp_path):
    from check_for_ebay_deals import APP_HANDLER_TAG, setup_logging

    data = tmp_path / "data"
    for _ in range(3):
        logger = setup_logging(str(data))
    tagged = [h for h in logger.handlers if getattr(h, APP_HANDLER_TAG, False)]
    assert len(tagged) == 2  # console + file, stable across invocations
    assert (data / "logs" / "bot.log").is_file()
    for handler in list(logger.handlers):
        if getattr(handler, APP_HANDLER_TAG, False):
            logger.removeHandler(handler)
            handler.close()
