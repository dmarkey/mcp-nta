"""Domain models for the NTA MCP server."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Stop:
    stop_id: str
    name: str
    latitude: float
    longitude: float
    street: str | None = None
    routes_served: list[str] = field(default_factory=list)


@dataclass
class Route:
    route_id: str
    short_name: str
    long_name: str
    agency: str
    route_type: str  # "bus", "rail", "tram"


@dataclass
class Departure:
    route: str
    destination: str
    scheduled: datetime
    predicted: datetime
    delay_seconds: int
    status: str  # "on time", "late", "early", "cancelled"


@dataclass
class VehiclePosition:
    route: str
    destination: str
    latitude: float
    longitude: float
    speed_kmh: float | None
    bearing: float | None
    near: str


@dataclass
class Alert:
    headline: str
    description: str
    affected_routes: list[str] = field(default_factory=list)
    affected_stops: list[str] = field(default_factory=list)
    start: datetime | None = None
    end: datetime | None = None
