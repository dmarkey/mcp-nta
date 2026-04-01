"""RealtimeClient — fetch, parse, and cache GTFS-RT feeds."""

from __future__ import annotations

import datetime
import logging
import time

import httpx
from google.transit.gtfs_realtime_pb2 import FeedMessage  # pyright: ignore

logger = logging.getLogger(__name__)

TRIP_UPDATES_URL = "https://api.nationaltransport.ie/gtfsr/v2/TripUpdates"
VEHICLES_URL = "https://api.nationaltransport.ie/gtfsr/v2/Vehicles"
COMBINED_URL = "https://api.nationaltransport.ie/gtfsr/v2/gtfsr"


class RealtimeClient:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._cache: dict[str, tuple[float, FeedMessage]] = {}

    def _headers(self) -> dict[str, str]:
        return {"Cache-Control": "no-cache", "x-api-key": self._api_key}

    async def _fetch(self, url: str, cache_ttl: int) -> FeedMessage:
        cached = self._cache.get(url)
        if cached and (time.time() - cached[0]) < cache_ttl:
            return cached[1]

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()

        feed = FeedMessage()
        feed.ParseFromString(resp.content)
        self._cache[url] = (time.time(), feed)
        return feed

    def get_feed_age(self, feed: FeedMessage) -> int | None:
        """Return the age of a feed in seconds based on its header timestamp.

        Returns None if no header timestamp is available.
        """
        if feed.header.timestamp > 0:
            feed_time = feed.header.timestamp
            return int(time.time() - feed_time)
        return None

    def get_feed_timestamp(self, feed: FeedMessage) -> datetime.datetime | None:
        """Return the feed header timestamp as a datetime."""
        if feed.header.timestamp > 0:
            return datetime.datetime.fromtimestamp(
                feed.header.timestamp, tz=datetime.timezone.utc
            )
        return None

    async def get_trip_updates(self, cache_ttl: int = 30) -> FeedMessage:
        return await self._fetch(TRIP_UPDATES_URL, cache_ttl)

    async def get_vehicles(self, cache_ttl: int = 30) -> FeedMessage:
        return await self._fetch(VEHICLES_URL, cache_ttl)

    async def get_alerts(self, cache_ttl: int = 60) -> FeedMessage:
        return await self._fetch(COMBINED_URL, cache_ttl)
