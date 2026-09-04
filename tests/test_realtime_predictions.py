"""Regression tests for realtime prediction parsing.

The bug these guard against was silent: a delay of exactly 0 seconds means
"on time", but the original code tested the protobuf field for truthiness, so
every punctual service was treated as having no prediction at all — roughly a
third of the live NTA feed.  Nothing raised; departures just quietly claimed
"no live data".
"""

from __future__ import annotations

import datetime
from typing import cast

from google.transit.gtfs_realtime_pb2 import TripUpdate  # pyright: ignore

from mcp_nta.static_data import StaticDataManager
from mcp_nta.realtime import stop_time_prediction
from mcp_nta.tools.stop_departures import (
    _find_prediction,
    _Prediction,
    _resolve_delay,
)

WEEK = 7 * 24 * 60 * 60


def _stu(
    *,
    seq: int = 5,
    stop_id: str = "S1",
    arr_time: int | None = None,
    arr_delay: int | None = None,
    dep_delay: int | None = None,
) -> TripUpdate.StopTimeUpdate:
    stu = TripUpdate.StopTimeUpdate()
    stu.stop_sequence = seq
    stu.stop_id = stop_id
    if arr_time is not None:
        stu.arrival.time = arr_time
    if arr_delay is not None:
        stu.arrival.delay = arr_delay
    if dep_delay is not None:
        stu.departure.delay = dep_delay
    return stu


class TestStopTimePrediction:
    def test_zero_arrival_delay_is_a_real_prediction(self):
        """The regression: 0 means on time, not missing."""
        assert stop_time_prediction(_stu(arr_delay=0)) == (None, 0)

    def test_zero_departure_delay_is_a_real_prediction(self):
        assert stop_time_prediction(_stu(dep_delay=0)) == (None, 0)

    def test_positive_and_negative_delays(self):
        assert stop_time_prediction(_stu(arr_delay=180)) == (None, 180)
        assert stop_time_prediction(_stu(arr_delay=-90)) == (None, -90)

    def test_arrival_delay_preferred_over_departure(self):
        assert stop_time_prediction(_stu(arr_delay=60, dep_delay=999)) == (None, 60)

    def test_absolute_time_wins_over_delay(self):
        arrival, delay = stop_time_prediction(_stu(arr_time=1_788_520_000, arr_delay=42))
        assert delay is None
        assert arrival == datetime.datetime.fromtimestamp(
            1_788_520_000, tz=datetime.timezone.utc
        )

    def test_no_prediction(self):
        assert stop_time_prediction(_stu()) == (None, None)

    def test_absurd_delays_rejected_both_directions(self):
        assert stop_time_prediction(_stu(arr_delay=2 * WEEK)) == (None, None)
        assert stop_time_prediction(_stu(arr_delay=-2 * WEEK)) == (None, None)


class TestFindPrediction:
    preds = [
        _Prediction(1, 0, None, "A"),
        _Prediction(5, 60, None, "B"),
        _Prediction(9, 120, None, "C"),
    ]

    def test_exact_match(self):
        pred, exact = _find_prediction(self.preds, 5)
        assert exact and pred is not None and pred.stop_id == "B"

    def test_falls_back_to_nearest_upstream(self):
        pred, exact = _find_prediction(self.preds, 7)
        assert not exact and pred is not None and pred.stop_id == "B"

    def test_nothing_upstream(self):
        assert _find_prediction(self.preds, 0) == (None, False)

    def test_past_the_end_uses_last(self):
        pred, exact = _find_prediction(self.preds, 99)
        assert not exact and pred is not None and pred.stop_id == "C"


class _FakeStatic:
    """Stands in for StaticDataManager.get_scheduled_arrival."""

    def __init__(self, scheduled: datetime.datetime | None):
        self._scheduled = scheduled
        self.calls: list[tuple[str, str, int]] = []

    def get_scheduled_arrival(self, trip_id, stop_id, stop_sequence, now):
        self.calls.append((trip_id, stop_id, stop_sequence))
        return self._scheduled


class TestResolveDelay:
    now = datetime.datetime(2026, 9, 4, 12, 0, tzinfo=datetime.timezone.utc)
    sched = datetime.datetime(2026, 9, 4, 12, 10, tzinfo=datetime.timezone.utc)

    def test_exact_absolute_time_used_directly(self):
        arrival = self.sched + datetime.timedelta(minutes=3)
        got = _resolve_delay(
            _Prediction(5, None, arrival, "B"), True, "t1", self.sched, self.now,
            cast(StaticDataManager, _FakeStatic(None)),
        )
        assert got == (arrival, 180)

    def test_zero_delay_resolves_to_on_time(self):
        got = _resolve_delay(
            _Prediction(5, 0, None, "B"), True, "t1", self.sched, self.now,
            cast(StaticDataManager, _FakeStatic(None)),
        )
        assert got == (self.sched, 0)

    def test_upstream_absolute_time_converted_via_schedule(self):
        """An upstream stop reporting only an absolute time still yields a delay."""
        upstream_sched = datetime.datetime(2026, 9, 4, 11, 50, tzinfo=datetime.timezone.utc)
        upstream_actual = upstream_sched + datetime.timedelta(minutes=4)
        static = _FakeStatic(upstream_sched)
        got = _resolve_delay(
            _Prediction(3, None, upstream_actual, "A"), False, "t1", self.sched,
            self.now, cast(StaticDataManager, static),
        )
        assert got == (self.sched + datetime.timedelta(minutes=4), 240)
        assert static.calls == [("t1", "A", 3)]

    def test_upstream_absolute_time_unusable_without_schedule(self):
        got = _resolve_delay(
            _Prediction(3, None, self.now, "A"), False, "t1", self.sched, self.now,
            cast(StaticDataManager, _FakeStatic(None)),
        )
        assert got is None

    def test_upstream_conversion_rejects_absurd_result(self):
        upstream_sched = datetime.datetime(2026, 9, 4, 11, 50, tzinfo=datetime.timezone.utc)
        static = _FakeStatic(upstream_sched)
        got = _resolve_delay(
            _Prediction(3, None, upstream_sched + datetime.timedelta(days=14), "A"),
            False, "t1", self.sched, self.now, cast(StaticDataManager, static),
        )
        assert got is None

    def test_no_prediction_at_all(self):
        got = _resolve_delay(
            _Prediction(5, None, None, "B"), True, "t1", self.sched, self.now,
            cast(StaticDataManager, _FakeStatic(None)),
        )
        assert got is None
