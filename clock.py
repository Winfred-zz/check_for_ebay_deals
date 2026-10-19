"""Quiet-window evaluation on an injected, timezone-aware clock.

Approved contract (design.md): alerts are suppressed from 02:00 inclusive
until 12:00 exclusive in the configured timezone. 12:00 allows delivery.
UTC never observes DST, but the predicate is zone-agnostic and takes a
tz-aware datetime, so DST-sensitive zones (e.g. Europe/London) evaluate
on wall-clock hour, which is what "quiet hours" must mean for users.
"""

from datetime import datetime

QUIET_START_HOUR = 2
QUIET_END_HOUR = 12


def is_quiet(now: datetime) -> bool:
    """True when alert delivery must be suppressed at ``now``.

    ``now`` must be timezone-aware; naive datetimes are rejected because
    the contract is explicit about the zone (no host-local guesswork).
    """
    if now.tzinfo is None:
        raise ValueError("quiet-window clock must be timezone-aware (e.g. UTC)")
    hour = now.hour
    return QUIET_START_HOUR <= hour < QUIET_END_HOUR


def utc_now() -> datetime:
    """Explicit UTC clock; the approved timezone for this project."""
    from datetime import timezone

    return datetime.now(tz=timezone.utc)
