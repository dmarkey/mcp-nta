"""StaticDataManager — download, parse, index GTFS static + NaPTAN data.

On first run the GTFS zip and NaPTAN CSV are downloaded, parsed, and stored
in a local SQLite database (~/.cache/mcp-nta/gtfs.db).  Subsequent starts
just open the existing DB — no parsing needed.  The DB is rebuilt when it
becomes older than *ttl* seconds (default 24 h).
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import io
import logging
import os
import sqlite3
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from .models import Route, Stop
from .util import route_type_name

logger = logging.getLogger(__name__)

GTFS_URL = "https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip"
NAPTAN_STOPS_URL = (
    "https://www.transportforireland.ie/transitData/Data/NaPTAN_Stop_Points.csv"
)

DEFAULT_TTL = 24 * 60 * 60  # 24 hours

# Keep StopTimeEntry as the public type returned by get_scheduled_stop_times
# so callers don't need to change.

@dataclass(slots=True)
class StopTimeEntry:
    """A single scheduled arrival at a stop."""
    trip_id: str
    arrival_hour: int   # may be >= 24 for overnight trips
    arrival_min: int
    arrival_sec: int
    stop_sequence: int


VALID_ROUTE_TYPES = {"bus", "rail", "tram"}


def _filter_cache_key(route_filter: list[str] | None) -> str:
    """Stable string key for the current filter, used to detect config changes."""
    if not route_filter:
        return ""
    return ",".join(sorted(f.lower() for f in route_filter))


def _cache_dir() -> Path:
    """Return the platform cache directory for mcp-nta."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    d = base / "mcp-nta"
    d.mkdir(parents=True, exist_ok=True)
    return d


_SCHEMA = """
CREATE TABLE IF NOT EXISTS stops (
    stop_id   TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    latitude  REAL NOT NULL,
    longitude REAL NOT NULL,
    street    TEXT
);

CREATE TABLE IF NOT EXISTS routes (
    route_id   TEXT PRIMARY KEY,
    short_name TEXT NOT NULL,
    long_name  TEXT NOT NULL,
    agency     TEXT NOT NULL,
    route_type TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trips (
    trip_rowid INTEGER PRIMARY KEY,
    trip_id    TEXT NOT NULL UNIQUE,
    route_id   TEXT NOT NULL,
    service_id TEXT NOT NULL,
    headsign   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS stop_times (
    trip_rowid    INTEGER NOT NULL,
    stop_id       TEXT NOT NULL,
    arrival_hour  INTEGER NOT NULL,
    arrival_min   INTEGER NOT NULL,
    arrival_sec   INTEGER NOT NULL,
    stop_sequence INTEGER NOT NULL,
    hour_key      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar (
    service_id TEXT PRIMARY KEY,
    start_date TEXT NOT NULL,
    end_date   TEXT NOT NULL,
    monday     INTEGER NOT NULL,
    tuesday    INTEGER NOT NULL,
    wednesday  INTEGER NOT NULL,
    thursday   INTEGER NOT NULL,
    friday     INTEGER NOT NULL,
    saturday   INTEGER NOT NULL,
    sunday     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_dates (
    service_id     TEXT NOT NULL,
    date           TEXT NOT NULL,
    exception_type INTEGER NOT NULL,
    PRIMARY KEY (service_id, date)
);

CREATE TABLE IF NOT EXISTS stop_routes (
    stop_id  TEXT NOT NULL,
    route_id TEXT NOT NULL,
    PRIMARY KEY (stop_id, route_id)
);

CREATE TABLE IF NOT EXISTS route_stops_ordered (
    route_id      TEXT NOT NULL,
    stop_sequence INTEGER NOT NULL,
    stop_id       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_stop_times_stop_hour ON stop_times (stop_id, hour_key);
CREATE INDEX IF NOT EXISTS idx_trips_route ON trips (route_id);
CREATE INDEX IF NOT EXISTS idx_trips_trip_id ON trips (trip_id);
CREATE INDEX IF NOT EXISTS idx_routes_short_name ON routes (short_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_stops_name ON stops (name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_stop_routes_stop ON stop_routes (stop_id);
CREATE INDEX IF NOT EXISTS idx_route_stops_ordered ON route_stops_ordered (route_id, stop_sequence);
"""


