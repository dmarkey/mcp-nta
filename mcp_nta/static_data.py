"""StaticDataManager — download, parse, index GTFS static + NaPTAN data."""

from __future__ import annotations

import asyncio
import csv
import datetime
import io
import logging
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass

import httpx

from .models import Route, Stop
from .util import route_type_name

logger = logging.getLogger(__name__)

GTFS_URL = "https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip"
NAPTAN_STOPS_URL = (
    "https://www.transportforireland.ie/transitData/Data/NaPTAN_Stop_Points.csv"
)

DEFAULT_TTL = 24 * 60 * 60  # 24 hours


@dataclass(slots=True)
class StopTimeEntry:
    """A single scheduled arrival at a stop."""
    trip_id: str
    arrival_hour: int   # may be >= 24 for overnight trips
    arrival_min: int
    arrival_sec: int
    stop_sequence: int


@dataclass(slots=True)
class ServiceCalendar:
    """Calendar validity for a service_id."""
    start_date: datetime.date
    end_date: datetime.date
    days: list[bool]  # [mon, tue, wed, thu, fri, sat, sun]


class StaticDataManager:
    def __init__(self, ttl: int = DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._loaded_at: float = 0
        self._loading = False
        self._lock = asyncio.Lock()

        # GTFS data
        self._stops: dict[str, Stop] = {}
        self._routes: dict[str, Route] = {}
        self._trip_to_route: dict[str, str] = {}  # trip_id -> route_id
        self._trip_headsign: dict[str, str] = {}  # trip_id -> headsign
        self._trip_service: dict[str, str] = {}   # trip_id -> service_id
        self._route_to_stops: dict[str, list[str]] = {}  # route_id -> ordered stop_ids
        self._stop_to_routes: dict[str, set[str]] = defaultdict(set)  # stop_id -> route_ids

        # Schedule data: stop_id -> {hour -> [StopTimeEntry]}
        self._stop_times: dict[str, dict[int, list[StopTimeEntry]]] = defaultdict(lambda: defaultdict(list))

        # Calendar
        self._calendars: dict[str, ServiceCalendar] = {}  # service_id -> calendar
        self._calendar_exceptions: dict[str, int] = {}     # "service_id:date" -> type (1=added, 2=removed)

        # Agencies
        self._agencies: dict[str, str] = {}  # agency_id -> name

    @property
    def is_loaded(self) -> bool:
        return self._loaded_at > 0

    @property
    def is_stale(self) -> bool:
        return (time.time() - self._loaded_at) > self._ttl

    async def ensure_loaded(self) -> None:
        if self.is_loaded and not self.is_stale:
            return
        async with self._lock:
            if self.is_loaded and not self.is_stale:
                return
            await self._load()

    async def _load(self) -> None:
        logger.info("Loading static data...")
        self._loading = True
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                await asyncio.gather(
                    self._load_gtfs(client),
                    self._load_naptan(client),
                )
            self._build_stop_routes()
            self._loaded_at = time.time()
            logger.info(
                "Static data loaded: %d stops, %d routes",
                len(self._stops),
                len(self._routes),
            )
        finally:
            self._loading = False

    async def _load_gtfs(self, client: httpx.AsyncClient) -> None:
        resp = await client.get(GTFS_URL)
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            self._parse_agencies(zf)
            self._parse_routes(zf)
            self._parse_stops_gtfs(zf)
            self._parse_calendar(zf)
            self._parse_calendar_dates(zf)
            self._parse_trips(zf)
            self._parse_stop_times(zf)

    def _parse_agencies(self, zf: zipfile.ZipFile) -> None:
        try:
            with zf.open("agency.txt") as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
                for row in reader:
                    self._agencies[row["agency_id"]] = row["agency_name"]
        except KeyError:
            pass

    def _parse_routes(self, zf: zipfile.ZipFile) -> None:
        with zf.open("routes.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                route_id = row["route_id"]
                agency_id = row.get("agency_id", "")
                self._routes[route_id] = Route(
                    route_id=route_id,
                    short_name=row.get("route_short_name", ""),
                    long_name=row.get("route_long_name", ""),
                    agency=self._agencies.get(agency_id, agency_id),
                    route_type=route_type_name(int(row.get("route_type", "3"))),
                )

    def _parse_stops_gtfs(self, zf: zipfile.ZipFile) -> None:
        with zf.open("stops.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                stop_id = row["stop_id"]
                lat = float(row.get("stop_lat", 0))
                lon = float(row.get("stop_lon", 0))
                if stop_id not in self._stops:
                    self._stops[stop_id] = Stop(
                        stop_id=stop_id,
                        name=row.get("stop_name", stop_id),
                        latitude=lat,
                        longitude=lon,
                    )

    def _parse_calendar(self, zf: zipfile.ZipFile) -> None:
        try:
            with zf.open("calendar.txt") as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
                for row in reader:
                    service_id = row["service_id"]
                    start_str = row["start_date"].replace("-", "")
                    end_str = row["end_date"].replace("-", "")
                    days = [
                        bool(int(row.get(d, "0")))
                        for d in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
                    ]
                    self._calendars[service_id] = ServiceCalendar(
                        start_date=datetime.datetime.strptime(start_str, "%Y%m%d").date(),
                        end_date=datetime.datetime.strptime(end_str, "%Y%m%d").date(),
                        days=days,
                    )
        except KeyError:
            logger.warning("No calendar.txt found in GTFS data")

    def _parse_calendar_dates(self, zf: zipfile.ZipFile) -> None:
        try:
            with zf.open("calendar_dates.txt") as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
                for row in reader:
                    service_id = row["service_id"]
                    date_str = row["date"].replace("-", "")
                    date = datetime.datetime.strptime(date_str, "%Y%m%d").date()
                    exception_type = int(row["exception_type"])
                    self._calendar_exceptions[f"{service_id}:{date}"] = exception_type
        except KeyError:
            logger.warning("No calendar_dates.txt found in GTFS data")

    def _parse_trips(self, zf: zipfile.ZipFile) -> None:
        with zf.open("trips.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                trip_id = row["trip_id"]
                self._trip_to_route[trip_id] = row["route_id"]
                self._trip_service[trip_id] = row["service_id"]
                headsign = row.get("trip_headsign", "")
                if headsign:
                    self._trip_headsign[trip_id] = headsign

    def _parse_stop_times(self, zf: zipfile.ZipFile) -> None:
        route_stops_tmp: dict[str, list[tuple[int, str]]] = defaultdict(list)

        with zf.open("stop_times.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                trip_id = row["trip_id"]
                stop_id = row["stop_id"]
                route_id = self._trip_to_route.get(trip_id)
                if route_id is None:
                    continue

                self._stop_to_routes[stop_id].add(route_id)

                seq = int(row.get("stop_sequence", 0))
                arrival_time = row.get("arrival_time", "")
                if arrival_time:
                    parts = arrival_time.split(":")
                    h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
                    hour_key = h % 24
                    entry = StopTimeEntry(
                        trip_id=trip_id,
                        arrival_hour=h,
                        arrival_min=m,
                        arrival_sec=s,
                        stop_sequence=seq,
                    )
                    self._stop_times[stop_id][hour_key].append(entry)

                route_stops_tmp.setdefault(f"{route_id}:{trip_id}", []).append(
                    (seq, stop_id)
                )

        # Pick the longest trip per route as representative
        best: dict[str, list[tuple[int, str]]] = {}
        for key, stops in route_stops_tmp.items():
            route_id = key.split(":")[0]
            if route_id not in best or len(stops) > len(best[route_id]):
                best[route_id] = stops

        for route_id, stops in best.items():
            stops.sort(key=lambda x: x[0])
            self._route_to_stops[route_id] = [s[1] for s in stops]

    async def _load_naptan(self, client: httpx.AsyncClient) -> None:
        try:
            resp = await client.get(NAPTAN_STOPS_URL)
            resp.raise_for_status()
            text = resp.text
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                atco = row.get("AtcoCode", "").strip()
                if not atco:
                    continue
                name = row.get("CommonName", "").strip()
                street = row.get("Street", "").strip() or None
                lat = float(row.get("Latitude", 0) or 0)
                lon = float(row.get("Longitude", 0) or 0)
                if atco in self._stops:
                    if street:
                        self._stops[atco].street = street
                    if name and not self._stops[atco].name:
                        self._stops[atco].name = name
                else:
                    self._stops[atco] = Stop(
                        stop_id=atco,
                        name=name or atco,
                        latitude=lat,
                        longitude=lon,
                        street=street,
                    )
        except Exception:
            logger.warning("Failed to load NaPTAN data, continuing without it", exc_info=True)

    def _build_stop_routes(self) -> None:
        """Populate routes_served on each Stop from the stop_to_routes index."""
        for stop_id, route_ids in self._stop_to_routes.items():
            stop = self._stops.get(stop_id)
            if stop is None:
                continue
            names = set()
            for rid in route_ids:
                route = self._routes.get(rid)
                if route and route.short_name:
                    names.add(route.short_name)
            stop.routes_served = sorted(names)

    # --- Calendar helpers ---

    def is_service_running(self, service_id: str, date: datetime.date) -> bool:
        """Check if a service_id is active on the given date."""
        exc_key = f"{service_id}:{date}"
        exc = self._calendar_exceptions.get(exc_key)
        if exc == 1:  # added
            return True
        if exc == 2:  # removed
            return False
        cal = self._calendars.get(service_id)
        if cal is None:
            return False
        if not (cal.start_date <= date <= cal.end_date):
            return False
        return cal.days[date.weekday()]

    def get_scheduled_stop_times(
        self,
        stop_id: str,
        now: datetime.datetime,
        max_minutes: int = 60,
        route_ids: set[str] | None = None,
    ) -> list[tuple[StopTimeEntry, datetime.datetime]]:
        """Get scheduled arrivals at a stop, filtered by calendar and time window.

        Returns list of (StopTimeEntry, arrival_datetime) sorted by arrival time.
        The returned datetimes are in Irish local time (Europe/Dublin).
        """
        # GTFS schedule times are in local time (Europe/Dublin).
        # Work in naive local time for comparison, then attach tz at the end.
        try:
            import zoneinfo
            dublin_tz = zoneinfo.ZoneInfo("Europe/Dublin")
        except Exception:
            dublin_tz = datetime.timezone.utc

        now_local = now.astimezone(dublin_tz)
        today = now_local.date()
        result: list[tuple[StopTimeEntry, datetime.datetime]] = []

        # Check hours that could contain arrivals in our window
        now_td = datetime.timedelta(hours=now_local.hour, minutes=now_local.minute, seconds=now_local.second)
        end_td = now_td + datetime.timedelta(minutes=max_minutes)

        # We need to check: previous hour (for late-window entries),
        # current hour, and hours up to max_minutes ahead
        check_hours: list[int] = []
        start_hour = max(0, now.hour - 1)
        end_hour = now.hour + (max_minutes // 60) + 1
        for h in range(start_hour, end_hour + 1):
            check_hours.append(h % 24)

        stop_hours = self._stop_times.get(stop_id)
        if not stop_hours:
            return result

        for hour in check_hours:
            entries = stop_hours.get(hour, [])
            for entry in entries:
                # Filter by route
                if route_ids:
                    trip_route = self._trip_to_route.get(entry.trip_id)
                    if trip_route not in route_ids:
                        continue

                # Check calendar
                service_id = self._trip_service.get(entry.trip_id)
                if service_id and not self.is_service_running(service_id, today):
                    continue

                # Compute arrival datetime
                arrival_td = datetime.timedelta(
                    hours=entry.arrival_hour, minutes=entry.arrival_min, seconds=entry.arrival_sec
                )

                # Handle overnight (hour >= 24): arrival is on the same service day
                # but if arrival_td > 24h, it's past midnight relative to the service start
                if arrival_td - datetime.timedelta(hours=12) > now_td:
                    # More than 12h in the future from now's time-of-day — might be yesterday's overnight
                    # Skip unless it falls in our window
                    pass

                arrival_dt_naive = datetime.datetime(today.year, today.month, today.day) + arrival_td

                # If arrival is more than 12h in the past, assume it's tomorrow
                now_naive = now_local.replace(tzinfo=None)
                if (now_naive - arrival_dt_naive).total_seconds() > 12 * 3600:
                    arrival_dt_naive += datetime.timedelta(days=1)

                # Check time window
                if arrival_dt_naive < now_naive - datetime.timedelta(minutes=1):
                    continue
                if arrival_dt_naive > now_naive + datetime.timedelta(minutes=max_minutes):
                    continue

                # Attach timezone for downstream use
                arrival_dt = arrival_dt_naive.replace(tzinfo=dublin_tz)
                result.append((entry, arrival_dt))

        result.sort(key=lambda x: x[1])
        return result

    # --- Public query methods ---

    def search_stops(self, query: str, limit: int = 5) -> list[Stop]:
        q = query.lower()
        results: list[Stop] = []
        for stop in self._stops.values():
            if q in stop.name.lower():
                results.append(stop)
                if len(results) >= limit * 5:
                    break
        results.sort(key=lambda s: (not s.name.lower().startswith(q), s.name))
        return results[:limit]

    def search_routes(self, query: str, limit: int = 5) -> list[Route]:
        q = query.lower()
        results: list[Route] = []
        for route in self._routes.values():
            if (
                q == route.short_name.lower()
                or q in route.short_name.lower()
                or q in route.long_name.lower()
            ):
                results.append(route)
        results.sort(
            key=lambda r: (
                r.short_name.lower() != q,
                not r.short_name.lower().startswith(q),
                r.short_name,
            )
        )
        return results[:limit]

    def get_stop(self, stop_id: str) -> Stop | None:
        return self._stops.get(stop_id)

    def get_route(self, route_id: str) -> Route | None:
        return self._routes.get(route_id)

    def get_route_by_short_name(self, short_name: str) -> Route | None:
        sn = short_name.lower()
        for route in self._routes.values():
            if route.short_name.lower() == sn:
                return route
        return None

    def get_route_ids_by_short_name(self, short_name: str) -> list[str]:
        sn = short_name.lower()
        return [r.route_id for r in self._routes.values() if r.short_name.lower() == sn]

    def get_stops_for_route(self, route_id: str) -> list[Stop]:
        stop_ids = self._route_to_stops.get(route_id, [])
        return [self._stops[sid] for sid in stop_ids if sid in self._stops]

    def get_routes_for_stop(self, stop_id: str) -> list[Route]:
        route_ids = self._stop_to_routes.get(stop_id, set())
        return [self._routes[rid] for rid in route_ids if rid in self._routes]

    def resolve_trip(self, trip_id: str) -> tuple[str | None, str | None]:
        """Return (route_short_name, headsign) for a trip_id."""
        route_id = self._trip_to_route.get(trip_id)
        if route_id is None:
            return None, None
        route = self._routes.get(route_id)
        short_name = route.short_name if route else None
        headsign = self._trip_headsign.get(trip_id, "")
        return short_name, headsign

    def resolve_trip_route_id(self, trip_id: str) -> str | None:
        return self._trip_to_route.get(trip_id)
