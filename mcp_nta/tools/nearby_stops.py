"""nearby_stops — find the nearest stops to a location."""

from __future__ import annotations

from ..static_data import StaticDataManager
from ..util import haversine_km


async def nearby_stops(
    static: StaticDataManager,
    latitude: float,
    longitude: float,
    limit: int = 10,
    route: str | None = None,
) -> str:
    await static.ensure_loaded()
    stops = static.find_nearest_stops(latitude, longitude, limit, route_short_name=route)
    if not stops:
        msg = f"No stops found near ({latitude}, {longitude})"
        if route:
            msg += f" on route {route}"
        return msg + "."

    header = f"Nearest {len(stops)} stop(s) to ({latitude:.5f}, {longitude:.5f})"
    if route:
        header += f" on route {route}"
    lines = [header + ":\n"]
    for i, stop in enumerate(stops, 1):
        dist = haversine_km(latitude, longitude, stop.latitude, stop.longitude)
        loc = stop.street or f"{stop.latitude:.5f}, {stop.longitude:.5f}"
        routes = ", ".join(stop.routes_served) if stop.routes_served else "none listed"
        lines.append(f"{i}. {stop.name} (stop {stop.stop_id}) — {loc} — {dist:.2f} km away")
        lines.append(f"   Routes: {routes}\n")
    return "\n".join(lines)
