"""Discord gateway supervision + exit mapping (CR01), driven offline.

The live branch of lifecycle() logs in, then runs the gateway as a
supervised background task (reconnect=True) with a bounded readiness
wait. These tests inject a fake discord.Client so the exact failure
modes map to the documented exit codes with no network:

- token rejection  -> LoginRejected -> exit 4
- gateway never ready within the window -> RuntimeError -> exit 1

main() logs the failure to bot.log under --data-dir before returning,
which is the assertion target (the logger flushes every record).
"""

import asyncio
from pathlib import Path

import discord
import pytest

import check_for_ebay_deals as cfd

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CSV = ROOT / "tests" / "fixtures" / "offline-scan" / "smoke.csv"
DISABLE_NETWORK_ENV = "CHECK_FOR_EBAY_DEALS_DISABLE_NETWORK"


class RejectedClient:
    """Stands in for discord.Client; login always fails like a bad token."""

    def __init__(self, *, intents: discord.Intents) -> None:
        self.intents = intents

    async def login(self, token: str) -> None:
        del token
        raise discord.LoginFailure("invalid token")

    async def close(self) -> None:
        return None


class NeverReadyClient(RejectedClient):
    """Login succeeds; the supervised connect/readiness never completes."""

    async def login(self, token: str) -> None:
        del token

    async def connect(self, *, reconnect: bool = True) -> None:
        del reconnect
        await asyncio.sleep(3600)  # supervised task, never completes

    async def wait_until_ready(self) -> None:
        await asyncio.sleep(3600)  # readiness never arrives


def live_args(data: Path) -> list[str]:
    return ["--once", "--csv", str(FIXTURE_CSV), "--data-dir", str(data)]


def log_text(data: Path) -> str:
    return (data / "logs" / "bot.log").read_text(encoding="utf-8")


def prepare_live(monkeypatch: pytest.MonkeyPatch, client_cls: type) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "fake-token-never-printed")
    monkeypatch.setenv("CHANNELID", "123456789012345678")
    monkeypatch.delenv(DISABLE_NETWORK_ENV, raising=False)
    monkeypatch.setattr(discord, "Client", lambda **kw: client_cls(**kw))
    monkeypatch.setattr(
        cfd.config, "load_ebay_credentials",
        lambda path: {"client_id": "fake-id", "client_secret": "fake-secret"},
    )


def test_login_rejection_maps_to_exit_4(tmp_path: Path, monkeypatch):
    prepare_live(monkeypatch, RejectedClient)
    data = tmp_path / "data"
    assert cfd.main(live_args(data)) == 4
    text = log_text(data)
    assert "Discord login rejected: invalid token" in text
    assert "Traceback" not in text  # login failure is a mapped code, not a crash


def test_gateway_never_ready_maps_to_exit_1(tmp_path: Path, monkeypatch):
    prepare_live(monkeypatch, NeverReadyClient)
    monkeypatch.setattr(cfd, "READY_TIMEOUT_SECONDS", 0.1)  # fast readiness window
    data = tmp_path / "data"
    assert cfd.main(live_args(data)) == 1
    text = log_text(data)
    assert "Discord gateway did not become ready" in text
    assert "Discord gateway connected" not in text  # never falsely "ready"
