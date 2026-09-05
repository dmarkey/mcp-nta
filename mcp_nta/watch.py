"""Departure watches — push a notification when a bus is N minutes away.

A watch is a standing rule: "for stop X, route 37 toward Wilton Terrace, tell me
when a bus is 20 minutes out, but only between 08:00 and 12:00". A single
background task polls the same corrected departure logic the get_departures tool
uses, and when a matching departure crosses the lead-time threshold it pushes an
MCP log notification to the client that created the watch.

Delivery reuses MCP's server->client log-notification channel
(session.send_log_message with logger="events"), so a background event can reach
a client that isn't currently blocked on a tool call. This needs a session that
outlives the request — true for the stdio transport (one session per process)
and for stateful HTTP, but not for stateless HTTP.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import secrets
from dataclasses import dataclass, field

from .realtime import RealtimeClient
from .static_data import StaticDataManager, _dublin_tz
from .tools.stop_departures import compute_departures

logger = logging.getLogger(__name__)

# How often the poll loop wakes. The realtime feed is cached for 30s, so a 45s
# cadence means roughly one upstream fetch per cycle no matter how many watches
# are active — well clear of the API's rate limit.
POLL_SECONDS = 45


def parse_window(window: str | None) -> tuple[int, int] | None:
    """Parse "HH:MM-HH:MM" into (start, end) minutes-of-day (local time).

    Returns None for an empty window (always active). A window whose start is
    after its end is treated as spanning midnight (e.g. "22:00-02:00").
    """
    if not window or not window.strip():
        return None
    try:
        start_s, end_s = window.split("-", 1)
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
    except ValueError as exc:
        raise ValueError(
            f'Invalid window {window!r}: expected "HH:MM-HH:MM", e.g. "08:00-12:00".'
        ) from exc
    start, end = sh * 60 + sm, eh * 60 + em
    if not (0 <= start < 24 * 60 and 0 <= end < 24 * 60):
        raise ValueError(f"Invalid window {window!r}: times must be within 00:00-23:59.")
    return start, end


def window_contains(window: tuple[int, int] | None, local_minute: int) -> bool:
    """Is local_minute (minutes since local midnight) inside the window?"""
    if window is None:
        return True
    start, end = window
    if start <= end:
        return start <= local_minute <= end
    return local_minute >= start or local_minute <= end  # wraps past midnight


# Monday=0 .. Sunday=6, matching datetime.date.weekday().
_DAY_NUMBERS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
_DAY_SHORTCUTS = {
    "weekdays": {0, 1, 2, 3, 4},
    "weekends": {5, 6},
    "daily": None,
    "all": None,
}


def parse_days(days: str | None) -> set[int] | None:
    """Parse a day filter into weekday numbers (Mon=0..Sun=6).

    Returns None for an empty filter (active every day). Accepts
    comma-separated day names ("mon,wed,fri", full names also work),
    ranges ("mon-fri", wrapping past Sunday is allowed), and the shortcuts
    "weekdays", "weekends", "daily", and "all".
    """
    if not days or not days.strip():
        return None
    picked: set[int] = set()
    for raw_token in days.split(","):
        token = raw_token.strip().lower()
        if not token:
            continue
        if token in _DAY_SHORTCUTS:
            shortcut = _DAY_SHORTCUTS[token]
            if shortcut is None:
                return None
            picked |= shortcut
            continue
        if "-" in token:
            start_s, _, end_s = token.partition("-")
            start = _DAY_NUMBERS.get(start_s.strip())
            end = _DAY_NUMBERS.get(end_s.strip())
            if start is None or end is None:
                raise ValueError(
                    f"Invalid days {days!r}: expected day names like "
                    '"mon-fri", e.g. "weekdays" or "mon,wed,fri".'
                )
            day = start
            while True:
                picked.add(day)
                if day == end:
                    break
                day = (day + 1) % 7
            continue
        day = _DAY_NUMBERS.get(token)
        if day is None:
            raise ValueError(
                f"Invalid days {days!r}: expected day names like "
                '"mon-fri", e.g. "weekdays" or "mon,wed,fri".'
            )
        picked.add(day)
    if not picked:
        return None
    return picked


@dataclass
class Watch:
    id: str
    stop_id: str
    stop_name: str
    route: str | None
    route_ids: set[str] | None
    direction: str | None            # headsign substring, case-insensitive
    lead_minutes: int
    window: tuple[int, int] | None   # local minutes-of-day, or None = always
    window_text: str | None
    client: str
    session: object
    created: datetime.datetime
    fired: set[tuple[str, str]] = field(default_factory=set)  # (trip_id, date)
    days: set[int] | None = None    # weekdays (Mon=0..Sun=6), or None = every day
    days_text: str | None = None

    def describe(self) -> str:
        parts = [f"route {self.route}" if self.route else "all routes"]
        if self.direction:
            parts.append(f"toward {self.direction}")
        parts.append(f"at {self.stop_name}")
        parts.append(f"{self.lead_minutes} min out")
        if self.window_text:
            parts.append(f"between {self.window_text}")
        if self.days_text:
            parts.append(f"on {self.days_text}")
        return ", ".join(parts)


class WatchRegistry:
    """Holds active watches and runs the single background poll loop."""

    def __init__(self, static: StaticDataManager, realtime: RealtimeClient) -> None:
        self._static = static
        self._realtime = realtime
        self._watches: dict[str, Watch] = {}
        # Latest session seen per client name, so delivery survives a reconnect
        # (a new session for the same client takes over its watches' delivery).
        self._sessions: dict[str, object] = {}

    # -- session tracking --------------------------------------------------

    def touch_session(self, client: str, session: object) -> None:
        self._sessions[client] = session

    # -- watch lifecycle ---------------------------------------------------

    def add(self, watch: Watch) -> None:
        self._watches[watch.id] = watch
        self._sessions[watch.client] = watch.session

    def remove(self, watch_id: str) -> Watch | None:
        return self._watches.pop(watch_id, None)

    def list_for(self, client: str) -> list[Watch]:
        return [w for w in self._watches.values() if w.client == client]

    # -- the poll loop -----------------------------------------------------

    async def run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("watch poll tick failed")
            await asyncio.sleep(POLL_SECONDS)

    async def _tick(self) -> None:
        if not self._watches:
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        local = now.astimezone(_dublin_tz())
        local_minute = local.hour * 60 + local.minute
        weekday = local.weekday()
        today = local.date().isoformat()

        for watch in list(self._watches.values()):
            if watch.days is not None and weekday not in watch.days:
                continue
            if not window_contains(watch.window, local_minute):
                continue
            try:
                await self._check_watch(watch, now, today)
            except Exception:
                logger.exception("watch %s check failed", watch.id)

    async def _check_watch(
        self, watch: Watch, now: datetime.datetime, today: str
    ) -> None:
        # Day gate (Europe/Dublin local day): the poll loop pre-filters, but
        # check here too so direct callers get the same semantics.
        if watch.days is not None:
            weekday = now.astimezone(_dublin_tz()).weekday()
            if weekday not in watch.days:
                return
        # Look a little past the lead time so a bus is caught the first cycle it
        # crosses the threshold.
        window_minutes = watch.lead_minutes + 5
        departures, _feed = await compute_departures(
            self._static, self._realtime, watch.stop_id, watch.route_ids,
            window_minutes, now,
        )

        # Drop stale fired-markers from earlier days so the set can't grow without
        # bound and a matching trip fires again on its next service day.
        watch.fired = {(t, d) for (t, d) in watch.fired if d == today}

        for dep in departures:
            if watch.direction and watch.direction.lower() not in dep.destination.lower():
                continue
            eta = (dep.predicted - now).total_seconds() / 60.0
            if not (0 <= eta <= watch.lead_minutes):
                continue
            marker = (dep.trip_id, today)
            if marker in watch.fired:
                continue
            # Mark fired only if delivery succeeds. A bus that already passed
            # can't re-fire (eta < 0 is excluded above, and compute_departures
            # drops departures more than a minute in the past), so retrying a
            # failed send on the next cycle re-notifies a still-approaching bus
            # rather than losing the one notification that matters.
            if await self._deliver(watch, dep, round(eta)):
                watch.fired.add(marker)

    async def _deliver(self, watch: Watch, dep, eta_minutes: int) -> bool:
        """Push one event. Returns True if the client accepted it."""
        live = dep.status != "scheduled"
        payload = {
            "event": "bus_approaching",
            "watch_id": watch.id,
            "stop": watch.stop_name,
            "stop_id": watch.stop_id,
            "route": dep.route,
            "destination": dep.destination,
            "eta_minutes": eta_minutes,
            "predicted": dep.predicted.astimezone(_dublin_tz()).strftime("%H:%M"),
            "live": live,
            "message": (
                f"{dep.route} to {dep.destination} is about {eta_minutes} min from "
                f"{watch.stop_name} "
                f"({'live' if live else 'scheduled'}, due {dep.predicted.astimezone(_dublin_tz()).strftime('%H:%M')})."
            ),
        }
        session = self._sessions.get(watch.client, watch.session)
        try:
            await session.send_log_message(  # type: ignore[attr-defined]
                level="notice", data=payload, logger="events"
            )
            return True
        except Exception:
            # Client gone (e.g. mid-reconnect). Leave the trip unmarked so the
            # next cycle retries while the bus is still approaching.
            logger.warning("delivery failed for watch %s (client %s)", watch.id, watch.client)
            return False


def make_watch(
    static: StaticDataManager,
    stop_id: str,
    route: str | None,
    direction: str | None,
    lead_minutes: int,
    window: str | None,
    client: str,
    session: object,
    days: str | None = None,
) -> Watch:
    """Validate inputs and build a Watch (does not register it)."""
    stop = static.get_stop(stop_id)
    if stop is None:
        raise ValueError(f"Stop {stop_id!r} not found.")
    route_ids: set[str] | None = None
    if route:
        route_ids = set(static.get_route_ids_by_short_name(route))
        if not route_ids:
            raise ValueError(f"Route {route!r} not found.")
    if lead_minutes <= 0 or lead_minutes > 120:
        raise ValueError("lead_minutes must be between 1 and 120.")
    win = parse_window(window)
    parsed_days = parse_days(days)
    return Watch(
        id=secrets.token_hex(4),
        stop_id=stop_id,
        stop_name=stop.name,
        route=route,
        route_ids=route_ids,
        direction=direction,
        lead_minutes=lead_minutes,
        window=win,
        window_text=window.strip() if window and window.strip() else None,
        client=client,
        session=session,
        created=datetime.datetime.now(datetime.timezone.utc),
        days=parsed_days,
        days_text=days.strip() if days and days.strip() else None,
    )
