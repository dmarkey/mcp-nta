"""get_service_alerts — active service alerts."""

from __future__ import annotations

from datetime import datetime, timezone

from ..models import Alert
from ..realtime import RealtimeClient
from ..static_data import StaticDataManager


async def get_service_alerts(
    static: StaticDataManager,
    realtime: RealtimeClient,
    route: str | None = None,
    stop_id: str | None = None,
) -> str:
    await static.ensure_loaded()

    route_ids: set[str] | None = None
    if route:
        route_ids = set(static.get_route_ids_by_short_name(route))

    feed = await realtime.get_alerts()
    alerts: list[Alert] = []
    total_alerts = 0

    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        total_alerts += 1
        a = entity.alert

        affected_route_ids: list[str] = []
        affected_stop_ids: list[str] = []
        for ie in a.informed_entity:
            if ie.route_id:
                affected_route_ids.append(ie.route_id)
            if ie.stop_id:
                affected_stop_ids.append(ie.stop_id)

        # Filter
        if route_ids and not (set(affected_route_ids) & route_ids):
            continue
        if stop_id and stop_id not in affected_stop_ids:
            continue

        # Resolve names
        route_names = []
        for rid in affected_route_ids:
            r = static.get_route(rid)
            route_names.append(r.short_name if r else rid)

        stop_names = []
        for sid in affected_stop_ids:
            s = static.get_stop(sid)
            stop_names.append(s.name if s else sid)

        headline = ""
        description = ""
        for ts in a.header_text.translation:
            if ts.language in ("en", "EN", ""):
                headline = ts.text
                break
        if not headline and a.header_text.translation:
            headline = a.header_text.translation[0].text

        for ts in a.description_text.translation:
            if ts.language in ("en", "EN", ""):
                description = ts.text
                break
        if not description and a.description_text.translation:
            description = a.description_text.translation[0].text

        start_dt = None
        end_dt = None
        for period in a.active_period:
            if period.start:
                start_dt = datetime.fromtimestamp(period.start, tz=timezone.utc)
            if period.end:
                end_dt = datetime.fromtimestamp(period.end, tz=timezone.utc)
            break

        alerts.append(
            Alert(
                headline=headline,
                description=description,
                affected_routes=route_names,
                affected_stops=stop_names,
                start=start_dt,
                end=end_dt,
            )
        )

    if not alerts:
        # The NTA GTFS-Realtime feed does not currently publish a service alerts
        # feed — the "combined" endpoint is an alias of TripUpdates and carries
        # no alert entities. Distinguish that (nothing to report because the
        # source is empty) from "the feed has alerts, none match this filter",
        # so callers don't read silence as a confirmed all-clear.
        if total_alerts == 0:
            return (
                "Service alerts are not available: NTA's GTFS-Realtime feed does not "
                "publish service alerts, so disruptions cannot be confirmed or ruled "
                "out from this data source. Check the operator's own service updates."
            )
        if route:
            filter_desc = f" affecting route {route}"
        elif stop_id:
            s = static.get_stop(stop_id)
            filter_desc = f" affecting {s.name if s else stop_id}"
        else:
            filter_desc = ""
        return (
            f"No active service alerts{filter_desc} "
            f"({total_alerts} alert(s) active on other routes/stops)."
        )

    filter_desc = ""
    if route:
        filter_desc = f" affecting route {route}"
    lines = [f"{len(alerts)} active alert(s){filter_desc}:\n"]
    for i, alert in enumerate(alerts, 1):
        text = alert.headline or alert.description
        if alert.description and alert.headline:
            text = f"{alert.headline} — {alert.description}"
        if alert.affected_routes:
            text += f"\n   Routes: {', '.join(alert.affected_routes)}"
        if alert.end:
            text += f"\n   Until: {alert.end.strftime('%Y-%m-%d %H:%M')}"
        lines.append(f"{i}. {text}\n")
    return "\n".join(lines)
