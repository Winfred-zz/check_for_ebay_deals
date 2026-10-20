"""Make the application's root modules available to pytest."""

import sys
import os
import socket

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


os.environ["CHECK_FOR_EBAY_DEALS_DISABLE_NETWORK"] = "1"


def _is_local(address: object) -> bool:
    return isinstance(address, tuple) and address[0] in (
        "127.0.0.1",
        "::1",
        "localhost",
    )


@pytest.fixture(autouse=True)
def block_external_connections(monkeypatch):
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex
    original_cc = socket.create_connection

    def connect(sock, address):
        if isinstance(address, tuple) and not _is_local(address):
            raise AssertionError(f"External network forbidden in tests: {address}")
        return original(sock, address)

    def connect_ex(sock, address):
        if isinstance(address, tuple) and not _is_local(address):
            raise AssertionError(f"External network forbidden in tests: {address}")
        return original_ex(sock, address)

    def create_connection(address, *args, **kwargs):
        if isinstance(address, tuple) and not _is_local(address):
            raise AssertionError(f"External network forbidden in tests: {address}")
        return original_cc(address, *args, **kwargs)

    _ = monkeypatch.setattr(socket.socket, "connect", connect)
    _ = monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    _ = monkeypatch.setattr(socket, "create_connection", create_connection)
