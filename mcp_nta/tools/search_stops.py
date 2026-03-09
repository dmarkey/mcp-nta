"""search_stops — find stops by name."""

from __future__ import annotations

from ..static_data import StaticDataManager


async def search_stops(static: StaticDataManager, query: str, limit: int = 5) -> str:
    await static.ensure_loaded()
    stops = static.search_stops(query, limit)
    if not stops:
        return f'No stops found matching "{query}".'

    lines = [f'Found {len(stops)} stop(s) matching "{query}":\n']
    for i, stop in enumerate(stops, 1):
        loc = stop.street or f"{stop.latitude:.5f}, {stop.longitude:.5f}"
        routes = ", ".join(stop.routes_served) if stop.routes_served else "none listed"
        lines.append(f"{i}. {stop.name} (stop {stop.stop_id}) — {loc}")
        lines.append(f"   Routes: {routes}\n")
    return "\n".join(lines)
