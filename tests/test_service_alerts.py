"""Tests for get_service_alerts message accuracy.

NTA's GTFS-Realtime "combined" endpoint is an alias of TripUpdates and carries
no alert entities, so the tool must not report an empty feed as a confirmed
"no active alerts" — that reads to a downstream agent as an all-clear it cannot
actually vouch for.
"""

from __future__ import annotations

from typing import cast

from google.transit.gtfs_realtime_pb2 import FeedMessage  # pyright: ignore

from mcp_nta.static_data import StaticDataManager
from mcp_nta.tools.service_alerts import get_service_alerts


class _FakeStatic:
    """Minimal stand-in for StaticDataManager used by get_service_alerts."""

    async def ensure_loaded(self):
        return None

    def get_route_ids_by_short_name(self, short_name):
        return [f"R_{short_name}"]

    def get_route(self, route_id):
        return None

    def get_stop(self, stop_id):
        return None


class _FakeRealtime:
    def __init__(self, feed: FeedMessage):
        self._feed = feed

    async def get_alerts(self, *a, **k):
        return self._feed


def _feed_with_alert(route_id: str = "R_37") -> FeedMessage:
    feed = FeedMessage()
    ent = feed.entity.add()
    ent.id = "a1"
    ent.alert.header_text.translation.add(language="en", text="Diversion in effect")
    ent.alert.informed_entity.add(route_id=route_id)
    return feed


def _empty_feed() -> FeedMessage:
    """A TripUpdates-shaped feed: entities present, but none are alerts."""
    feed = FeedMessage()
    ent = feed.entity.add()
    ent.id = "t1"
    ent.trip_update.trip.trip_id = "5851_1"
    return feed


async def _call(feed, **kw):
    return await get_service_alerts(
        cast(StaticDataManager, _FakeStatic()), _FakeRealtime(feed), **kw
    )


async def test_empty_feed_is_reported_as_unavailable_not_all_clear():
    out = await _call(_empty_feed())
    assert "not available" in out.lower()
    assert "no active service alerts" not in out.lower()


async def test_no_alert_entities_even_with_route_filter():
    out = await _call(_empty_feed(), route="37")
    assert "not available" in out.lower()


async def test_matching_alert_is_reported():
    out = await _call(_feed_with_alert("R_37"), route="37")
    assert "Diversion in effect" in out
    assert "1 active alert" in out


async def test_nonmatching_filter_names_the_other_alerts():
    """When alerts exist but none match, say so — and count the rest."""
    out = await _call(_feed_with_alert("R_99"), route="37")
    assert "No active service alerts affecting route 37" in out
    assert "1 alert(s) active on other routes/stops" in out
