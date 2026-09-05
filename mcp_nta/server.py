"""MCP server — FastMCP tool definitions."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from fastmcp.server.lifespan import lifespan

from .realtime import RealtimeClient
from .static_data import StaticDataManager
from .watch import WatchRegistry, make_watch
from .tools.nearby_stops import nearby_stops as _nearby_stops
from .tools.route_stops import get_route_stops as _get_route_stops
from .tools.search_routes import search_routes as _search_routes
from .tools.search_stops import search_stops as _search_stops
from .tools.service_alerts import get_service_alerts as _get_service_alerts
from .tools.stop_departures import get_stop_departures as _get_stop_departures
from .tools.track_route import track_route as _track_route
from .tools.vehicle_positions import get_vehicle_positions as _get_vehicle_positions

logger = logging.getLogger(__name__)

# These are set by create_server() before mcp.run() is called.
_static: StaticDataManager | None = None
_realtime: RealtimeClient | None = None
_watches: WatchRegistry | None = None


def _client_name(ctx: Context) -> str:
    """Stable identity for the connected client, so watch delivery survives a
    reconnect (a new session for the same client takes over its watches)."""
    try:
        return ctx.session.client_params.clientInfo.name or "unknown"  # type: ignore[union-attr]
    except Exception:
        return "unknown"


@lifespan
async def _app_lifespan(server):
    """Start the background data loader and the departure-watch poller."""
    assert _static is not None and _realtime is not None
    global _watches
    _watches = WatchRegistry(_static, _realtime)
    tasks = [
        asyncio.create_task(_background_loader(_static)),
        asyncio.create_task(_watches.run()),
    ]
    try:
        yield {}
    finally:
        for task in tasks:
            task.cancel()


mcp = FastMCP("mcp-nta", lifespan=_app_lifespan)


# -- Tools ----------------------------------------------------------------

@mcp.tool
async def search_transport(
    query: Annotated[str, "Search term, e.g. 'Oaktree Green', 'O'Connell Street', 'Heuston'"],
    limit: Annotated[int, "Max results (default 5)"] = 5,
) -> str:
    """Search for Irish public transport locations by name. Covers bus stops, train stations (Irish Rail, DART), and tram/Luas stops. Returns IDs, locations, and routes served."""
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
async def get_departures(
    stop_id: Annotated[str, "Stop/station ID (use search_transport to find it)"],
    route: Annotated[str | None, "Filter by route short name, e.g. '37'"] = None,
    minutes: Annotated[int, "Time window in minutes (default 60, max 120)"] = 60,
) -> str:
    """Get upcoming real-time departures from a bus stop, train station, or tram stop, optionally filtered by route."""
    assert _static is not None and _realtime is not None
    return await _get_stop_departures(_static, _realtime, stop_id, route, min(minutes, 120))


@mcp.tool
async def track_route(
    route: Annotated[str, "Route short name, e.g. '37'"],
    stop_ids: Annotated[list[str] | None, "Specific stop IDs to show predictions for (optional — omit to show all stops on route)"] = None,
    direction: Annotated[str | None, "Filter by destination keyword, e.g. 'Wilton' or 'Blanchardstown'"] = None,
    minutes: Annotated[int, "Time window in minutes (default 60)"] = 60,
) -> str:
    """Track all active buses/vehicles on a route. Shows vehicle positions and predicted arrivals at specific stops. Use this when single-stop data seems stale — it cross-references vehicle GPS, trip predictions, and multiple stops to give a fuller picture of where buses actually are."""
    assert _static is not None and _realtime is not None
    return await _track_route(_static, _realtime, route, stop_ids, direction, min(minutes, 120))


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
    """Get active service alerts for Irish public transport, optionally filtered by route or stop.

    Note: NTA's GTFS-Realtime feed does not currently publish service alerts, so
    this usually reports that alerts are unavailable. An empty result is not a
    confirmation that services are running normally.
    """
    assert _static is not None and _realtime is not None
    return await _get_service_alerts(_static, _realtime, route, stop_id)


@mcp.tool
async def get_route_transport(
    route: Annotated[str, "Route short name, e.g. '37'"],
    direction: Annotated[Literal["inbound", "outbound"] | None, "'inbound' or 'outbound' (default: both)"] = None,
) -> str:
    """List all bus stops, train stations, or tram stops on a given route in order."""
    assert _static is not None
    return await _get_route_stops(_static, route, direction)


@mcp.tool
async def nearby_transport(
    latitude: Annotated[float, "Latitude of the location"],
    longitude: Annotated[float, "Longitude of the location"],
    route: Annotated[str | None, "Filter by route short name, e.g. '37'"] = None,
    radius_km: Annotated[float | None, "Only return results within this radius in km"] = None,
    transport_type: Annotated[Literal["bus", "rail", "tram"] | None, "Filter by mode: 'bus', 'rail' (Irish Rail/DART), or 'tram' (Luas). Default: all types."] = None,
    limit: Annotated[int, "Max results (default 10)"] = 10,
) -> str:
    """Find the nearest bus stops, train stations (Irish Rail, DART), and tram/Luas stops to a given lat/lon. Use this when a user mentions a place or location. Optionally filter by route, radius, or transport type. Returns details, routes served, and distance."""
    assert _static is not None
    return await _nearby_stops(_static, latitude, longitude, limit, route, radius_km, transport_type)


# -- Departure watches -----------------------------------------------------

@mcp.tool
async def subscribe_events(ctx: Context) -> str:
    """Register this session as the delivery channel for server-push events.

    Call once after connecting (and again after reconnecting; repeat calls are
    harmless). Departure-watch notifications (see watch_departures) are pushed
    as MCP log notifications with logger="events". Clients that subscribe on
    connect keep receiving events across reconnects. Requires a persistent
    session (stdio, or HTTP with NTA_STATELESS=false).
    """
    assert _watches is not None
    _watches.touch_session(_client_name(ctx), ctx.session)
    return ("Subscribed: departure-watch events for this client will be "
            "delivered to this session as log notifications (logger='events').")


@mcp.tool
async def watch_departures(
    ctx: Context,
    stop_id: Annotated[str, "Stop/station ID to watch (use search_transport to find it)"],
    route: Annotated[str | None, "Route short name to watch, e.g. '37'"] = None,
    direction: Annotated[str | None, "Only match this direction — a substring of the destination/headsign, e.g. 'Wilton Terrace'"] = None,
    lead_minutes: Annotated[int, "Notify when a matching departure is this many minutes away (1-120, default 20)"] = 20,
    window: Annotated[str | None, "Only notify during this local time window, 'HH:MM-HH:MM', e.g. '08:00-12:00'. Omit for any time."] = None,
    days: Annotated[str | None, "Only notify on these days, e.g. 'weekdays', 'weekends', or 'mon,wed,fri'. Omit for every day."] = None,
) -> str:
    """Get a push notification when a bus/train is a set number of minutes from a stop.

    Registers a standing watch and returns immediately. When a matching departure
    is within lead_minutes of the stop (optionally only during a daily time
    window and on certain days), the server pushes an MCP log notification
    (logger="events", payload {"event":"bus_approaching", ...}) to this session
    — each matching trip fires once. Requires a persistent session (stdio or
    stateful HTTP). Use list_watches to review and cancel_watch to stop one.
    """
    assert _static is not None and _watches is not None
    await _static.ensure_loaded()
    try:
        watch = make_watch(
            _static, stop_id, route, direction, lead_minutes, window,
            _client_name(ctx), ctx.session, days,
        )
    except ValueError as exc:
        return f"Could not create watch: {exc}"
    _watches.add(watch)
    return f"Watching: {watch.describe()}. Watch id {watch.id}. I'll notify you here when one is due."


@mcp.tool
async def list_watches(ctx: Context) -> str:
    """List your active departure watches (see watch_departures)."""
    assert _watches is not None
    _watches.touch_session(_client_name(ctx), ctx.session)
    watches = _watches.list_for(_client_name(ctx))
    if not watches:
        return "No active watches. Create one with watch_departures."
    lines = [f"{len(watches)} active watch(es):"]
    for w in watches:
        lines.append(f"  [{w.id}] {w.describe()}")
    return "\n".join(lines)


@mcp.tool
async def cancel_watch(
    ctx: Context,
    watch_id: Annotated[str, "The watch id returned by watch_departures / list_watches"],
) -> str:
    """Cancel a departure watch by id."""
    assert _watches is not None
    watch = _watches.remove(watch_id)
    if watch is None:
        return f"No watch with id {watch_id!r}."
    return f"Cancelled watch {watch_id}: {watch.describe()}."


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
