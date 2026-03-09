"""get_vehicle_positions — current positions of vehicles."""

from __future__ import annotations

from ..models import VehiclePosition
from ..realtime import RealtimeClient
from ..static_data import StaticDataManager
from ..util import haversine_km


async def get_vehicle_positions(  # noqa: PLR0913
    static: StaticDataManager,
    realtime: RealtimeClient,
    route: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_km: float = 1.0,
    limit: int = 10,
) -> str:
    if not route and (latitude is None or longitude is None):
        return "Error: at least one of 'route' or 'latitude'/'longitude' is required."

    await static.ensure_loaded()

    route_ids: set[str] | None = None
    if route:
        route_ids = set(static.get_route_ids_by_short_name(route))
        if not route_ids:
            return f'Route "{route}" not found.'

    feed = await realtime.get_vehicles()
    positions: list[VehiclePosition] = []

    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        v = entity.vehicle
        trip_id = v.trip.trip_id if v.HasField("trip") else ""
        trip_route_id = static.resolve_trip_route_id(trip_id) if trip_id else None

        if route_ids and trip_route_id not in route_ids:
            continue

        vlat = v.position.latitude
        vlon = v.position.longitude

        if latitude is not None and longitude is not None:
            dist = haversine_km(latitude, longitude, vlat, vlon)
            if dist > radius_km:
                continue

        route_name, headsign = static.resolve_trip(trip_id) if trip_id else (None, None)

        speed = v.position.speed * 3.6 if v.position.speed else None

        # Find nearest stop
        nearest = static.find_nearest_stop_name(vlat, vlon)

        positions.append(
            VehiclePosition(
                route=route_name or "?",
                destination=headsign or "Unknown",
                latitude=vlat,
                longitude=vlon,
                speed_kmh=round(speed, 1) if speed is not None else None,
                bearing=v.position.bearing if v.position.bearing else None,
                near=nearest,
            )
        )

    positions = positions[:limit]

    if not positions:
        filter_desc = f" on route {route}" if route else " in the specified area"
        return f"No vehicles found{filter_desc}."

    filter_desc = f" on route {route}" if route else ""
    lines = [f"{len(positions)} vehicle(s) found{filter_desc}:\n"]
    for i, vp in enumerate(positions, 1):
        speed_str = f"{vp.speed_kmh} km/h" if vp.speed_kmh else "stopped"
        lines.append(
            f"{i}. {vp.route} -> {vp.destination} | Near {vp.near} | Speed: {speed_str}"
        )
    return "\n".join(lines)


