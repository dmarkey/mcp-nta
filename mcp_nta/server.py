"""MCP server — FastMCP tool definitions."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from fastmcp.server.lifespan import lifespan

from .realtime import RealtimeClient
from .static_data import StaticDataManager
from .tools.nearby_stops import nearby_stops as _nearby_stops
from .tools.route_stops import get_route_stops as _get_route_stops
from .tools.search_routes import search_routes as _search_routes
from .tools.search_stops import search_stops as _search_stops
from .tools.service_alerts import get_service_alerts as _get_service_alerts
from .tools.stop_departures import get_stop_departures as _get_stop_departures
from .tools.vehicle_positions import get_vehicle_positions as _get_vehicle_positions

logger = logging.getLogger(__name__)

# These are set by create_server() before mcp.run() is called.
_static: StaticDataManager | None = None
_realtime: RealtimeClient | None = None


@lifespan
async def _app_lifespan(server):
    """Start the background data loader when the server starts."""
    assert _static is not None
    task = asyncio.create_task(_background_loader(_static))
    try:
        yield {}
    finally:
        task.cancel()


mcp = FastMCP("mcp-nta", lifespan=_app_lifespan)


# -- Tools ----------------------------------------------------------------

@mcp.tool
async def search_stops(
    query: Annotated[str, "Search term, e.g. 'Oaktree Green', 'O'Connell Street'"],
    limit: Annotated[int, "Max results (default 5)"] = 5,
) -> str:
    """Find Irish public transport stops by name. Returns stop IDs, locations, and routes served."""
    assert _static is not None
    return await _search_stops(_static, query, limit)


@mcp.tool
async def search_routes(
    query: Annotated[str, "Route number or name, e.g. '37', 'DART', 'Green Line'"],
    limit: Annotated[int, "Max results (default 5)"] = 5,
) -> str:
    """Find Irish public transport routes by number or name. Returns route details and agency."""
    assert _static is not None
    return await _search_routes(_static, query, limit)


@mcp.tool
async def get_stop_departures(
    stop_id: Annotated[str, "Stop ID (use search_stops to find it)"],
    route: Annotated[str | None, "Filter by route short name, e.g. '37'"] = None,
    minutes: Annotated[int, "Time window in minutes (default 60, max 120)"] = 60,
) -> str:
    """Get upcoming real-time departures from a specific stop, optionally filtered by route."""
    assert _static is not None and _realtime is not None
    return await _get_stop_departures(_static, _realtime, stop_id, route, min(minutes, 120))


@mcp.tool
async def get_vehicle_positions(
    route: Annotated[str | None, "Filter by route short name"] = None,
    latitude: Annotated[float | None, "Centre latitude for proximity search"] = None,
    longitude: Annotated[float | None, "Centre longitude for proximity search"] = None,
    radius_km: Annotated[float, "Radius in km (default 1.0)"] = 1.0,
    limit: Annotated[int, "Max results (default 10)"] = 10,
) -> str:
    """Get current positions of public transport vehicles, filtered by route or proximity to a location."""
    assert _static is not None and _realtime is not None
    return await _get_vehicle_positions(_static, _realtime, route, latitude, longitude, radius_km, limit)


@mcp.tool
async def get_service_alerts(
    route: Annotated[str | None, "Filter by route short name"] = None,
    stop_id: Annotated[str | None, "Filter by stop ID"] = None,
) -> str:
    """Get active service alerts for Irish public transport, optionally filtered by route or stop."""
    assert _static is not None and _realtime is not None
    return await _get_service_alerts(_static, _realtime, route, stop_id)


@mcp.tool
async def get_route_stops(
    route: Annotated[str, "Route short name, e.g. '37'"],
    direction: Annotated[Literal["inbound", "outbound"] | None, "'inbound' or 'outbound' (default: both)"] = None,
) -> str:
    """List all stops on a given route in order."""
    assert _static is not None
    return await _get_route_stops(_static, route, direction)


@mcp.tool
async def nearby_stops(
    latitude: Annotated[float, "Latitude of the location"],
    longitude: Annotated[float, "Longitude of the location"],
    route: Annotated[str | None, "Filter by route short name, e.g. '37'"] = None,
    limit: Annotated[int, "Max results (default 10)"] = 10,
) -> str:
    """Find the nearest public transport stops to a given location. Optionally filter by route. Returns stop details, routes served, and distance."""
    assert _static is not None
    return await _nearby_stops(_static, latitude, longitude, limit, route)


# -- Lifecycle -------------------------------------------------------------

def create_server(
    api_key: str,
    route_filter: list[str] | None = None,
    ttl: int = 24 * 60 * 60,
) -> FastMCP:
    """Initialise shared state and return the configured FastMCP instance."""
    global _static, _realtime
    _static = StaticDataManager(route_filter=route_filter, ttl=ttl)
    _realtime = RealtimeClient(api_key)
    return mcp


async def _background_loader(static: StaticDataManager) -> None:
    """Load static data at startup and refresh periodically."""
    while True:
        try:
            await static.ensure_loaded()
        except Exception:
            logger.exception("Failed to load/refresh static data")
        await asyncio.sleep(static._ttl)
