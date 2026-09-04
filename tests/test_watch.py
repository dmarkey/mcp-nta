"""Tests for the departure-watch matching, windowing, and delivery."""

from __future__ import annotations

import datetime

import pytest

from mcp_nta.models import Departure
from mcp_nta.watch import (
    Watch,
    WatchRegistry,
    parse_window,
    window_contains,
)

UTC = datetime.timezone.utc


class TestParseWindow:
    def test_none_and_blank_mean_always(self):
        assert parse_window(None) is None
        assert parse_window("  ") is None

    def test_basic(self):
        assert parse_window("08:00-12:00") == (480, 720)

    def test_overnight_kept_as_is(self):
        assert parse_window("22:00-02:00") == (1320, 120)

    def test_bad_format_raises(self):
        with pytest.raises(ValueError):
            parse_window("8am to noon")
        with pytest.raises(ValueError):
            parse_window("25:00-26:00")


class TestWindowContains:
    def test_none_always_true(self):
        assert window_contains(None, 0)
        assert window_contains(None, 1439)

    def test_normal_window(self):
        w = (480, 720)  # 08:00-12:00
        assert not window_contains(w, 479)
        assert window_contains(w, 480)
        assert window_contains(w, 600)
        assert window_contains(w, 720)
        assert not window_contains(w, 721)

    def test_overnight_window(self):
        w = (1320, 120)  # 22:00-02:00
        assert window_contains(w, 1350)   # 22:30
        assert window_contains(w, 30)     # 00:30
        assert not window_contains(w, 720)  # 12:00


class _FakeSession:
    def __init__(self):
        self.sent: list[dict] = []
        self.fail = False

    async def send_log_message(self, level, data, logger=None, related_request_id=None):
        if self.fail:
            raise RuntimeError("client gone")
        self.sent.append({"level": level, "data": data, "logger": logger})


class _FakeStatic:
    async def ensure_loaded(self):
        return None

    def get_stop(self, stop_id):
        return None


class _StubRegistry(WatchRegistry):
    """WatchRegistry with compute_departures stubbed to a fixed list."""

    def __init__(self, departures):
        super().__init__(static=_FakeStatic(), realtime=None)  # type: ignore[arg-type]
        self._departures = departures

    async def _compute(self, *a, **k):
        return self._departures, None


def _dep(dest, eta_min, now, trip_id, status="on time"):
    return Departure(
        route="37",
        destination=dest,
        scheduled=now + datetime.timedelta(minutes=eta_min),
        predicted=now + datetime.timedelta(minutes=eta_min),
        delay_seconds=0,
        status=status,
        trip_id=trip_id,
    )


def _watch(session, **over):
    base = dict(
        id="w1", stop_id="S1", stop_name="Bachelors Walk", route="37",
        route_ids={"1 37 c a"}, direction="Wilton Terrace", lead_minutes=20,
        window=None, window_text=None, client="test", session=session,
        created=datetime.datetime.now(UTC),
    )
    base.update(over)
    return Watch(**base)


async def _run_check(registry, watch, now, today, monkeypatch):
    monkeypatch.setattr(registry, "_compute", registry._compute, raising=False)
    # patch compute_departures used inside _check_watch
    import mcp_nta.watch as wmod
    async def fake_compute(static, realtime, stop_id, route_ids, minutes, n):
        return registry._departures, None
    monkeypatch.setattr(wmod, "compute_departures", fake_compute)
    await registry._check_watch(watch, now, today)


async def test_fires_when_within_lead(monkeypatch):
    now = datetime.datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    sess = _FakeSession()
    reg = _StubRegistry([_dep("Wilton Terrace", 18, now, "t1")])
    w = _watch(sess)
    reg.add(w)
    await _run_check(reg, w, now, "2026-09-05", monkeypatch)
    assert len(sess.sent) == 1
    payload = sess.sent[0]["data"]
    assert payload["event"] == "bus_approaching"
    assert payload["eta_minutes"] == 18
    assert payload["destination"] == "Wilton Terrace"
    assert sess.sent[0]["logger"] == "events"


async def test_does_not_fire_beyond_lead(monkeypatch):
    now = datetime.datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    sess = _FakeSession()
    reg = _StubRegistry([_dep("Wilton Terrace", 25, now, "t1")])
    w = _watch(sess)
    reg.add(w)
    await _run_check(reg, w, now, "2026-09-05", monkeypatch)
    assert sess.sent == []


async def test_direction_filter_excludes_other_way(monkeypatch):
    now = datetime.datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    sess = _FakeSession()
    reg = _StubRegistry([_dep("Blanchardstown SC", 10, now, "t1")])
    w = _watch(sess)
    reg.add(w)
    await _run_check(reg, w, now, "2026-09-05", monkeypatch)
    assert sess.sent == []


async def test_fires_once_per_trip(monkeypatch):
    now = datetime.datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    sess = _FakeSession()
    reg = _StubRegistry([_dep("Wilton Terrace", 15, now, "t1")])
    w = _watch(sess)
    reg.add(w)
    await _run_check(reg, w, now, "2026-09-05", monkeypatch)
    await _run_check(reg, w, now, "2026-09-05", monkeypatch)  # same trip, next poll
    assert len(sess.sent) == 1


async def test_same_trip_refires_next_day(monkeypatch):
    sess = _FakeSession()
    reg = _StubRegistry([])
    w = _watch(sess)
    reg.add(w)
    day1 = datetime.datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    reg._departures = [_dep("Wilton Terrace", 15, day1, "t1")]
    await _run_check(reg, w, day1, "2026-09-05", monkeypatch)
    day2 = datetime.datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
    reg._departures = [_dep("Wilton Terrace", 15, day2, "t1")]
    await _run_check(reg, w, day2, "2026-09-06", monkeypatch)
    assert len(sess.sent) == 2


async def test_scheduled_status_marked_not_live(monkeypatch):
    now = datetime.datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    sess = _FakeSession()
    reg = _StubRegistry([_dep("Wilton Terrace", 12, now, "t1", status="scheduled")])
    w = _watch(sess)
    reg.add(w)
    await _run_check(reg, w, now, "2026-09-05", monkeypatch)
    assert sess.sent[0]["data"]["live"] is False


async def test_delivery_failure_keeps_fired_marker(monkeypatch):
    now = datetime.datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    sess = _FakeSession()
    sess.fail = True
    reg = _StubRegistry([_dep("Wilton Terrace", 15, now, "t1")])
    w = _watch(sess)
    reg.add(w)
    await _run_check(reg, w, now, "2026-09-05", monkeypatch)
    assert ("t1", "2026-09-05") in w.fired  # not retried in a storm on reconnect
