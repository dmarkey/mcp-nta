"""MCP server — tool definitions and dispatch."""

from __future__ import annotations

import asyncio
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .realtime import RealtimeClient
from .static_data import StaticDataManager
from .tools.route_stops import get_route_stops
from .tools.search_routes import search_routes
from .tools.search_stops import search_stops
from .tools.service_alerts import get_service_alerts
from .tools.stop_departures import get_stop_departures
from .tools.vehicle_positions import get_vehicle_positions

logger = logging.getLogger(__name__)


async def serve(api_key: str, route_filter: list[str] | None = None, ttl: int = 24 * 60 * 60) -> None:
    """Run the NTA MCP server."""
    static = StaticDataManager(route_filter=route_filter, ttl=ttl)
    realtime = RealtimeClient(api_key)
    server = Server("mcp-nta")

    # Start loading static data in the background
    asyncio.create_task(_background_loader(static))

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="search_stops",
                description="Find Irish public transport stops by name. Returns stop IDs, locations, and routes served.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search term, e.g. 'Oaktree Green', 'O'Connell Street'",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 5)",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="search_routes",
                description="Find Irish public transport routes by number or name. Returns route details and agency.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Route number or name, e.g. '37', 'DART', 'Green Line'",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 5)",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="get_stop_departures",
                description="Get upcoming real-time departures from a specific stop, optionally filtered by route.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "stop_id": {
                            "type": "string",
                            "description": "Stop ID (use search_stops to find it)",
                        },
                        "route": {
                            "type": "string",
                            "description": "Filter by route short name, e.g. '37'",
                        },
                        "minutes": {
                            "type": "integer",
                            "description": "Time window in minutes (default 60, max 120)",
                            "default": 60,
                        },
                    },
                    "required": ["stop_id"],
                },
            ),
            Tool(
                name="get_vehicle_positions",
                description="Get current positions of public transport vehicles, filtered by route or proximity to a location.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "route": {
                            "type": "string",
                            "description": "Filter by route short name",
                        },
                        "latitude": {
                            "type": "number",
                            "description": "Centre latitude for proximity search",
                        },
                        "longitude": {
                            "type": "number",
                            "description": "Centre longitude for proximity search",
                        },
                        "radius_km": {
                            "type": "number",
                            "description": "Radius in km (default 1.0)",
                            "default": 1.0,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 10)",
                            "default": 10,
                        },
                    },
                },
            ),
            Tool(
                name="get_service_alerts",
                description="Get active service alerts for Irish public transport, optionally filtered by route or stop.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "route": {
                            "type": "string",
                            "description": "Filter by route short name",
                        },
                        "stop_id": {
                            "type": "string",
                            "description": "Filter by stop ID",
                        },
                    },
                },
            ),
            Tool(
                name="get_route_stops",
                description="List all stops on a given route in order.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "route": {
                            "type": "string",
                            "description": "Route short name, e.g. '37'",
                        },
                        "direction": {
                            "type": "string",
                            "description": "'inbound' or 'outbound' (default: both)",
                            "enum": ["inbound", "outbound"],
                        },
                    },
                    "required": ["route"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            result = await _dispatch(static, realtime, name, arguments)
        except Exception as e:
            logger.exception("Tool %s failed", name)
            result = f"Error: {e}"
        return [TextContent(type="text", text=result)]

    options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, options, raise_exceptions=True)


async def _dispatch(
    static: StaticDataManager,
    realtime: RealtimeClient,
    name: str,
    args: dict,
) -> str:
    match name:
        case "search_stops":
            return await search_stops(static, args["query"], args.get("limit", 5))
        case "search_routes":
            return await search_routes(static, args["query"], args.get("limit", 5))
        case "get_stop_departures":
            return await get_stop_departures(
                static,
                realtime,
                args["stop_id"],
                args.get("route"),
                min(args.get("minutes", 60), 120),
            )
        case "get_vehicle_positions":
            return await get_vehicle_positions(
                static,
                realtime,
                args.get("route"),
                args.get("latitude"),
                args.get("longitude"),
                args.get("radius_km", 1.0),
                args.get("limit", 10),
            )
        case "get_service_alerts":
            return await get_service_alerts(
                static,
                realtime,
                args.get("route"),
                args.get("stop_id"),
            )
        case "get_route_stops":
            return await get_route_stops(
                static,
                args["route"],
                args.get("direction"),
            )
        case _:
            return f"Unknown tool: {name}"


async def _background_loader(static: StaticDataManager) -> None:
    """Load static data at startup and refresh periodically."""
    while True:
        try:
            await static.ensure_loaded()
        except Exception:
            logger.exception("Failed to load/refresh static data")
        await asyncio.sleep(static._ttl)
