"""Make the application's root modules available to pytest."""

import sys
import os
import socket

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


os.environ["CHECK_FOR_EBAY_DEALS_DISABLE_NETWORK"] = "1"


@pytest.fixture(autouse=True)
def block_external_connections(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in (
            "127.0.0.1", "::1", "localhost"
        ):
            raise AssertionError(f"External network forbidden in tests: {address}")
        return original(sock, address)

    _ = monkeypatch.setattr(socket.socket, "connect", connect)