class StaticDataManager:
    def __init__(self, ttl: int = DEFAULT_TTL, route_filter: list[str] | None = None) -> None:
        self._ttl = ttl
        self._route_filter = route_filter
        self._filter_key = _filter_cache_key(route_filter)
        self._loaded_at: float = 0
        self._lock = asyncio.Lock()
        self._db_path = _cache_dir() / "gtfs.db"
        self._db: sqlite3.Connection | None = None

    def _get_db(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = sqlite3.connect(str(self._db_path))
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        return self._db

    @property
    def is_loaded(self) -> bool:
        return self._loaded_at > 0

    @property
    def is_stale(self) -> bool:
        return (time.time() - self._loaded_at) > self._ttl

    def _db_is_fresh(self) -> bool:
        """Check if the SQLite DB exists, was built recently, and matches the current filter."""
        if not self._db_path.exists():
            return False
        try:
            db = self._get_db()
            row = db.execute(
                "SELECT value FROM meta WHERE key = 'built_at'"
            ).fetchone()
            if row is None:
                return False
            built_at = float(row[0])
            age = time.time() - built_at
            if age >= self._ttl:
                logger.info("DB expired (%.0fs old), rebuilding", age)
                return False

            # Check if the route filter has changed
            frow = db.execute(
                "SELECT value FROM meta WHERE key = 'route_filter'"
            ).fetchone()
            cached_filter = frow[0] if frow else ""
            if cached_filter != self._filter_key:
                logger.info("Route filter changed (%r -> %r), rebuilding", cached_filter, self._filter_key)
                return False

            logger.info("Using cached DB (%.0fs old)", age)
            self._loaded_at = time.time()
            return True
        except Exception:
            return False

    async def ensure_loaded(self) -> None:
        if self.is_loaded and not self.is_stale:
            return
        async with self._lock:
            if self.is_loaded and not self.is_stale:
                return
            if self._db_is_fresh():
                return
            if self.is_loaded:
                # DB exists but is stale — rebuild in the background so
                # queries keep using the old DB while the new one builds.
                # Mark as non-stale so we don't trigger another rebuild.
                self._loaded_at = time.time()
                asyncio.create_task(self._rebuild_in_background())
                return
            # No DB at all — must wait for the build to complete.
            await self._build_db()

    async def _rebuild_in_background(self) -> None:
        """Rebuild the DB without blocking query handling."""
        try:
            await self._build_db()
        except Exception:
            logger.exception("Background DB rebuild failed")

    async def _build_db(self) -> None:
        """Download GTFS + NaPTAN and build the SQLite database.

        Downloads are async (non-blocking).  The heavy parse/insert work runs
        in a worker thread so the event loop stays free for queries.  The old
        DB connection keeps serving requests until the new DB is ready, then
        we swap atomically.
        """
        logger.info("Building static data DB...")
        t0 = time.time()
        cache = _cache_dir()

        # Step 1: download files (async, non-blocking)
        gtfs_path = cache / "gtfs_download.zip"
        naptan_path = cache / "naptan_download.csv"
        async with httpx.AsyncClient(timeout=60) as client:
            await asyncio.gather(
                self._download_to_file(client, GTFS_URL, gtfs_path),
                self._download_to_file(client, NAPTAN_STOPS_URL, naptan_path),
            )

        # Step 2: build DB in a worker thread (CPU-heavy, would block the event loop)
        tmp_path = self._db_path.with_suffix(".tmp")
        await asyncio.to_thread(
            self._build_db_sync, gtfs_path, naptan_path, tmp_path
        )

        # Step 3: swap — old connection keeps serving until we replace it
        old_db = self._db
        self._db = None          # Next _get_db() call will open the new file
        tmp_path.replace(self._db_path)
        if old_db is not None:
            old_db.close()

        self._loaded_at = time.time()
        elapsed = time.time() - t0
        logger.info("DB built in %.1fs at %s", elapsed, self._db_path)

        # Clean up downloaded files
        gtfs_path.unlink(missing_ok=True)
        naptan_path.unlink(missing_ok=True)

    def _build_db_sync(self, gtfs_path: Path, naptan_path: Path, tmp_path: Path) -> None:
        """Synchronous DB build — called from a worker thread."""
        tmp_path.unlink(missing_ok=True)

        db = sqlite3.connect(str(tmp_path))
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=OFF")
        db.executescript(_SCHEMA)

        # --- Parse GTFS ---
        with zipfile.ZipFile(str(gtfs_path)) as zf:
            agencies = self._insert_agencies(db, zf)
            self._insert_routes(db, zf, agencies)

            # Resolve route filter to route_ids
            allowed_route_ids = self._resolve_filter(db)
            if allowed_route_ids is not None:
                logger.info("Route filter active: %d route(s) matched", len(allowed_route_ids))

            self._insert_stops_gtfs(db, zf)
            self._insert_calendar(db, zf)
            self._insert_calendar_dates(db, zf)
            trip_to_route, trip_to_rowid = self._insert_trips(db, zf, allowed_route_ids)
            self._insert_stop_times(db, zf, trip_to_route, trip_to_rowid)

        del trip_to_route, trip_to_rowid

        # --- Build stop_routes and route_stops from SQL ---
        self._build_stop_routes_table(db)
        self._build_route_stops_ordered(db)

        # Prune routes not matched by the filter
        if allowed_route_ids is not None:
            placeholders = ",".join("?" * len(allowed_route_ids))
            db.execute(f"DELETE FROM routes WHERE route_id NOT IN ({placeholders})", list(allowed_route_ids))

        # Only keep stops served by remaining routes
        db.execute("""
            DELETE FROM stops WHERE stop_id NOT IN (
                SELECT DISTINCT stop_id FROM stop_routes
            )
        """)

        # --- Parse NaPTAN (only for stops we kept) ---
        try:
            self._insert_naptan_from_file(db, naptan_path)
        except Exception:
            logger.warning("Failed to load NaPTAN data, continuing without it", exc_info=True)

        db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('built_at', ?)",
            (str(time.time()),),
        )
        db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('route_filter', ?)",
            (self._filter_key,),
        )
        db.commit()
        db.execute("VACUUM")
        db.close()

    @staticmethod
    async def _download_to_file(client: httpx.AsyncClient, url: str, path: Path) -> None:
        """Stream a download to disk instead of holding it in memory."""
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)

    def _resolve_filter(self, db: sqlite3.Connection) -> set[str] | None:
        """Resolve self._route_filter into a set of route_ids. Returns None if no filter."""
        if not self._route_filter:
            return None

        route_ids: set[str] = set()
        for entry in self._route_filter:
            lower = entry.lower()
            if lower in VALID_ROUTE_TYPES:
                # Match by route_type
                rows = db.execute(
                    "SELECT route_id FROM routes WHERE route_type = ?", (lower,)
                ).fetchall()
                route_ids.update(r[0] for r in rows)
            else:
                # Match by short_name (case-insensitive)
                rows = db.execute(
                    "SELECT route_id FROM routes WHERE short_name = ? COLLATE NOCASE", (entry,)
                ).fetchall()
                route_ids.update(r[0] for r in rows)

        if not route_ids:
            logger.warning("Route filter matched no routes: %s", self._route_filter)

        return route_ids

    # --- GTFS insert helpers ---

    def _insert_agencies(self, db: sqlite3.Connection, zf: zipfile.ZipFile) -> dict[str, str]:
        agencies: dict[str, str] = {}
        try:
            with zf.open("agency.txt") as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
                for row in reader:
                    agencies[row["agency_id"]] = row["agency_name"]
        except KeyError:
            pass
        return agencies

    def _insert_routes(self, db: sqlite3.Connection, zf: zipfile.ZipFile, agencies: dict[str, str]) -> None:
        with zf.open("routes.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            rows = []
            for row in reader:
                agency_id = row.get("agency_id", "")
                rows.append((
                    row["route_id"],
                    row.get("route_short_name", ""),
                    row.get("route_long_name", ""),
                    agencies.get(agency_id, agency_id),
                    route_type_name(int(row.get("route_type", "3"))),
                ))
            db.executemany(
                "INSERT OR REPLACE INTO routes VALUES (?,?,?,?,?)", rows
            )

    def _insert_stops_gtfs(self, db: sqlite3.Connection, zf: zipfile.ZipFile) -> None:
        with zf.open("stops.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            rows = []
            for row in reader:
                rows.append((
                    row["stop_id"],
                    row.get("stop_name", row["stop_id"]),
                    float(row.get("stop_lat", 0)),
                    float(row.get("stop_lon", 0)),
                    None,
                ))
            db.executemany(
                "INSERT OR IGNORE INTO stops VALUES (?,?,?,?,?)", rows
            )

    def _insert_calendar(self, db: sqlite3.Connection, zf: zipfile.ZipFile) -> None:
        try:
            with zf.open("calendar.txt") as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
                rows = []
                for row in reader:
                    start = row["start_date"].replace("-", "")
                    end = row["end_date"].replace("-", "")
                    rows.append((
                        row["service_id"], start, end,
                        int(row.get("monday", 0)), int(row.get("tuesday", 0)),
                        int(row.get("wednesday", 0)), int(row.get("thursday", 0)),
                        int(row.get("friday", 0)), int(row.get("saturday", 0)),
                        int(row.get("sunday", 0)),
                    ))
                db.executemany(
                    "INSERT OR REPLACE INTO calendar VALUES (?,?,?,?,?,?,?,?,?,?)", rows
                )
        except KeyError:
            logger.warning("No calendar.txt in GTFS data")

    def _insert_calendar_dates(self, db: sqlite3.Connection, zf: zipfile.ZipFile) -> None:
        try:
            with zf.open("calendar_dates.txt") as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
                rows = []
                for row in reader:
                    date_str = row["date"].replace("-", "")
                    rows.append((row["service_id"], date_str, int(row["exception_type"])))
                db.executemany(
                    "INSERT OR REPLACE INTO calendar_dates VALUES (?,?,?)", rows
                )
        except KeyError:
            logger.warning("No calendar_dates.txt in GTFS data")

    def _insert_trips(
        self, db: sqlite3.Connection, zf: zipfile.ZipFile,
        allowed_route_ids: set[str] | None = None,
    ) -> tuple[dict[str, str], dict[str, int]]:
        """Insert trips and return (trip_to_route, trip_to_rowid) mappings."""
        trip_to_route: dict[str, str] = {}
        with zf.open("trips.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            rows = []
            for row in reader:
                trip_id = row["trip_id"]
                route_id = row["route_id"]
                if allowed_route_ids is not None and route_id not in allowed_route_ids:
                    continue
                trip_to_route[trip_id] = route_id
                rows.append((trip_id, route_id, row["service_id"], row.get("trip_headsign", "")))
            db.executemany(
                "INSERT OR REPLACE INTO trips (trip_id, route_id, service_id, headsign) VALUES (?,?,?,?)", rows
            )

        # Build trip_id -> rowid mapping
        trip_to_rowid: dict[str, int] = {}
        for row in db.execute("SELECT trip_rowid, trip_id FROM trips"):
            trip_to_rowid[row[1]] = row[0]

        return trip_to_route, trip_to_rowid

    def _insert_stop_times(
        self, db: sqlite3.Connection, zf: zipfile.ZipFile,
        trip_to_route: dict[str, str], trip_to_rowid: dict[str, int],
    ) -> None:
        BATCH_SIZE = 50_000
        batch: list[tuple] = []

        with zf.open("stop_times.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                trip_id = row["trip_id"]
                if trip_id not in trip_to_route:
                    continue

                trip_rowid = trip_to_rowid.get(trip_id)
                if trip_rowid is None:
                    continue

                seq = int(row.get("stop_sequence", 0))
                arrival_time = row.get("arrival_time", "")
                if arrival_time:
                    parts = arrival_time.split(":")
                    h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
                    batch.append((trip_rowid, row["stop_id"], h, m, s, seq, h % 24))

                if len(batch) >= BATCH_SIZE:
                    db.executemany(
                        "INSERT INTO stop_times VALUES (?,?,?,?,?,?,?)", batch
                    )
                    batch.clear()

        if batch:
            db.executemany(
                "INSERT INTO stop_times VALUES (?,?,?,?,?,?,?)", batch
            )

    def _insert_naptan_from_file(self, db: sqlite3.Connection, path: Path) -> None:
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                atco = row.get("AtcoCode", "").strip()
                if not atco:
                    continue
                name = row.get("CommonName", "").strip()
                street = row.get("Street", "").strip() or None
                lat = float(row.get("Latitude", 0) or 0)
                lon = float(row.get("Longitude", 0) or 0)

                existing = db.execute(
                    "SELECT stop_id FROM stops WHERE stop_id = ?", (atco,)
                ).fetchone()
                if existing:
                    if street:
                        db.execute(
                            "UPDATE stops SET street = ? WHERE stop_id = ?",
                            (street, atco),
                        )
                else:
                    db.execute(
                        "INSERT INTO stops VALUES (?,?,?,?,?)",
                        (atco, name or atco, lat, lon, street),
                    )

    def _build_stop_routes_table(self, db: sqlite3.Connection) -> None:
        """Populate stop_routes from stop_times + trips."""
        db.execute("""
            INSERT OR IGNORE INTO stop_routes (stop_id, route_id)
            SELECT DISTINCT st.stop_id, t.route_id
            FROM stop_times st
            JOIN trips t ON st.trip_rowid = t.trip_rowid
        """)

    def _build_route_stops_ordered(self, db: sqlite3.Connection) -> None:
        """Pick the longest trip per route and store its stop order."""
        # Step 1: find the trip_rowid with the most stops per route
        best_trips = db.execute("""
            SELECT t.route_id, st.trip_rowid
            FROM stop_times st
            JOIN trips t ON st.trip_rowid = t.trip_rowid
            GROUP BY st.trip_rowid
            ORDER BY t.route_id, COUNT(*) DESC
        """).fetchall()

        # Keep only the first (longest) trip per route
        seen_routes: set[str] = set()
        best_rowids: list[int] = []
        for route_id, trip_rowid in best_trips:
            if route_id not in seen_routes:
                seen_routes.add(route_id)
                best_rowids.append(trip_rowid)

        # Step 2: insert stop sequences for those trips
        placeholders = ",".join("?" * len(best_rowids))
        db.execute(f"""
            INSERT INTO route_stops_ordered (route_id, stop_sequence, stop_id)
            SELECT t.route_id, st.stop_sequence, st.stop_id
            FROM stop_times st
            JOIN trips t ON st.trip_rowid = t.trip_rowid
            WHERE st.trip_rowid IN ({placeholders})
            ORDER BY t.route_id, st.stop_sequence
        """, best_rowids)

    # --- Calendar helpers ---

    def is_service_running(self, service_id: str, date: datetime.date) -> bool:
        """Check if a service_id is active on the given date."""
        db = self._get_db()
        date_str = date.strftime("%Y%m%d")

        exc = db.execute(
            "SELECT exception_type FROM calendar_dates WHERE service_id = ? AND date = ?",
            (service_id, date_str),
        ).fetchone()
        if exc:
            if exc[0] == 1:
                return True
            if exc[0] == 2:
                return False

        day_cols = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        day_col = day_cols[date.weekday()]
        row = db.execute(
            f"SELECT {day_col} FROM calendar WHERE service_id = ? AND start_date <= ? AND end_date >= ?",
            (service_id, date_str, date_str),
        ).fetchone()
        if row is None:
            return False
        return bool(row[0])

    def get_scheduled_stop_times(
        self,
        stop_id: str,
        now: datetime.datetime,
        max_minutes: int = 60,
        route_ids: set[str] | None = None,
    ) -> list[tuple[StopTimeEntry, datetime.datetime]]:
        """Get scheduled arrivals at a stop, filtered by calendar and time window."""
        try:
            import zoneinfo
            dublin_tz = zoneinfo.ZoneInfo("Europe/Dublin")
        except Exception:
            dublin_tz = datetime.timezone.utc

        now_local = now.astimezone(dublin_tz)
        today = now_local.date()
        now_naive = now_local.replace(tzinfo=None)

        start_hour = max(0, now_local.hour - 1)
        end_hour = now_local.hour + (max_minutes // 60) + 1
        check_hours = [h % 24 for h in range(start_hour, end_hour + 1)]

        db = self._get_db()
        placeholders = ",".join("?" * len(check_hours))

        if route_ids:
            route_placeholders = ",".join("?" * len(route_ids))
            sql = f"""
                SELECT t.trip_id, st.arrival_hour, st.arrival_min,
                       st.arrival_sec, st.stop_sequence, t.service_id
                FROM stop_times st
                JOIN trips t ON st.trip_rowid = t.trip_rowid
                WHERE st.stop_id = ?
                  AND st.hour_key IN ({placeholders})
                  AND t.route_id IN ({route_placeholders})
            """
            params: list = [stop_id, *check_hours, *route_ids]
        else:
            sql = f"""
                SELECT t.trip_id, st.arrival_hour, st.arrival_min,
                       st.arrival_sec, st.stop_sequence, t.service_id
                FROM stop_times st
                JOIN trips t ON st.trip_rowid = t.trip_rowid
                WHERE st.stop_id = ?
                  AND st.hour_key IN ({placeholders})
            """
            params = [stop_id, *check_hours]

        result: list[tuple[StopTimeEntry, datetime.datetime]] = []

        for row in db.execute(sql, params):
            trip_id, arr_h, arr_m, arr_s, seq, service_id = row

            if not self.is_service_running(service_id, today):
                continue

            arrival_td = datetime.timedelta(hours=arr_h, minutes=arr_m, seconds=arr_s)
            arrival_dt_naive = datetime.datetime(today.year, today.month, today.day) + arrival_td

            if (now_naive - arrival_dt_naive).total_seconds() > 12 * 3600:
                arrival_dt_naive += datetime.timedelta(days=1)

            if arrival_dt_naive < now_naive - datetime.timedelta(minutes=1):
                continue
            if arrival_dt_naive > now_naive + datetime.timedelta(minutes=max_minutes):
                continue

            arrival_dt = arrival_dt_naive.replace(tzinfo=dublin_tz)
            entry = StopTimeEntry(
                trip_id=trip_id,
                arrival_hour=arr_h,
                arrival_min=arr_m,
                arrival_sec=arr_s,
                stop_sequence=seq,
            )
            result.append((entry, arrival_dt))

        result.sort(key=lambda x: x[1])
        return result

    # --- Public query methods ---

    def search_stops(self, query: str, limit: int = 5) -> list[Stop]:
        db = self._get_db()
        q = f"%{query}%"
        rows = db.execute(
            """
            SELECT s.stop_id, s.name, s.latitude, s.longitude, s.street,
                   GROUP_CONCAT(DISTINCT r.short_name)
            FROM stops s
            LEFT JOIN stop_routes sr ON s.stop_id = sr.stop_id
            LEFT JOIN routes r ON sr.route_id = r.route_id
            WHERE s.name LIKE ? COLLATE NOCASE
            GROUP BY s.stop_id
            ORDER BY s.name NOT LIKE ? COLLATE NOCASE, s.name
            LIMIT ?
            """,
            (q, f"{query}%", limit),
        ).fetchall()

        results = []
        for row in rows:
            stop = Stop(
                stop_id=row[0], name=row[1], latitude=row[2],
                longitude=row[3], street=row[4],
            )
            if row[5]:
                stop.routes_served = sorted(row[5].split(","))
            results.append(stop)
        return results

    def search_routes(self, query: str, limit: int = 5) -> list[Route]:
        db = self._get_db()
        q = f"%{query}%"
        rows = db.execute(
            """
            SELECT route_id, short_name, long_name, agency, route_type
            FROM routes
            WHERE short_name LIKE ? COLLATE NOCASE
               OR long_name LIKE ? COLLATE NOCASE
            ORDER BY
                short_name != ? COLLATE NOCASE,
                short_name NOT LIKE ? COLLATE NOCASE,
                short_name
            LIMIT ?
            """,
            (q, q, query, f"{query}%", limit),
        ).fetchall()

        return [
            Route(route_id=r[0], short_name=r[1], long_name=r[2], agency=r[3], route_type=r[4])
            for r in rows
        ]

    def get_stop(self, stop_id: str) -> Stop | None:
        db = self._get_db()
        row = db.execute(
            "SELECT stop_id, name, latitude, longitude, street FROM stops WHERE stop_id = ?",
            (stop_id,),
        ).fetchone()
        if row is None:
            return None
        return Stop(stop_id=row[0], name=row[1], latitude=row[2], longitude=row[3], street=row[4])

    def get_route(self, route_id: str) -> Route | None:
        db = self._get_db()
        row = db.execute(
            "SELECT route_id, short_name, long_name, agency, route_type FROM routes WHERE route_id = ?",
            (route_id,),
        ).fetchone()
        if row is None:
            return None
        return Route(route_id=row[0], short_name=row[1], long_name=row[2], agency=row[3], route_type=row[4])

    def get_route_by_short_name(self, short_name: str) -> Route | None:
        db = self._get_db()
        row = db.execute(
            "SELECT route_id, short_name, long_name, agency, route_type FROM routes WHERE short_name = ? COLLATE NOCASE LIMIT 1",
            (short_name,),
        ).fetchone()
        if row is None:
            return None
        return Route(route_id=row[0], short_name=row[1], long_name=row[2], agency=row[3], route_type=row[4])

    def get_route_ids_by_short_name(self, short_name: str) -> list[str]:
        db = self._get_db()
        rows = db.execute(
            "SELECT route_id FROM routes WHERE short_name = ? COLLATE NOCASE",
            (short_name,),
        ).fetchall()
        return [r[0] for r in rows]

    def get_stops_for_route(self, route_id: str) -> list[Stop]:
        db = self._get_db()
        rows = db.execute(
            """
            SELECT s.stop_id, s.name, s.latitude, s.longitude, s.street
            FROM route_stops_ordered rs
            JOIN stops s ON rs.stop_id = s.stop_id
            WHERE rs.route_id = ?
            ORDER BY rs.stop_sequence
            """,
            (route_id,),
        ).fetchall()
        return [
            Stop(stop_id=r[0], name=r[1], latitude=r[2], longitude=r[3], street=r[4])
            for r in rows
        ]

    def get_routes_for_stop(self, stop_id: str) -> list[Route]:
        db = self._get_db()
        rows = db.execute(
            """
            SELECT r.route_id, r.short_name, r.long_name, r.agency, r.route_type
            FROM stop_routes sr
            JOIN routes r ON sr.route_id = r.route_id
            WHERE sr.stop_id = ?
            """,
            (stop_id,),
        ).fetchall()
        return [
            Route(route_id=r[0], short_name=r[1], long_name=r[2], agency=r[3], route_type=r[4])
            for r in rows
        ]

    def resolve_trip(self, trip_id: str) -> tuple[str | None, str | None]:
        """Return (route_short_name, headsign) for a trip_id."""
        db = self._get_db()
        row = db.execute(
            """
            SELECT r.short_name, t.headsign
            FROM trips t
            JOIN routes r ON t.route_id = r.route_id
            WHERE t.trip_id = ?
            """,
            (trip_id,),
        ).fetchone()
        if row is None:
            return None, None
        return row[0], row[1]

    def resolve_trip_route_id(self, trip_id: str) -> str | None:
        db = self._get_db()
        row = db.execute(
            "SELECT route_id FROM trips WHERE trip_id = ?", (trip_id,)
        ).fetchone()
        return row[0] if row else None

    def find_nearest_stop_name(self, lat: float, lon: float) -> str:
        """Find the name of the nearest stop (approximate, using bounding box)."""
        db = self._get_db()
        # ~0.01 degrees ≈ 1km bounding box first, then sort by distance
        delta = 0.01
        rows = db.execute(
            """
            SELECT name, latitude, longitude FROM stops
            WHERE latitude BETWEEN ? AND ?
              AND longitude BETWEEN ? AND ?
            """,
            (lat - delta, lat + delta, lon - delta, lon + delta),
        ).fetchall()

        if not rows:
            # Widen search
            delta = 0.05
            rows = db.execute(
                """
                SELECT name, latitude, longitude FROM stops
                WHERE latitude BETWEEN ? AND ?
                  AND longitude BETWEEN ? AND ?
                """,
                (lat - delta, lat + delta, lon - delta, lon + delta),
            ).fetchall()

        if not rows:
            return "unknown"

        # Simple squared-distance comparison (fine for small areas)
        best_name = rows[0][0]
        best_d = (rows[0][1] - lat) ** 2 + (rows[0][2] - lon) ** 2
        for name, slat, slon in rows[1:]:
            d = (slat - lat) ** 2 + (slon - lon) ** 2
            if d < best_d:
                best_d = d
                best_name = name
        return best_name
