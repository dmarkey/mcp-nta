"""MCP server for Ireland's NTA public transport.

Inspired by ireland-nta-mcp (https://github.com/dmarkey/ireland-nta-mcp).
"""

from .server import serve

import asyncio
import os

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))


def main() -> None:
    """MCP NTA Server — queryable tools for Irish public transport."""
    api_key = os.getenv("NTA_API_KEY")
    if not api_key:
        raise ValueError("NTA_API_KEY must be set in the environment or .env file.")
    asyncio.run(serve(api_key))


if __name__ == "__main__":
    main()
