"""RealtimeClient — fetch, parse, and cache GTFS-RT feeds."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time

import httpx
from google.transit.gtfs_realtime_pb2 import FeedMessage  # pyright: ignore

from .tls import ssl_context

logger = logging.getLogger(__name__)

TRIP_UPDATES_URL = "https://api.nationaltransport.ie/gtfsr/v2/TripUpdates"
VEHICLES_URL = "https://api.nationaltransport.ie/gtfsr/v2/Vehicles"
COMBINED_URL = "https://api.nationaltransport.ie/gtfsr/v2/gtfsr"

# The NTA API sits behind a flaky load balancer; a dropped connection on one
# attempt usually succeeds on the next.
MAX_ATTEMPTS = 3
RETRY_DELAY = 0.5


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

        content = await self._get(url)

        feed = FeedMessage()
        feed.ParseFromString(content)
        self._cache[url] = (time.time(), feed)
        return feed

    async def _get(self, url: str) -> bytes:
        """GET *url*, retrying transport-level failures a few times."""
        async with httpx.AsyncClient(timeout=30, verify=ssl_context()) as client:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    resp = await client.get(url, headers=self._headers())
                    resp.raise_for_status()
                    return resp.content
                except httpx.TransportError as exc:
                    if attempt == MAX_ATTEMPTS:
                        raise
                    logger.warning(
                        "%s failed (attempt %d/%d): %s — retrying",
                        url, attempt, MAX_ATTEMPTS, exc,
                    )
                    await asyncio.sleep(RETRY_DELAY * attempt)
        raise AssertionError("unreachable")  # pragma: no cover

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


def stop_time_prediction(stu) -> tuple[datetime.datetime | None, int | None]:
    """Extract (absolute_arrival, delay_seconds) from a StopTimeUpdate.

    A delay of exactly ``0`` means "on time" — a genuine prediction — so the
    delay fields are probed with ``HasField`` rather than for truthiness.
    Testing ``stu.arrival.delay`` directly silently discards every punctual
    service, which is roughly a third of the NTA feed.

    Either element may be None; both are None when the feed carries no
    prediction for this stop.
    """
    arrival_dt: datetime.datetime | None = None
    delay: int | None = None

    if stu.HasField("arrival") and stu.arrival.time > 0:
        arrival_dt = datetime.datetime.fromtimestamp(
            stu.arrival.time, tz=datetime.timezone.utc
        )
    elif stu.HasField("arrival") and stu.arrival.HasField("delay"):
        delay = stu.arrival.delay
    elif stu.HasField("departure") and stu.departure.HasField("delay"):
        delay = stu.departure.delay

    # Guard against nonsensical values (more than a week either way).
    if delay is not None and abs(delay) > 7 * 24 * 60 * 60:
        return None, None

    return arrival_dt, delay
