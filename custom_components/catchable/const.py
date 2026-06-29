"""Constants for the Catchable integration."""

from typing import Any

DOMAIN = "catchable"
NAME = "Catchable"

CONF_STOP_ID = "stop_id"
CONF_STOP_NAME = "stop_name"
# Transient config-flow fields: a searchable city picker narrows the station
# list, then a station picker whose value is the stop id. Neither is persisted
# in the entry (we store CONF_STOP_ID/CONF_STOP_NAME).
CONF_CITY = "city"
CONF_STATION = "station"
CONF_SCAN_INTERVAL = "scan_interval_seconds"
CONF_MAX_DEPARTURES = "max_departures"
CONF_WALK_TIME = "walk_time_minutes"
CONF_TRANSPORT_TYPES = "transport_types"
CONF_TYPES_ALL = "all_transport_types"
# A board is configured for exactly one data source and one direction.
CONF_SOURCE = "source"
# ``directions`` (plural, a list) is the legacy key kept only for reading
# older config entries.
CONF_DIRECTION = "direction"
CONF_DIRECTIONS = "directions"

DEFAULT_SCAN_INTERVAL = 90
DEFAULT_MAX_DEPARTURES = 8
DEFAULT_WALK_TIME = 5
DEFAULT_DIRECTION = "departures"
DEFAULT_DIRECTIONS = ["departures"]
DEFAULT_SOURCE = "vbb"

# Board direction, with English display labels (fallback for entity names).
DIRECTION_LABELS = {
    "departures": "Departures",
    "arrivals": "Arrivals",
}
DIRECTION_ORDER = ["departures", "arrivals"]

# Localized direction words used for entity names, keyed by HA language.
DIRECTION_LABELS_BY_LANG = {
    "en": {"departures": "Departures", "arrivals": "Arrivals"},
    "de": {"departures": "Abfahrten", "arrivals": "Ankünfte"},
}


def direction_label(direction: str, language: str | None = "en") -> str:
    """Return the localized direction word, falling back to English."""
    lang = (language or "en").split("-")[0].lower()
    table = DIRECTION_LABELS_BY_LANG.get(lang, DIRECTION_LABELS_BY_LANG["en"])
    return table.get(direction, table["departures"])


# Transport categories derived from GTFS route_type, with icon + display labels.
TRANSPORT_TYPE_LABELS = {
    "subway": "🚇 U-Bahn",
    "suburban": "🚈 S-Bahn",
    "regional": "🚆 Train",
    "tram": "🚊 Tram",
    "bus": "🚌 Bus",
    "ferry": "⛴️ Ferry",
}
TRANSPORT_TYPE_ORDER = ["subway", "suburban", "regional", "tram", "bus", "ferry"]

VBB_GTFS_RT_URL = "https://production.gtfsrt.vbb.de/data"

# Registry of GTFS-RT data sources. The adapter is generic; each source is just
# a realtime feed URL plus a directory of bundled name lookups
# (routes.json / stops.json, plus an optional stations_index.json for the
# config-flow city/station picker) scoped to a region to keep them small.
#
# ``enabled`` controls whether a source is offered in the config flow; when a
# single source is enabled, the source picker step is skipped automatically.
# New regions only need a feed URL + a lookup folder under ``sources/<key>/``.
GTFS_RT_SOURCES: dict[str, dict[str, Any]] = {
    "vbb": {
        "name": "VBB (Berlin-Brandenburg)",
        "realtime_url": VBB_GTFS_RT_URL,
        "lookup_dir": "sources/vbb",
        "enabled": True,
    },
}


def enabled_sources() -> dict[str, dict[str, Any]]:
    """Return only the sources offered in the config flow."""
    return {key: defn for key, defn in GTFS_RT_SOURCES.items() if defn.get("enabled")}


def resolve_source(source: str | None) -> dict[str, Any]:
    """Return the source definition, falling back to the default source."""
    return GTFS_RT_SOURCES.get(source or DEFAULT_SOURCE, GTFS_RT_SOURCES[DEFAULT_SOURCE])
