"""Utility functions: haversine, time formatting, text helpers."""

from __future__ import annotations

import math
from datetime import datetime, timezone


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in km between two points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def format_time(dt: datetime) -> str:
    """Format a datetime as HH:MM."""
    return dt.strftime("%H:%M")


def relative_time(dt: datetime, now: datetime | None = None) -> str:
    """Return a human string like 'in 4 min' or 'due now'."""
    if now is None:
        now = datetime.now(timezone.utc)
    diff = (dt - now).total_seconds()
    minutes = int(diff / 60)
    if minutes <= 0:
        return "due now"
    if minutes == 1:
        return "in 1 min"
    return f"in {minutes} min"


def delay_text(delay_seconds: int) -> str:
    """Return human-readable delay text."""
    if abs(delay_seconds) < 60:
        return "on time"
    minutes = delay_seconds // 60
    if minutes > 0:
        return f"+{minutes} min late"
    return f"{-minutes} min early"


ROUTE_TYPE_MAP = {
    0: "tram",
    1: "metro",
    2: "rail",
    3: "bus",
    4: "ferry",
    5: "cable tram",
    6: "gondola",
    7: "funicular",
    11: "trolleybus",
    12: "monorail",
}


def route_type_name(gtfs_type: int) -> str:
    """Map GTFS route_type int to a human name."""
    return ROUTE_TYPE_MAP.get(gtfs_type, "bus")
