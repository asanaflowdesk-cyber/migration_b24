from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Kazakhstan uses UTC+5; DeskFlow business hours are evaluated in Kazakhstan time.
KZ_TIMEZONE = timezone(timedelta(hours=5))

ACTIVE_START = (8, 0)
QUIET_START = (22, 1)


def is_active_poll_window(now: datetime | None = None) -> bool:
    """Return True from 08:00 through 22:00:59 Kazakhstan time."""
    current = now or datetime.now(KZ_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KZ_TIMEZONE)
    else:
        current = current.astimezone(KZ_TIMEZONE)

    minute_of_day = current.hour * 60 + current.minute
    active_start = ACTIVE_START[0] * 60 + ACTIVE_START[1]
    quiet_start = QUIET_START[0] * 60 + QUIET_START[1]
    return active_start <= minute_of_day < quiet_start


def poll_interval_seconds(
    now: datetime | None = None,
    *,
    active_seconds: float = 1.0,
    quiet_seconds: float = 60.0,
) -> float:
    """Choose the empty-queue polling interval for the current Kazakhstan time."""
    return active_seconds if is_active_poll_window(now) else quiet_seconds
