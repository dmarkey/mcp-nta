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

## How it works

1. **Static data** (GTFS schedule + NaPTAN stops) is downloaded and indexed in memory at startup, refreshed every 24 hours.
2. **Real-time feeds** (GTFS-RT) are fetched on demand with a short cache (30s).
3. Each tool combines static reference data with real-time feeds to answer a specific question with concise, named results.

## Installation

```bash
# Using uv
uvx mcp-nta

# Or install with pip
pip install mcp-nta
python -m mcp_nta
```

## Configuration

Set the `NTA_API_KEY` environment variable (get one from the [NTA developer portal](https://developer.nationaltransport.ie/)).

### Claude Desktop / Claude Code

```json
{
  "mcpServers": {
    "nta": {
      "command": "uvx",
      "args": ["mcp-nta"],
      "env": {
        "NTA_API_KEY": "YOUR_KEY"
      }
    }
  }
}
```

## Example

> "What buses are due at Oaktree Green for the 37?"

The LLM calls `search_stops(query="Oaktree Green")`, gets back stop IDs, then calls `get_stop_departures(stop_id="4573", route="37")` and gets:

```
Upcoming departures from Oaktree Green (stop 4573) for route 37:

1. 37 -> City Centre | Due: 14:23 (in 4 min) — scheduled 14:20, +3 min late
2. 37 -> City Centre | Due: 14:41 (in 22 min) — on time
3. 37 -> City Centre | Due: 15:05 (in 46 min) — scheduled 15:02, +3 min late
```

Two tool calls, two small responses, complete answer.

## License

MIT
