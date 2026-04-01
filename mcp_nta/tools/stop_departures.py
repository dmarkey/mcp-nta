"""get_stop_departures — upcoming departures from a stop.

Uses the static GTFS schedule as the base, then overlays real-time delay
data from the GTFS-RT TripUpdates feed. When the realtime feed doesn't
have a prediction for a specific stop, we use the delay from the nearest
upstream stop on the same trip (matching the approach used by tfi-gtfs).
"""

from __future__ import annotations

import datetime

from ..models import Departure
from ..realtime import RealtimeClient
from ..static_data import StaticDataManager
from ..util import delay_text, format_time, relative_time


def _build_live_delays(
    feed, static: StaticDataManager
) -> dict[str, list[tuple[int, int | None, datetime.datetime | None]]]:
    """Parse the TripUpdates feed into a lookup: trip_id -> sorted list of
    (stop_sequence, delay_seconds | None, arrival_datetime | None).

    For each stop_time_update we store either:
      - An absolute arrival time (if arrival.time > 0)
      - A delay in seconds (if only delay is provided)
    """
    # https://developers.google.com/transit/gtfs-realtime/reference#enum-schedulerelationship
    STOP_SCHEDULED = 0

    delays: dict[str, list[tuple[int, int | None, datetime.datetime | None]]] = {}
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        trip_id = tu.trip.trip_id
        trip_delays: list[tuple[int, int | None, datetime.datetime | None]] = []

        for stu in tu.stop_time_update:
            if stu.schedule_relationship != STOP_SCHEDULED:
                continue

            seq = stu.stop_sequence
            arrival_time_abs: datetime.datetime | None = None
            delay: int | None = None

            if stu.HasField("arrival") and stu.arrival.time > 0:
                arrival_time_abs = datetime.datetime.fromtimestamp(
                    stu.arrival.time, tz=datetime.timezone.utc
                )
            elif stu.HasField("arrival") and stu.arrival.delay:
                delay = stu.arrival.delay
                # Filter out nonsensical delays (> 1 week or large negative)
                if delay < -60 * 60 * 24 * 7:
                    continue
            elif stu.HasField("departure") and stu.departure.delay:
                delay = stu.departure.delay
                if delay < -60 * 60 * 24 * 7:
                    continue

            trip_delays.append((seq, delay, arrival_time_abs))

        if trip_delays:
            trip_delays.sort(key=lambda x: x[0])
            delays[trip_id] = trip_delays

    return delays


def _get_live_delay(
    trip_delays: list[tuple[int, int | None, datetime.datetime | None]],
    stop_sequence: int,
) -> tuple[int | None, datetime.datetime | None]:
    """Binary search for the delay at or before the given stop_sequence.

    Returns (delay_seconds, absolute_arrival) — one or both may be None.
    """
    left, right = 0, len(trip_delays) - 1
    while left <= right:
        mid = (left + right) // 2
        if trip_delays[mid][0] < stop_sequence:
            left = mid + 1
        elif trip_delays[mid][0] > stop_sequence:
            right = mid - 1
        else:
            # Exact match
            return trip_delays[mid][1], trip_delays[mid][2]

    # No exact match — use the closest preceding stop
    if left == 0:
        return None, None
    return trip_delays[left - 1][1], None  # Don't use abs time from a different stop


async def get_stop_departures(
    static: StaticDataManager,
    realtime: RealtimeClient,
    stop_id: str,
    route: str | None = None,
    minutes: int = 60,
) -> str:
    await static.ensure_loaded()
    stop = static.get_stop(stop_id)
    stop_name = stop.name if stop else stop_id

    route_ids: set[str] | None = None
    if route:
        route_ids = set(static.get_route_ids_by_short_name(route))
        if not route_ids:
            return f'Route "{route}" not found.'

    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Get scheduled arrivals from static data
    scheduled = static.get_scheduled_stop_times(stop_id, now, minutes, route_ids)

    # 2. Get live delays from realtime feed
    feed = await realtime.get_trip_updates()
    live_delays = _build_live_delays(feed, static)

    # 3. Combine: for each scheduled arrival, overlay live delay
    departures: list[Departure] = []
    seen_trips: set[str] = set()

    for entry, scheduled_dt in scheduled:
        if entry.trip_id in seen_trips:
            continue
        seen_trips.add(entry.trip_id)

        route_name, headsign = static.resolve_trip(entry.trip_id)

        delay_seconds = 0
        predicted_dt = scheduled_dt
        status = "scheduled"

        trip_delays = live_delays.get(entry.trip_id)
        if trip_delays:
            delay, abs_arrival = _get_live_delay(trip_delays, entry.stop_sequence)
            if abs_arrival is not None:
                predicted_dt = abs_arrival
                delay_seconds = int((predicted_dt - scheduled_dt).total_seconds())
                status = "on time" if abs(delay_seconds) < 60 else ("late" if delay_seconds > 0 else "early")
            elif delay is not None:
                delay_seconds = delay
                predicted_dt = scheduled_dt + datetime.timedelta(seconds=delay)
                status = "on time" if abs(delay) < 60 else ("late" if delay > 0 else "early")
            else:
                status = "scheduled"

        # Skip if already departed
        if predicted_dt < now - datetime.timedelta(minutes=1):
            continue

        departures.append(
            Departure(
                route=route_name or "?",
                destination=headsign or "Unknown",
                scheduled=scheduled_dt,
                predicted=predicted_dt,
                delay_seconds=delay_seconds,
                status=status,
            )
        )

    departures.sort(key=lambda d: d.predicted)
    departures = departures[:20]

    if not departures:
        route_info = f" for route {route}" if route else ""
        return f"No upcoming departures from {stop_name} (stop {stop_id}){route_info} in the next {minutes} minutes."

    # Feed freshness indicator
    feed_age = realtime.get_feed_age(feed)
    if feed_age is not None and feed_age > 120:
        freshness = f"⚠ Real-time data is {feed_age // 60}m {feed_age % 60}s old — may be stale"
    elif feed_age is not None:
        freshness = f"Real-time data age: {feed_age}s"
    else:
        freshness = "Real-time feed timestamp unavailable"

    live_count = sum(1 for d in departures if d.status != "scheduled")
    sched_count = len(departures) - live_count

    route_info = f" for route {route}" if route else ""
    lines = [f"Upcoming departures from {stop_name} (stop {stop_id}){route_info}:"]
    lines.append(f"[{freshness} | {live_count} live, {sched_count} scheduled-only]\n")
    for i, dep in enumerate(departures, 1):
        pred_str = format_time(dep.predicted)
        rel = relative_time(dep.predicted, now)
        if dep.status == "on time":
            timing = "on time"
        elif dep.status == "scheduled":
            timing = "scheduled (no live data)"
        else:
            sched_str = format_time(dep.scheduled)
            timing = f"scheduled {sched_str}, {delay_text(dep.delay_seconds)}"
        lines.append(f"{i}. {dep.route} -> {dep.destination} | Due: {pred_str} ({rel}) — {timing}")
    return "\n".join(lines)
