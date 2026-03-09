"""search_routes — find routes by number or name."""

from __future__ import annotations

from ..static_data import StaticDataManager


async def search_routes(static: StaticDataManager, query: str, limit: int = 5) -> str:
    await static.ensure_loaded()
    routes = static.search_routes(query, limit)
    if not routes:
        return f'No routes found matching "{query}".'

    lines = [f'Found {len(routes)} route(s) matching "{query}":\n']
    for i, route in enumerate(routes, 1):
        lines.append(
            f"{i}. Route {route.short_name} ({route.agency}) — {route.long_name}"
        )
        lines.append(f"   Type: {route.route_type.capitalize()}\n")
    return "\n".join(lines)
