"""Helpers shared between tool modules."""

from __future__ import annotations

import datetime

from ..static_data import StaticDataManager


def no_live_data_message(
    static: StaticDataManager, route: str, route_ids: set[str], suffix: str = ""
) -> str:
    """Explain an empty realtime result, distinguishing the two causes.

    The NTA feed omits some routes entirely, so "no vehicles" can mean either
    that the route isn't running or that it is running unreported.  Checking
    today's schedule tells them apart.
    """
    today = datetime.datetime.now(datetime.timezone.utc).date()
    scheduled = static.count_scheduled_trips(route_ids, today)
    if scheduled:
        return (
            f"No realtime data for route {route}{suffix}. The NTA feed is currently "
            f"publishing no vehicles or trip updates for this route, though "
            f"{scheduled} trip(s) are scheduled today — this is a gap in the "
            f"upstream feed, not an error. Use get_stop_departures for scheduled times."
        )
    return f"No trips scheduled on route {route}{suffix} today."
