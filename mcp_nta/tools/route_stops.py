"""get_route_stops — list all stops on a route in order."""

from __future__ import annotations

from ..static_data import StaticDataManager


async def get_route_stops(
    static: StaticDataManager,
    route: str,
    direction: str | None = None,
) -> str:
    await static.ensure_loaded()

    route_obj = static.get_route_by_short_name(route)
    if not route_obj:
        return f'Route "{route}" not found.'

    stops = static.get_stops_for_route(route_obj.route_id)
    if not stops:
        return f"No stops found for route {route}."

    desc = route_obj.long_name or route
    lines = [f"Route {route_obj.short_name} — {desc} ({len(stops)} stops):\n"]
    for i, stop in enumerate(stops, 1):
        lines.append(f"{i}. {stop.name} (stop {stop.stop_id})")
    return "\n".join(lines)
