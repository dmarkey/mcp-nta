"""track_route — cross-reference a route across multiple stops and vehicles.

When departure data at a single stop seems stale or unreliable, this tool
gives a fuller picture by showing where each trip on the route currently is:
its real-time position (if vehicle tracking is available), and its predicted
arrival at the stops the user cares about.
"""

from __future__ import annotations

import datetime

from ..models import Stop
from ..realtime import RealtimeClient
from ..static_data import StaticDataManager
from ..util import format_time, haversine_km, relative_time


async def track_route(
    static: StaticDataManager,
    realtime: RealtimeClient,
    route: str,
    stop_ids: list[str] | None = None,
    direction: str | None = None,
    minutes: int = 60,
) -> str:
    """Track all active trips on a route, showing vehicle positions and
    predicted arrivals at specified stops (or the next few stops on the route).
    """
    await static.ensure_loaded()

    route_ids = set(static.get_route_ids_by_short_name(route))
    if not route_ids:
        return f'Route "{route}" not found.'

    now = datetime.datetime.now(datetime.timezone.utc)

    # Resolve target stops
    target_stops: list[Stop] = []
    if stop_ids:
        for sid in stop_ids:
            s = static.get_stop(sid)
            if s:
                target_stops.append(s)
    if not target_stops:
        # Use all stops on the route
        for rid in route_ids:
            target_stops = static.get_stops_for_route(rid)
            if target_stops:
                break

    target_stop_ids = {s.stop_id for s in target_stops}
    stop_name_map = {s.stop_id: s.name for s in target_stops}

    # Fetch both feeds concurrently-ish (they're cached independently)
    trip_feed = await realtime.get_trip_updates()
    vehicle_feed = await realtime.get_vehicles()

    # Feed freshness
    trip_age = realtime.get_feed_age(trip_feed)
    vehicle_age = realtime.get_feed_age(vehicle_feed)

    # Build vehicle position lookup: trip_id -> (lat, lon, speed, nearest_stop_name)
    vehicle_positions: dict[str, tuple[float, float, float | None, str]] = {}
    for entity in vehicle_feed.entity:
        if not entity.HasField("vehicle"):
            continue
        v = entity.vehicle
        trip_id = v.trip.trip_id if v.HasField("trip") else ""
        if not trip_id:
            continue
        trip_route_id = static.resolve_trip_route_id(trip_id)
        if trip_route_id not in route_ids:
            continue
        vlat = v.position.latitude
        vlon = v.position.longitude
        speed = v.position.speed * 3.6 if v.position.speed else None
        nearest = static.find_nearest_stop_name(vlat, vlon)
        vehicle_positions[trip_id] = (vlat, vlon, speed, nearest)

    # Build trip update lookup: trip_id -> list of (stop_id, stop_sequence, predicted_dt)
    # We need stop_id mapping from static data
    trip_stop_predictions: dict[str, list[tuple[str, int, datetime.datetime | None, int | None]]] = {}
    for entity in trip_feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        trip_id = tu.trip.trip_id
        trip_route_id = static.resolve_trip_route_id(trip_id)
        if trip_route_id not in route_ids:
            continue

        _, headsign = static.resolve_trip(trip_id)

        predictions: list[tuple[str, int, datetime.datetime | None, int | None]] = []
        for stu in tu.stop_time_update:
            stop_id_rt = stu.stop_id if stu.stop_id else None
            seq = stu.stop_sequence
            arrival_dt: datetime.datetime | None = None
            delay: int | None = None

            if stu.HasField("arrival") and stu.arrival.time > 0:
                arrival_dt = datetime.datetime.fromtimestamp(
                    stu.arrival.time, tz=datetime.timezone.utc
                )
            elif stu.HasField("arrival") and stu.arrival.delay:
                delay = stu.arrival.delay

            predictions.append((stop_id_rt or "", seq, arrival_dt, delay))

        if predictions:
            trip_stop_predictions[trip_id] = predictions

    # All trip IDs active on this route
    all_trip_ids = set(vehicle_positions.keys()) | set(trip_stop_predictions.keys())

    if not all_trip_ids:
        return f"No active trips found on route {route}."

    # Build output for each trip
    trip_summaries: list[tuple[datetime.datetime | None, str]] = []

    for trip_id in sorted(all_trip_ids):
        route_name, headsign = static.resolve_trip(trip_id)
        if direction:
            if headsign and direction.lower() not in headsign.lower():
                continue

        parts: list[str] = []
        parts.append(f"  {route_name or route} -> {headsign or 'Unknown'}")

        # Vehicle position
        vpos = vehicle_positions.get(trip_id)
        if vpos:
            vlat, vlon, speed, nearest = vpos
            speed_str = f"{speed:.0f} km/h" if speed else "stopped"
            parts.append(f"    📍 Vehicle near {nearest} ({speed_str})")

        # Predictions at target stops
        earliest_pred: datetime.datetime | None = None
        preds = trip_stop_predictions.get(trip_id, [])
        target_preds: list[str] = []
        for stop_id_rt, seq, arrival_dt, delay in preds:
            if stop_ids and stop_id_rt not in target_stop_ids:
                continue
            sname = stop_name_map.get(stop_id_rt, stop_id_rt)
            if arrival_dt:
                if arrival_dt < now - datetime.timedelta(minutes=2):
                    continue  # Already passed
                rel = relative_time(arrival_dt, now)
                target_preds.append(f"    → {sname}: {format_time(arrival_dt)} ({rel})")
                if earliest_pred is None or arrival_dt < earliest_pred:
                    earliest_pred = arrival_dt
            elif delay is not None:
                delay_str = f"+{delay // 60}m late" if delay > 60 else (
                    f"{-delay // 60}m early" if delay < -60 else "on time"
                )
                target_preds.append(f"    → {sname}: {delay_str}")

        if not target_preds and not vpos:
            continue  # No useful info for this trip

        if target_preds:
            parts.extend(target_preds[:10])  # Limit per trip
        elif vpos:
            parts.append("    (no arrival predictions available)")

        trip_summaries.append((earliest_pred, "\n".join(parts)))

    # Sort: trips with predictions first (by earliest arrival), then others
    trip_summaries.sort(key=lambda x: (x[0] is None, x[0] or now))

    if not trip_summaries:
        dir_info = f" ({direction})" if direction else ""
        return f"No active trips found on route {route}{dir_info}."

    # Freshness info
    freshness_parts = []
    if trip_age is not None:
        if trip_age > 120:
            freshness_parts.append(f"⚠ trip data {trip_age // 60}m {trip_age % 60}s old")
        else:
            freshness_parts.append(f"trip data {trip_age}s old")
    if vehicle_age is not None:
        if vehicle_age > 120:
            freshness_parts.append(f"⚠ vehicle data {vehicle_age // 60}m {vehicle_age % 60}s old")
        else:
            freshness_parts.append(f"vehicle data {vehicle_age}s old")

    freshness = " | ".join(freshness_parts) if freshness_parts else "feed timestamps unavailable"

    lines = [f"Tracking route {route} — {len(trip_summaries)} active trip(s):"]
    lines.append(f"[{freshness}]\n")
    for _, summary in trip_summaries:
        lines.append(summary)
        lines.append("")

    return "\n".join(lines)
