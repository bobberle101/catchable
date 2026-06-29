"""Sensor platform for Catchable (generic GTFS-RT adapter)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import timedelta
from typing import Any

import aiohttp
from google.transit import gtfs_realtime_pb2

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DIRECTION,
    CONF_DIRECTIONS,
    CONF_MAX_DEPARTURES,
    CONF_SCAN_INTERVAL,
    CONF_SOURCE,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_TRANSPORT_TYPES,
    CONF_WALK_TIME,
    DEFAULT_DIRECTION,
    DEFAULT_MAX_DEPARTURES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SOURCE,
    DEFAULT_WALK_TIME,
    DIRECTION_LABELS,
    DOMAIN,
    TRANSPORT_TYPE_ORDER,
    direction_label,
    resolve_source,
)

_LOGGER = logging.getLogger(__name__)

_SEGMENT_SPLIT = re.compile(r"[:_\-.]")
_STATION_NUM = re.compile(r"\d{6,}")
_COMPONENT_DIR = os.path.dirname(__file__)


def _digits_only(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _stop_id_matches(configured_stop_id: str, incoming_stop_id: str) -> bool:
    """Match a configured stop id against a GTFS RT structured stop id.

    GTFS RT uses platform-level ids like ``de:12054:900230071::1`` while users
    configure the bare station number ``900230071``. Compare the configured id
    against each colon/dash/underscore separated segment, both literally and
    digits-only, so the region prefix (``12054``) never pollutes the match.
    """
    configured = configured_stop_id.strip()
    incoming = incoming_stop_id.strip()
    if not configured or not incoming:
        return False
    if configured == incoming:
        return True
    configured_digits = _digits_only(configured)
    for segment in _SEGMENT_SPLIT.split(incoming):
        if not segment:
            continue
        if segment == configured:
            return True
        if configured_digits and _digits_only(segment) == configured_digits:
            return True
    return False


def _station_number(stop_id: str) -> str:
    for part in stop_id.split(":"):
        if part.isdigit() and len(part) >= 6:
            return part
    match = _STATION_NUM.search(stop_id)
    return match.group(0) if match else _digits_only(stop_id)


def _route_type_of(route_id: str, routes: dict[str, Any] | None = None) -> int | None:
    if routes:
        entry = routes.get(route_id)
        if entry and len(entry) > 1 and entry[1] is not None:
            return int(entry[1])
    if "_" in route_id:
        suffix = route_id.rsplit("_", 1)[-1]
        if suffix.isdigit():
            return int(suffix)
    return None


def _route_category(route_type: int | None) -> str | None:
    """Map a GTFS route_type to a coarse transport category.

    Handles both the standard codes (0-12) and the extended Hafas codes in the
    100-1099 range (used by VBB).
    """
    if route_type is None:
        return None
    # Standard GTFS route types.
    if route_type < 100:
        return {
            0: "tram",
            1: "subway",
            2: "regional",
            3: "bus",
            4: "ferry",
            5: "tram",
            11: "bus",
            12: "subway",
        }.get(route_type)
    # Extended Hafas route types (used by VBB).
    if 400 <= route_type <= 499:
        return "subway"
    if route_type == 109:
        return "suburban"
    if 100 <= route_type <= 199:
        return "regional"
    if 700 <= route_type <= 799:
        return "bus"
    if 900 <= route_type <= 999:
        return "tram"
    if 1000 <= route_type <= 1099:
        return "ferry"
    return None


def _mode_label(route_type: int | None) -> str:
    category = _route_category(route_type)
    return {
        "subway": "U",
        "suburban": "S",
        "regional": "",
        "tram": "Tram",
        "bus": "Bus",
        "ferry": "Ferry",
    }.get(category or "", "")


def _format_line(route_id: str, routes: dict[str, Any]) -> str | None:
    short: str | None = None
    route_type: int | None = None
    entry = routes.get(route_id)
    if entry:
        short = entry[0] or None
        route_type = entry[1] if len(entry) > 1 else None
    elif "_" in route_id:
        suffix = route_id.rsplit("_", 1)[-1]
        if suffix.isdigit():
            route_type = int(suffix)
    mode = _mode_label(route_type)
    if short:
        return f"{mode} {short}".strip() if mode else short
    return mode or None


def _destination_name(stop_id: str, stops: dict[str, Any]) -> str | None:
    name = stops.get(_station_number(stop_id))
    if not name:
        return None
    if ", " in name:
        return name.split(", ", 1)[1]
    return name


def _load_json(path: str, *, optional: bool = False) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        # Optional lookups are absent for most sources; that is expected and not
        # worth a warning.
        log = _LOGGER.debug if optional else _LOGGER.warning
        log("Lookup %s not found", path)
        return {}
    except (OSError, ValueError) as err:
        _LOGGER.warning("Could not load lookup %s: %s", path, err)
        return {}


def _load_lookups(lookup_dir: str) -> dict[str, dict[str, Any]]:
    """Load the per-source lookups from ``sources/<name>/``.

    ``routes`` / ``stops`` exist for every source. ``stations`` (parent ->
    member platform ids) and ``trips`` (trip_id -> route_id) are optional and
    only present for feeds that need them (e.g. a feed whose realtime data omits
    ``route_id`` or references platform-level stop ids).
    """
    base = os.path.join(_COMPONENT_DIR, lookup_dir)
    return {
        "routes": _load_json(os.path.join(base, "routes.json")),
        "stops": _load_json(os.path.join(base, "stops.json")),
        "stations": _load_json(os.path.join(base, "stations.json"), optional=True),
        "trips": _load_json(os.path.join(base, "trips.json"), optional=True),
    }


def _resolve_route_id(trip: Any, trips: dict[str, Any]) -> str:
    """Return the route id, falling back to a trip_id -> route_id lookup.

    Some realtime feeds leave ``route_id`` empty and only provide a ``trip_id``;
    a bundled ``trips.json`` resolves it back to a route.
    """
    route_id = trip.route_id or ""
    if route_id:
        return route_id
    if trips:
        return trips.get(trip.trip_id or "", "")
    return ""


def _build_alias_set(configured_stop_id: str, stations: dict[str, Any]) -> set[str]:
    """Return the configured id plus any member platform ids (parent station)."""
    aliases = {configured_stop_id}
    members = stations.get(configured_stop_id)
    if isinstance(members, list):
        aliases.update(members)
    return aliases


def _stop_matches(
    configured_stop_id: str, incoming_stop_id: str, aliases: set[str]
) -> bool:
    """Match by exact alias membership or by structured id (VBB)."""
    incoming = incoming_stop_id.strip()
    if incoming and incoming in aliases:
        return True
    return _stop_id_matches(configured_stop_id, incoming_stop_id)


def _parse_feed(
    payload: bytes,
    stop_id: str,
    max_departures: int,
    walk_time: int,
    transport_types: list[str] | None,
    directions: list[str] | None,
    lookups: dict[str, dict[str, Any]],
    source: str,
    realtime_url: str,
) -> dict[str, Any] | None:
    """Parse a GTFS RT payload (CPU bound, runs in executor).

    Only events at least ``walk_time`` minutes away are kept, so the list
    reflects what is actually catchable on foot. If ``transport_types`` is set,
    only those categories are kept. ``directions`` selects departures and/or
    arrivals: a departure shows the trip destination, an arrival shows where the
    trip comes from. Each entry carries the line, the relevant place name,
    minutes-to-go, integer delay, cancellation state and the board ``kind``.
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(payload)
    now = dt_util.utcnow()

    routes = lookups.get("routes", {})
    stops = lookups.get("stops", {})
    stations = lookups.get("stations", {})
    trips = lookups.get("trips", {})
    aliases = _build_alias_set(stop_id, stations)

    allowed = set(transport_types) if transport_types else None
    wanted = set(directions) if directions else {"departures"}
    want_departures = "departures" in wanted
    want_arrivals = "arrivals" in wanted
    cancelled_rel = gtfs_realtime_pb2.TripDescriptor.CANCELED
    skipped_rel = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED

    collected: list[dict[str, Any]] = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        trip_update = entity.trip_update
        route_id = _resolve_route_id(trip_update.trip, trips)
        if allowed is not None:
            category = _route_category(_route_type_of(route_id, routes))
            if category not in allowed:
                continue
        line = _format_line(route_id, routes)
        trip_cancelled = trip_update.trip.schedule_relationship == cancelled_rel
        stop_updates = list(trip_update.stop_time_update)
        destination = (
            _destination_name(stop_updates[-1].stop_id or "", stops)
            if stop_updates
            else None
        )
        origin = (
            _destination_name(stop_updates[0].stop_id or "", stops)
            if stop_updates
            else None
        )

        for stop_update in stop_updates:
            incoming_stop_id = stop_update.stop_id or ""
            if not incoming_stop_id:
                continue
            if not _stop_matches(stop_id, incoming_stop_id, aliases):
                continue

            cancelled = (
                trip_cancelled
                or stop_update.schedule_relationship == skipped_rel
            )

            # A single stop can yield both a departure and an arrival entry.
            candidates: list[tuple[str, str, Any | None]] = []
            if want_departures:
                candidates.append(("departure", "departure", destination))
            if want_arrivals:
                candidates.append(("arrival", "arrival", origin))

            for kind, field, place in candidates:
                if not stop_update.HasField(field):
                    continue
                event = getattr(stop_update, field)
                if not event.time:
                    continue

                event_dt = dt_util.utc_from_timestamp(int(event.time))
                minutes = int((event_dt - now).total_seconds() / 60)
                if minutes < walk_time:
                    continue

                delay_seconds = event.delay if event.HasField("delay") else 0

                collected.append(
                    {
                        "line": line,
                        "direction": place,
                        "departure_in_min": minutes,
                        "delay_min": int(delay_seconds // 60),
                        "cancelled": cancelled,
                        "kind": kind,
                    }
                )

    if not collected:
        return None

    collected.sort(key=lambda item: item["departure_in_min"])
    limited = collected[:max_departures]
    next_minutes = next(
        (d["departure_in_min"] for d in limited if not d["cancelled"]),
        limited[0]["departure_in_min"] if limited else None,
    )
    return {
        "next_minutes": next_minutes,
        "departures": limited,
        "source": source,
        "source_endpoint": realtime_url,
    }


async def _async_fetch_from_gtfs_rt(
    hass: HomeAssistant,
    realtime_url: str,
    stop_id: str,
    max_departures: int,
    walk_time: int,
    transport_types: list[str] | None,
    directions: list[str] | None,
    errors: list[str],
    lookups: dict[str, dict[str, Any]],
    source: str,
) -> dict[str, Any] | None:
    """Fetch and parse a GTFS RT feed for the configured source."""
    session = async_get_clientsession(hass)

    try:
        async with asyncio.timeout(30):
            response = await session.get(realtime_url)
            response.raise_for_status()
            payload = await response.read()
    except (TimeoutError, aiohttp.ClientError) as err:
        errors.append(f"gtfs_rt: {err}")
        _LOGGER.warning("GTFS RT fetch error (%s): %s", source, err)
        return None

    try:
        return await hass.async_add_executor_job(
            _parse_feed,
            payload,
            stop_id,
            max_departures,
            walk_time,
            transport_types,
            directions,
            lookups,
            source,
            realtime_url,
        )
    except Exception as err:  # noqa: BLE001
        errors.append(f"gtfs_rt_parse: {err}")
        _LOGGER.warning("GTFS RT parse error (%s): %s", source, err)
        return None


def _detect_transport_types(
    payload: bytes, stop_id: str, lookups: dict[str, dict[str, Any]]
) -> list[str]:
    """Return the transport categories that currently serve a stop."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(payload)
    routes = lookups.get("routes", {})
    stations = lookups.get("stations", {})
    trips = lookups.get("trips", {})
    aliases = _build_alias_set(stop_id, stations)
    found: set[str] = set()
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        trip_update = entity.trip_update
        if not any(
            _stop_matches(stop_id, stu.stop_id or "", aliases)
            for stu in trip_update.stop_time_update
        ):
            continue
        route_id = _resolve_route_id(trip_update.trip, trips)
        category = _route_category(_route_type_of(route_id, routes))
        if category:
            found.add(category)
    return [c for c in TRANSPORT_TYPE_ORDER if c in found]


async def async_available_transport_types(
    hass: HomeAssistant, source: str, stop_id: str
) -> list[str]:
    """Fetch the feed once and detect which transport types serve a stop."""
    source_def = resolve_source(source)
    realtime_url = source_def["realtime_url"]
    lookups = await hass.async_add_executor_job(
        _load_lookups, source_def["lookup_dir"]
    )
    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(30):
            response = await session.get(realtime_url)
            response.raise_for_status()
            payload = await response.read()
    except (TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.warning("Transport-type detection fetch failed: %s", err)
        return []

    try:
        return await hass.async_add_executor_job(
            _detect_transport_types, payload, stop_id, lookups
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Transport-type detection parse failed: %s", err)
        return []


_CITY_PAREN = re.compile(r"\(([^)]+)\)\s*$")


def _derive_city(name: str) -> str:
    """Best-effort city from a stop name (``... (City)`` or ``Town, Stop``)."""
    match = _CITY_PAREN.search(name)
    if match:
        return match.group(1).strip()
    if ", " in name:
        return name.split(", ", 1)[0].strip()
    return name


def _load_station_index(lookup_dir: str) -> dict[str, tuple[str, str]]:
    """Return {stop_id: (name, city)} for the config-flow city/station picker.

    Sources ship a prebuilt ``stations_index.json`` ([name, city] per id, e.g.
    the full VBB network). Sources without one fall back to the runtime lookups:
    parent stations from ``stations`` when present, otherwise every ``stops``
    entry, with the city derived from the name.
    """
    base = os.path.join(_COMPONENT_DIR, lookup_dir)
    raw = _load_json(os.path.join(base, "stations_index.json"), optional=True)
    if raw:
        return {
            sid: (
                value[0],
                value[1] if len(value) > 1 and value[1] else _derive_city(value[0]),
            )
            for sid, value in raw.items()
        }

    stops = _load_json(os.path.join(base, "stops.json"))
    stations = _load_json(os.path.join(base, "stations.json"), optional=True)
    ids = stations.keys() if stations else stops.keys()
    return {sid: (stops.get(sid, sid), _derive_city(stops.get(sid, sid))) for sid in ids}


async def async_city_options(hass: HomeAssistant, source: str) -> list[str]:
    """Return the sorted list of distinct cities offered by a source."""
    source_def = resolve_source(source)
    index = await hass.async_add_executor_job(
        _load_station_index, source_def["lookup_dir"]
    )
    return sorted({city for _, city in index.values()}, key=str.lower)


async def async_stations_in_city(
    hass: HomeAssistant, source: str, city: str
) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Return (stop_id, name) pairs in a city plus the id->name map for that city.

    The id->name map lets the config flow resolve a friendly name for the picked
    stop id when building the entry title.
    """
    source_def = resolve_source(source)
    index = await hass.async_add_executor_job(
        _load_station_index, source_def["lookup_dir"]
    )
    items = [
        (sid, name) for sid, (name, station_city) in index.items() if station_city == city
    ]
    items.sort(key=lambda kv: (kv[1].lower(), kv[0]))
    return items, {sid: name for sid, name in items}


def _resolve_direction(entry: ConfigEntry) -> str:
    """Return the single board direction, tolerating the legacy list key."""
    value = entry.options.get(CONF_DIRECTION, entry.data.get(CONF_DIRECTION))
    if value in DIRECTION_LABELS:
        return value
    legacy = entry.options.get(CONF_DIRECTIONS, entry.data.get(CONF_DIRECTIONS))
    if isinstance(legacy, list) and legacy and legacy[0] in DIRECTION_LABELS:
        return legacy[0]
    if isinstance(legacy, str) and legacy in DIRECTION_LABELS:
        return legacy
    return DEFAULT_DIRECTION


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a Catchable departure board from a config entry."""

    def _option(key: str, default: int) -> int:
        return int(entry.options.get(key, entry.data.get(key, default)))

    stop_id = entry.data[CONF_STOP_ID]
    stop_name = entry.data[CONF_STOP_NAME]
    source = entry.data.get(CONF_SOURCE, DEFAULT_SOURCE)
    source_def = resolve_source(source)
    realtime_url = source_def["realtime_url"]
    lookup_dir = source_def["lookup_dir"]
    language = hass.config.language

    scan_interval = _option(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    max_departures = _option(CONF_MAX_DEPARTURES, DEFAULT_MAX_DEPARTURES)
    walk_time = _option(CONF_WALK_TIME, DEFAULT_WALK_TIME)
    transport_types = entry.options.get(
        CONF_TRANSPORT_TYPES, entry.data.get(CONF_TRANSPORT_TYPES)
    ) or None
    direction = _resolve_direction(entry)
    directions = [direction]

    lookups = await hass.async_add_executor_job(_load_lookups, lookup_dir)

    last_good: dict[str, Any] | None = None

    async def _update() -> dict[str, Any]:
        nonlocal last_good
        errors: list[str] = []
        refreshed_at = dt_util.utcnow().isoformat()

        gtfs_data = await _async_fetch_from_gtfs_rt(
            hass=hass,
            realtime_url=realtime_url,
            stop_id=stop_id,
            max_departures=max_departures,
            walk_time=walk_time,
            transport_types=transport_types,
            directions=directions,
            errors=errors,
            lookups=lookups,
            source=source,
        )
        if gtfs_data is not None:
            gtfs_data["stale"] = False
            gtfs_data["refreshed_at"] = refreshed_at
            last_good = gtfs_data
            return gtfs_data

        if last_good is not None:
            cached = dict(last_good)
            cached["stale"] = True
            cached["fallback_errors"] = errors
            cached["refreshed_at"] = refreshed_at
            return cached

        return {
            "next_minutes": None,
            "departures": [],
            "source": source,
            "source_endpoint": realtime_url,
            "stale": True,
            "fallback_errors": errors,
            "refreshed_at": refreshed_at,
        }

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_{source}_{stop_id}",
        update_interval=timedelta(seconds=scan_interval),
        update_method=_update,
    )
    # The coordinator's update_interval already schedules periodic polling; this
    # bootstrap just performs the first fetch.
    await coordinator.async_config_entry_first_refresh()
    sensor = CatchableDepartureSensor(
        coordinator,
        source,
        stop_id,
        stop_name,
        scan_interval,
        walk_time,
        transport_types,
        direction,
        language,
    )
    async_add_entities([sensor])


class CatchableDepartureSensor(CoordinatorEntity, SensorEntity, RestoreEntity):
    """Sensor for the next departure or arrival at a selected stop."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "min"

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        source: str,
        stop_id: str,
        stop_name: str,
        scan_interval: int,
        walk_time: int,
        transport_types: list[str] | None,
        direction: str,
        language: str | None = "en",
    ) -> None:
        super().__init__(coordinator)
        self._source = source
        self._stop_id = stop_id
        self._stop_name = stop_name
        self._scan_interval = int(scan_interval)
        self._walk_time = int(walk_time)
        self._transport_types = transport_types
        self._direction = direction if direction in DIRECTION_LABELS else DEFAULT_DIRECTION
        self._restored_data: dict[str, Any] | None = None
        # Direction is part of the name/id so departures and arrivals boards for
        # the same stop are distinct entities and self-describing in the UI.
        label = direction_label(self._direction, language)
        self._attr_name = f"{stop_name} {label}"
        self._attr_icon = (
            "mdi:tram" if self._direction == "departures" else "mdi:map-marker-down"
        )
        self._attr_unique_id = f"{source}_{stop_id}_{self._direction}"

    async def async_added_to_hass(self) -> None:
        """Restore last known departures for offline fallback across restarts."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None:
            return

        attrs = dict(last_state.attributes)
        departures = attrs.get("departures")
        if not isinstance(departures, list):
            departures = []

        restored_next = None
        try:
            if last_state.state not in ("unknown", "unavailable", "None", ""):
                restored_next = int(float(last_state.state))
        except (TypeError, ValueError):
            restored_next = None

        self._restored_data = {
            "next_minutes": restored_next,
            "departures": departures,
            "source": attrs.get("source", self._source),
            "source_endpoint": attrs.get("source_endpoint"),
            "stale": True,
            "fallback_errors": attrs.get("fallback_errors", []),
            "refreshed_at": attrs.get("refreshed_at"),
        }

    def _effective_data(self) -> dict[str, Any]:
        """Prefer live coordinator data when it has useful payload."""
        live = self.coordinator.data or {}
        if (
            live.get("departures")
            or live.get("next_minutes") is not None
            or live.get("refreshed_at") is not None
        ):
            return live
        return self._restored_data or live

    @property
    def available(self) -> bool:
        """Keep entity available when local cache exists."""
        return (
            self.coordinator.last_update_success
            or bool(self.coordinator.data)
            or bool(self._restored_data)
        )

    @property
    def native_value(self) -> int | None:
        """Return sensor value."""
        data = self._effective_data()
        return data.get("next_minutes")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""
        data = self._effective_data()
        return {
            "source": data.get("source", self._source),
            "stop_id": self._stop_id,
            "stop_name": self._stop_name,
            "source_endpoint": data.get("source_endpoint"),
            "scan_interval_seconds": self._scan_interval,
            "walk_time_minutes": self._walk_time,
            "transport_types": self._transport_types or "all",
            "direction": self._direction,
            "stale": data.get("stale", False),
            "fallback_errors": data.get("fallback_errors", []),
            "refreshed_at": data.get("refreshed_at"),
            "departures": data.get("departures", []),
        }
