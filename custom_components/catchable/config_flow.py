"""Config flow for Catchable (generic GTFS-RT adapter)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_CITY,
    CONF_DIRECTION,
    CONF_MAX_DEPARTURES,
    CONF_SOURCE,
    CONF_STATION,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_TRANSPORT_TYPES,
    CONF_TYPES_ALL,
    CONF_WALK_TIME,
    DEFAULT_DIRECTION,
    DEFAULT_MAX_DEPARTURES,
    DEFAULT_SOURCE,
    DEFAULT_WALK_TIME,
    DIRECTION_LABELS,
    DIRECTION_ORDER,
    DOMAIN,
    TRANSPORT_TYPE_LABELS,
    TRANSPORT_TYPE_ORDER,
    enabled_sources,
)
from .sensor import (
    async_available_transport_types,
    async_city_options,
    async_stations_in_city,
)

# Pick which GTFS-RT feed (region) a board reads from. Fixed at creation.
# Only enabled sources are offered; when a single source is enabled the picker
# step is skipped entirely.
_SOURCE_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=[
            SelectOptionDict(value=key, label=definition["name"])
            for key, definition in enabled_sources().items()
        ],
        multiple=False,
        mode=SelectSelectorMode.DROPDOWN,
    )
)

_WALK_TIME_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=0, max=120, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="Minutes"
    )
)
_MAX_DEP_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=3,
        max=20,
        step=1,
        mode=NumberSelectorMode.BOX,
        unit_of_measurement="departures",
    )
)
# A stop board shows exactly one direction; a radio list makes that explicit.
_DIRECTION_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=[
            SelectOptionDict(value=key, label=DIRECTION_LABELS[key])
            for key in DIRECTION_ORDER
        ],
        multiple=False,
        mode=SelectSelectorMode.LIST,
    )
)


def _type_order(available: list[str]) -> list[str]:
    """Return all type keys, available ones first (input list untouched)."""
    keys = list(available) if available else list(TRANSPORT_TYPE_ORDER)
    for key in TRANSPORT_TYPE_ORDER:
        if key not in keys:
            keys.append(key)
    return keys


def _type_selector(keys: list[str]) -> SelectSelector:
    """Build a checkbox list selector (icon + name per option)."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=key, label=TRANSPORT_TYPE_LABELS[key])
                for key in keys
            ],
            multiple=True,
            mode=SelectSelectorMode.LIST,
        )
    )


class CatchableConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Catchable."""

    VERSION = 1

    def __init__(self) -> None:
        self._source: str = DEFAULT_SOURCE
        self._city: str = ""
        self._stop_data: dict[str, Any] = {}
        self._available_types: list[str] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick the data source (region); city and station options depend on it.

        With a single enabled source the picker is pointless, so we select it
        automatically and jump straight to the city step.
        """
        sources = enabled_sources()
        if len(sources) == 1:
            self._source = next(iter(sources))
            return await self.async_step_city()

        if user_input is not None:
            self._source = user_input[CONF_SOURCE]
            return await self.async_step_city()

        default = DEFAULT_SOURCE if DEFAULT_SOURCE in sources else next(iter(sources))
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_SOURCE, default=default): _SOURCE_SELECTOR}
            ),
        )

    async def async_step_city(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick a city (searchable); it narrows the station list that follows."""
        errors: dict[str, str] = {}
        cities = await async_city_options(self.hass, self._source)
        city_selector = SelectSelector(
            SelectSelectorConfig(
                options=[SelectOptionDict(value=city, label=city) for city in cities],
                multiple=False,
                # Searchable dropdown: type the city name to filter the list.
                mode=SelectSelectorMode.DROPDOWN,
            )
        )

        if user_input is not None:
            city = str(user_input[CONF_CITY]).strip()
            if not city:
                errors["base"] = "invalid_city"
            else:
                self._city = city
                return await self.async_step_station()

        return self.async_show_form(
            step_id="city",
            data_schema=vol.Schema({vol.Required(CONF_CITY): city_selector}),
            errors=errors,
        )

    async def async_step_station(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick a station in the chosen city (id resolved), then settings."""
        errors: dict[str, str] = {}
        options, names = await async_stations_in_city(
            self.hass, self._source, self._city
        )
        station_selector = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=stop_id, label=name)
                    for stop_id, name in options
                ],
                multiple=False,
                # A searchable dropdown: the user types the station name and the
                # underlying stop id (the option value) is stored automatically.
                # custom_value is intentionally off so the field shows the
                # station name rather than the raw id.
                mode=SelectSelectorMode.DROPDOWN,
            )
        )

        if user_input is not None:
            stop_id = str(user_input[CONF_STATION]).strip()
            if not stop_id:
                errors["base"] = "invalid_input"
            else:
                self._stop_data = {
                    CONF_SOURCE: self._source,
                    CONF_STOP_ID: stop_id,
                    CONF_STOP_NAME: names.get(stop_id, stop_id),
                    CONF_MAX_DEPARTURES: int(user_input[CONF_MAX_DEPARTURES]),
                    CONF_WALK_TIME: int(user_input[CONF_WALK_TIME]),
                }
                self._available_types = await async_available_transport_types(
                    self.hass, self._source, stop_id
                )
                return await self.async_step_filters()

        return self.async_show_form(
            step_id="station",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STATION): station_selector,
                    vol.Required(
                        CONF_WALK_TIME, default=DEFAULT_WALK_TIME
                    ): _WALK_TIME_SELECTOR,
                    vol.Required(
                        CONF_MAX_DEPARTURES, default=DEFAULT_MAX_DEPARTURES
                    ): _MAX_DEP_SELECTOR,
                }
            ),
            description_placeholders={"city": self._city},
            errors=errors,
        )

    async def async_step_filters(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick transport types (checkboxes) and the board direction."""
        keys = _type_order(self._available_types)

        if user_input is not None:
            direction = user_input[CONF_DIRECTION]
            source = self._stop_data[CONF_SOURCE]
            stop_id = self._stop_data[CONF_STOP_ID]
            # One board per source per stop per direction; all three form the id.
            await self.async_set_unique_id(f"{source}_{stop_id}_{direction}")
            self._abort_if_unique_id_configured()

            data = dict(self._stop_data)
            data[CONF_TRANSPORT_TYPES] = (
                None if user_input[CONF_TYPES_ALL] else user_input[CONF_TRANSPORT_TYPES]
            )
            data[CONF_DIRECTION] = direction
            title = f"{self._stop_data[CONF_STOP_NAME]} {DIRECTION_LABELS[direction]}"
            return self.async_create_entry(title=title, data=data)

        default_types = self._available_types or list(keys)
        return self.async_show_form(
            step_id="filters",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DIRECTION, default=DEFAULT_DIRECTION
                    ): _DIRECTION_SELECTOR,
                    vol.Required(CONF_TYPES_ALL, default=True): BooleanSelector(),
                    vol.Required(
                        CONF_TRANSPORT_TYPES, default=default_types
                    ): _type_selector(keys),
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> CatchableOptionsFlow:
        """Get the options flow for this handler."""
        return CatchableOptionsFlow()


class CatchableOptionsFlow(config_entries.OptionsFlow):
    """Handle options for an existing stop (walk time, count, transport types)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage the options."""
        data = self.config_entry.data
        options = self.config_entry.options

        def _current(key: str, default: Any) -> Any:
            return options.get(key, data.get(key, default))

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_WALK_TIME: int(user_input[CONF_WALK_TIME]),
                    CONF_MAX_DEPARTURES: int(user_input[CONF_MAX_DEPARTURES]),
                    CONF_TRANSPORT_TYPES: (
                        None
                        if user_input[CONF_TYPES_ALL]
                        else user_input[CONF_TRANSPORT_TYPES]
                    ),
                },
            )

        available = await async_available_transport_types(
            self.hass, data.get(CONF_SOURCE, DEFAULT_SOURCE), data[CONF_STOP_ID]
        )
        keys = _type_order(available)
        current_types = _current(CONF_TRANSPORT_TYPES, None)
        all_selected = not current_types
        # Make sure every saved type is offered even if absent from the snapshot.
        for key in current_types or []:
            if key not in keys and key in TRANSPORT_TYPE_LABELS:
                keys.append(key)

        # Direction is fixed at creation (it defines the entity), so it is not
        # editable here — add a second stop for the other direction instead.
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_WALK_TIME,
                        default=int(_current(CONF_WALK_TIME, DEFAULT_WALK_TIME)),
                    ): _WALK_TIME_SELECTOR,
                    vol.Required(
                        CONF_MAX_DEPARTURES,
                        default=int(
                            _current(CONF_MAX_DEPARTURES, DEFAULT_MAX_DEPARTURES)
                        ),
                    ): _MAX_DEP_SELECTOR,
                    vol.Required(
                        CONF_TYPES_ALL, default=all_selected
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_TRANSPORT_TYPES,
                        default=current_types or list(keys),
                    ): _type_selector(keys),
                }
            ),
        )
