# mcp-nta

An MCP server for Ireland's National Transport Authority (NTA) public transport data. Provides focused, queryable tools that return small, human-readable results — not raw feed dumps.

Built with [FastMCP](https://github.com/PrefectHQ/fastmcp). Inspired by [ireland-nta-mcp](https://github.com/dmarkey/ireland-nta-mcp) and [tfi-gtfs](https://github.com/seanblanchfield/tfi-gtfs).

## Tools

| Tool | Description |
|------|-------------|
| `search_transport` | Find bus stops, train stations, and tram stops by name. Returns IDs, locations, routes served. |
| `search_routes` | Find routes by number or name. |
| `get_departures` | Real-time departures from a bus stop, train station, or tram stop, filtered by route/time window. |
| `get_vehicle_positions` | Live vehicle positions filtered by route or proximity. |
| `get_service_alerts` | Active service alerts filtered by route or stop. |
| `get_route_transport` | Ordered list of bus stops, train stations, or tram stops on a route. |
| `nearby_transport` | Find the nearest bus stops, train stations, and tram stops to a location. Filter by route, radius, or transport type (`bus`, `rail`, `tram`). |
| `watch_departures` | Get a push notification when a bus/train is N minutes from a stop, optionally only in a daily time window and toward a given direction. |
| `subscribe_events` | Register this session as the delivery channel for watch events. Call on connect. |
| `list_watches` / `cancel_watch` | Review and cancel active departure watches. |

### Departure watches (push notifications)

`watch_departures` registers a standing rule — e.g. *"between 08:00 and 12:00, tell me when a 37 toward Wilton Terrace is 20 minutes from my stop"* — and returns immediately. A background loop polls the same real-time departure logic and, when a matching departure crosses the lead time, pushes an **MCP log notification** to your session (`logger="events"`, payload `{"event": "bus_approaching", ...}`). Each matching trip fires once per day.

```
watch_departures(stop_id="8220DB000315", route="37",
                 direction="Wilton Terrace", lead_minutes=20,
                 window="08:00-12:00")
```

Requires a **persistent session** — the default `stdio` transport (one session per process) or HTTP with `NTA_STATELESS=false`. Your MCP client must also surface server log notifications for you to see them. Notifications are built from real-time predictions where available and fall back to the schedule otherwise (the payload's `live` flag says which); routes the NTA feed does not report in real time, and the post-midnight service-day window, are subject to the same coverage limits as `get_departures`.

The event channel follows the same convention as [media-box](https://github.com/dmarkey/media-box): the client calls `subscribe_events` on (re)connect, and events arrive as MCP log notifications with `logger="events"`. In [markeybot](https://github.com/dmarkey/markeybot), set `events: true` on this server in `mcp_servers` and set `events_chat_id`; markeybot subscribes on connect and relays each event into that chat.

## Installation

```bash
# Using uv
uvx mcp-nta

# Or install with pip
pip install mcp-nta
python -m mcp_nta
```

## Configuration

### Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NTA_API_KEY` | Yes | — | API key from the [NTA developer portal](https://developer.nationaltransport.ie/) |
| `NTA_ROUTES` | No | *(all routes)* | Comma-separated whitelist of routes to index (see [Route filtering](#route-filtering)) |
| `NTA_REFRESH_HOURS` | No | `24` | How often to re-download GTFS data (in hours) |
| `NTA_TRANSPORT` | No | `stdio` | Transport protocol: `stdio`, `http` (streamable HTTP), or `sse` |
| `NTA_HOST` | No | `0.0.0.0` | Bind address (http/sse only) |
| `NTA_PORT` | No | `8000` | Port (http/sse only) |

### Claude Desktop / Claude Code (stdio)

```json
{
  "mcpServers": {
    "nta": {
      "command": "uvx",
      "args": ["mcp-nta"],
      "env": {
        "NTA_API_KEY": "YOUR_KEY",
        "NTA_ROUTES": "37,39a,DART,Green"
      }
    }
  }
}
```

### Remote access (HTTP)

```bash
NTA_API_KEY=YOUR_KEY NTA_TRANSPORT=http NTA_PORT=8000 mcp-nta
```

The server will be available at `http://localhost:8000/mcp`. Any MCP client that supports streamable HTTP can connect to this URL.

### SSE (legacy)

```bash
NTA_API_KEY=YOUR_KEY NTA_TRANSPORT=sse NTA_PORT=8000 mcp-nta
```

Available at `http://localhost:8000/sse`. Maintained for backward compatibility — prefer `http` for new deployments.

## Route filtering

By default, the server indexes every route in the NTA GTFS feed — over 1,000 routes, ~7 million schedule entries. This works fine but produces a ~400 MB cache and takes a couple of minutes on first start.

Setting `NTA_ROUTES` restricts the index to only the routes you care about. This dramatically reduces build time, disk usage, and memory:

| | No filter | `37,39a,DART` |
|---|---|---|
| Cache size | ~400 MB | ~6 MB |
| First build | ~2 min | ~1 min |
| Memory (build) | ~120 MB | ~40 MB |

Each entry in `NTA_ROUTES` is matched **case-insensitively** against either:

- **Route short name** — the public-facing route number/name (e.g. `37`, `39a`, `102`, `Green`, `DART`)
- **Route type** — `bus`, `rail`, or `tram` to include all routes of that type

Examples:

```bash
# Just a few bus routes
NTA_ROUTES="37,39a,46a"

# All rail + a specific bus
NTA_ROUTES="rail,37"

# All trams (Luas Green and Red lines)
NTA_ROUTES="tram"

# Everything (default when unset)
NTA_ROUTES=""
```

Only stops served by whitelisted routes are kept. If the filter changes between runs, the database is automatically rebuilt.

## Architecture

### Static data

On first run, the server downloads two datasets from Transport for Ireland:

1. **GTFS schedule** — routes, stops, trips, stop times, and service calendars
2. **NaPTAN stop points** — supplementary stop metadata (street names)

These are parsed and stored in a **SQLite database** at `~/.cache/mcp-nta/gtfs.db`. On subsequent starts, the server opens the existing database with no parsing — startup is instant. The database is rebuilt automatically when it expires (controlled by `NTA_REFRESH_HOURS`) or when the route filter changes.

The SQLite approach means the server never loads the full dataset into memory. Queries read only the specific rows they need via indexed lookups.

### Real-time data

Real-time feeds (GTFS-RT) are fetched from the NTA API on demand and cached in memory for 30 seconds:

- **TripUpdates** — delays and predicted arrival times
- **VehiclePositions** — live GPS positions of vehicles
- **ServiceAlerts** — disruption notices

Each tool combines a targeted SQLite query against the static schedule with the relevant real-time feed to produce a concise, human-readable answer.

## Example

> "What buses are due at Oaktree Green for the 37?"

The LLM calls `search_transport(query="Oaktree Green")`, gets back stop IDs, then calls `get_departures(stop_id="8240DB001682", route="37")` and gets:

```
Upcoming departures from Oaktree Green (stop 8240DB001682) for route 37:

1. 37 -> Wilton Terrace | Due: 14:22 (in 4 min) — scheduled 14:20, +1 min late
2. 37 -> Wilton Terrace | Due: 14:40 (in 22 min) — on time
3. 37 -> Wilton Terrace | Due: 15:03 (in 45 min) — scheduled (no live data)
```

Two tool calls, two small responses, complete answer.

## License

MIT
