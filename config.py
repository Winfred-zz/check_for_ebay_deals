"""Configuration loading and validation.

All startup validation lives here: no filesystem reads, signal
registration, logging setup, or environment parsing happen at import
time anywhere in the project (F15/F16/F26). Paths resolve from an
explicit project root, overridable for deployment and offline smoke
runs. Errors are ConfigError with actionable, non-secret messages.
"""

from __future__ import annotations

import csv
import json
import os
import re
import stat
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TypedDict, cast

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



class EbayCredentials(TypedDict):
    """Production application credentials; never include values in diagnostics."""
    client_id: str
    client_secret: str


def load_ebay_credentials(path: str) -> EbayCredentials:
    """Load owner-only JSON without echoing credential values on failure."""
    source = Path(path)
    try:
        if not source.is_file():
            raise ConfigError(f"eBay credentials file not found: {path}")
        if stat.S_IMODE(source.stat().st_mode) & 0o077:
            raise ConfigError("eBay credentials file must have permissions 0600")
        raw: object = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ConfigError(
            "eBay credentials file is unreadable or invalid JSON"
        ) from None
    if not isinstance(raw, dict):
        raise ConfigError("eBay credentials must be a JSON object")
    raw_object = cast("dict[object, object]", raw)
    client_id = raw_object.get("client_id")
    client_secret = raw_object.get("client_secret")
    if not isinstance(client_id, str) or not client_id.strip():
        raise ConfigError("eBay credentials require a nonempty client_id")
    if not isinstance(client_secret, str) or not client_secret.strip():
        raise ConfigError("eBay credentials require a nonempty client_secret")
    return {"client_id": client_id.strip(), "client_secret": client_secret.strip()}

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

        try:
            raw_values = dotenv_values(env_file)
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"env file unreadable: {exc}") from exc
        values = {key: val for key, val in raw_values.items() if val}
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
            fieldnames = list(reader.fieldnames or [])
            missing = set(SEARCH_HEADERS) - set(fieldnames)
            if missing:
                raise ConfigError(
                    "search CSV missing required headers: "
                    + ", ".join(sorted(missing))
                    + " (template: examples/ebay_deals_to_check.csv)"
                )
            counts = {name: fieldnames.count(name) for name in fieldnames}
            duplicates = sorted(name for name, count in counts.items() if count > 1)
            if duplicates:
                raise ConfigError(
                    "search CSV has duplicate headers: " + ", ".join(duplicates)
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
    except (csv.Error, OSError) as exc:
        raise ConfigError(f"search CSV unreadable at {csv_path}: {exc}") from exc
    if not rows:
        raise ConfigError(
            f"search CSV has headers but no search rows: {csv_path}"
        )
    return rows


def ensure_writable_dir(path: str) -> str:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"cannot create data directory: {exc}") from exc
    if not (os.access(path, os.W_OK) and os.access(path, os.X_OK)):
        raise ConfigError(f"directory exists but is not writable: {path}")
    return path
