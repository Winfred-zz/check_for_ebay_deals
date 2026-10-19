from datetime import datetime, timezone

import pytest

from clock import is_quiet


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(1, 59, False), (2, 0, True), (11, 59, True), (12, 0, False)],
)
def test_quiet_window_boundaries(
    hour: int, minute: int, expected: bool
) -> None:
    moment = datetime(2026, 10, 19, hour, minute, tzinfo=timezone.utc)
    assert is_quiet(moment) is expected


def test_naive_clock_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _ = is_quiet(datetime(2026, 10, 19, 7))
