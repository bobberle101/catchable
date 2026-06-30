"""Catchable — a generic GTFS-RT departure board for Home Assistant."""

from __future__ import annotations

import logging
import os

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]

CARD_FILENAME = "catchable-departures-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILENAME}"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve and auto-register the bundled Lovelace card.

    This means users do not have to add a dashboard resource by hand — the card
    is served from the integration and loaded as a frontend module.

    The static path is served *with* cache headers and the module is referenced
    by a version-stamped URL (``?v=<version>``). This lets the browser cache the
    card so a frontend reload (e.g. right after a Home Assistant restart) serves
    it from cache instead of depending on a fresh fetch — that fetch can briefly
    404 in the window before this static route is re-registered, which otherwise
    leaves the custom element undefined and the cards stuck on "Configuration
    error" until a manual reload. Bumping the integration version busts the
    cache, so updates still propagate immediately.
    """
    card_path = os.path.join(os.path.dirname(__file__), CARD_FILENAME)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, card_path, cache_headers=True)]
    )
    integration = await async_get_integration(hass, DOMAIN)
    add_extra_js_url(hass, f"{CARD_URL}?v={integration.version}")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Catchable config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
