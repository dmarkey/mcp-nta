"""MCP server for Ireland's NTA public transport.

Inspired by ireland-nta-mcp (https://github.com/dmarkey/ireland-nta-mcp).
"""

from .server import create_server

import os

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))


def _parse_route_filter() -> list[str] | None:
    """Parse NTA_ROUTES env var into a list of route/type filters, or None."""
    raw = os.getenv("NTA_ROUTES", "").strip()
    if not raw:
        return None
    return [r.strip() for r in raw.split(",") if r.strip()]


def main() -> None:
    """MCP NTA Server — queryable tools for Irish public transport."""
    api_key = os.getenv("NTA_API_KEY")
    if not api_key:
        raise ValueError("NTA_API_KEY must be set in the environment or .env file.")
    route_filter = _parse_route_filter()
    ttl_hours = float(os.getenv("NTA_REFRESH_HOURS", "24"))
    ttl = int(ttl_hours * 3600)

    transport = os.getenv("NTA_TRANSPORT", "stdio").lower()
    host = os.getenv("NTA_HOST", "0.0.0.0")
    port = int(os.getenv("NTA_PORT", "8000"))

    server = create_server(api_key, route_filter=route_filter, ttl=ttl)

    kwargs: dict = {"transport": transport}
    if transport in ("http", "sse"):
        kwargs["host"] = host
        kwargs["port"] = port

    server.run(**kwargs)


if __name__ == "__main__":
    main()
