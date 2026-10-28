"""NYSE sessions and the order execution window.

exchange_calendars knows holidays and half-days (it takes ~2.5 s to import, so
it is imported only when an order is actually being checked).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Limits

NEW_YORK = ZoneInfo("America/New_York")

SessionBounds = Callable[[date], tuple[datetime, datetime] | None]


def session_bounds(day: date) -> tuple[datetime, datetime] | None:
    """(open, close) of the NYSE session on `day` as aware UTC datetimes, or None if closed."""
    import exchange_calendars as xcals

    calendar = xcals.get_calendar(
        "XNYS", start=(day - timedelta(days=7)).isoformat(), end=(day + timedelta(days=7)).isoformat()
    )
    label = day.isoformat()
    if not calendar.is_session(label):
        return None
    return calendar.session_open(label).to_pydatetime(), calendar.session_close(label).to_pydatetime()


def execution_window(
    day: date, limits: Limits, bounds: SessionBounds = session_bounds
) -> tuple[datetime, datetime] | None:
    """When orders may be placed on `day`: inside both the configured clock window
    and the session, ending `close_buffer` before the close (half-days included)."""
    session = bounds(day)
    if session is None:
        return None
    open_, close = session
    start = max(open_, datetime.combine(day, limits.window_start, NEW_YORK))
    end = min(close - limits.close_buffer, datetime.combine(day, limits.window_end, NEW_YORK))
    if end <= start:
        return None
    return start, end
