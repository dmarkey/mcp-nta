# mcp-nta

An MCP server for Ireland's National Transport Authority (NTA) public transport data. Provides focused, queryable tools that return small, human-readable results — not raw feed dumps.

Inspired by [ireland-nta-mcp](https://github.com/dmarkey/ireland-nta-mcp) and [tfi-gtfs](https://github.com/seanblanchfield/tfi-gtfs).

## Tools

| Tool | Description |
|------|-------------|
| `search_stops` | Find stops by name. Returns IDs, locations, routes served. |
| `search_routes` | Find routes by number or name. |
| `get_stop_departures` | Real-time departures from a stop, filtered by route/time window. |
| `get_vehicle_positions` | Live vehicle positions filtered by route or proximity. |
| `get_service_alerts` | Active service alerts filtered by route or stop. |
| `get_route_stops` | Ordered list of stops on a route. |

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

### Claude Desktop / Claude Code

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

The LLM calls `search_stops(query="Oaktree Green")`, gets back stop IDs, then calls `get_stop_departures(stop_id="8240DB001682", route="37")` and gets:

```
Upcoming departures from Oaktree Green (stop 8240DB001682) for route 37:

1. 37 -> Wilton Terrace | Due: 14:22 (in 4 min) — scheduled 14:20, +1 min late
2. 37 -> Wilton Terrace | Due: 14:40 (in 22 min) — on time
3. 37 -> Wilton Terrace | Due: 15:03 (in 45 min) — scheduled (no live data)
```

Two tool calls, two small responses, complete answer.

## License

MIT
