"""Configuration loading and validation.

All startup validation lives here: no filesystem reads, signal
registration, logging setup, or environment parsing happen at import
time anywhere in the project (F15/F16/F26). Paths resolve from an
explicit project root, overridable for deployment and offline smoke
runs. Errors are ConfigError with actionable, non-secret messages.
"""

from __future__ import annotations

import csv
import os
import re
from decimal import Decimal, InvalidOperation
from typing import TypedDict

CHANNELID_RE = re.compile(r"^[0-9]{1,25}$")
SEARCH_HEADERS = ("max_price", "friendly_name", "search_query")


class SearchRow(TypedDict):
    """One validated CSV search row; max_price stays a string ceiling."""
    search_query: str
    friendly_name: str
    max_price: str


class EnvValues(TypedDict):
    """Validated secrets/env: token value never printed, channel is numeric."""
    discord_token: str
    channel_id: int


class ConfigError(Exception):
    """Actionable startup configuration failure; carries no secrets."""


def _validate_ceiling(raw: str) -> None:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ConfigError(
            "max_price must be a plain decimal number, e.g. '30' or '19.99'"
        ) from None
    if not value.is_finite() or value < 0:
        raise ConfigError("max_price must be a finite, non-negative amount")


def load_env(env_file: str | None) -> EnvValues:
    """Return validated {discord_token, channel_id}.

    Values come from the process environment (preferred) or an optional
    explicit .env file. DISCORD_TOKEN presence is checked, never echoed.
    """
    values: dict[str, str] = {}
    if env_file:
        from dotenv import dotenv_values

        values = {key: val for key, val in dotenv_values(env_file).items() if val}
    token = os.environ.get("DISCORD_TOKEN") or values.get("DISCORD_TOKEN")
    channel = os.environ.get("CHANNELID") or values.get("CHANNELID")
    if not token:
        raise ConfigError(
            "DISCORD_TOKEN is missing. Set it in the environment or in an .env file "
            + "(template: examples/.env.example). The token value is never printed."
        )
    if channel is None:
        raise ConfigError(
            "CHANNELID is missing. Set it in the environment or .env file "
            + "(numeric Discord channel ID; see examples/.env.example)."
        )
    if not CHANNELID_RE.match(channel):
        raise ConfigError(
            "CHANNELID must be a numeric Discord channel ID (up to 25 digits)."
        )
    return {"discord_token": token, "channel_id": int(channel)}


def load_search_rows(csv_path: str) -> list[SearchRow]:
    """Read and fully validate the search CSV before any scan or send.

    A malformed row aborts startup (configuration error), so a scan can
    never persist some search results and then die on a bad row (F16).
    """
    if not os.path.isfile(csv_path):
        raise ConfigError(f"search CSV not found at {csv_path}")
    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing = set(SEARCH_HEADERS) - set(reader.fieldnames or [])
            if missing:
                raise ConfigError(
                    "search CSV missing required headers: "
                    + ", ".join(sorted(missing))
                    + " (template: examples/ebay_deals_to_check.csv)"
                )
            rows: list[SearchRow] = []
            for line_no, row in enumerate(reader, start=2):
                query = (row["search_query"] or "").strip()
                name = (row["friendly_name"] or "").strip()
                price = (row["max_price"] or "").strip()
                if not query:
                    raise ConfigError(f"search CSV line {line_no}: blank search_query")
                if not name:
                    raise ConfigError(f"search CSV line {line_no}: blank friendly_name")
                if not price:
                    raise ConfigError(f"search CSV line {line_no}: blank max_price")
                _validate_ceiling(price)
                rows.append(
                    {
                        "search_query": query,
                        "friendly_name": name,
                        "max_price": price,
                    }
                )
    except UnicodeError as exc:
        raise ConfigError(f"search CSV is not valid UTF-8 text: {exc}") from exc
    return rows


def ensure_writable_dir(path: str) -> str:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"cannot create data directory: {exc}") from exc
    if not (os.access(path, os.W_OK) and os.access(path, os.X_OK)):
        raise ConfigError(f"directory exists but is not writable: {path}")
    return path
