"""Catchable — a generic GTFS-RT departure board for Home Assistant."""

from __future__ import annotations

import logging
import os

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]

CARD_FILENAME = "catchable-departures-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILENAME}"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the bundled Lovelace card and register it so it loads everywhere.

    Users do not have to add a dashboard resource by hand — the card is served
    from the integration and registered automatically.

    The card is registered as a Lovelace *resource* rather than only via
    ``add_extra_js_url``. This is the crucial robustness fix: extra-JS modules are
    imported once during the frontend bootstrap and are **not** re-imported when
    Lovelace rebuilds the dashboard (a websocket reconnect, a companion-app
    webview restore, or a view rebuild "after a while"). On any such rebuild the
    custom element is momentarily undefined and Home Assistant bakes in a
    permanent "Configuration error" card until the page is fully reloaded.
    Lovelace *resources*, by contrast, are reloaded on every rebuild, so the
    element stays defined and the cards keep working.

    The resource URL is version-stamped (``?v=<version>``) so the browser can
    cache it; bumping the integration version busts the cache and propagates
    updates immediately. When resources cannot be managed programmatically
    (YAML-mode dashboards expose a read-only resource collection) we fall back to
    ``add_extra_js_url`` so the card still loads.
    """
    card_path = os.path.join(os.path.dirname(__file__), CARD_FILENAME)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, card_path, cache_headers=True)]
    )

    integration = await async_get_integration(hass, DOMAIN)
    card_url = f"{CARD_URL}?v={integration.version}"

    async def _register_card(_hass: HomeAssistant) -> None:
        """Register the card once Home Assistant (and Lovelace) has started."""
        if not await _async_register_resource(hass, card_url):
            # YAML-mode resources or Lovelace not available: best-effort fallback.
            add_extra_js_url(hass, card_url)

    # Defer until startup completes so the Lovelace resource collection exists and
    # is loaded. ``async_at_started`` fires immediately if HA is already running
    # (e.g. the integration is set up after a reload).
    async_at_started(hass, _register_card)
    return True


async def _async_register_resource(hass: HomeAssistant, url: str) -> bool:
    """Create or update the card's Lovelace resource. Returns True on success.

    Returns False (so the caller can fall back) when the resource collection is
    missing or read-only (YAML mode), or anything unexpected goes wrong — setup
    must never fail because of a frontend convenience.
    """
    try:
        lovelace = hass.data.get("lovelace")
        if lovelace is None:
            return False
        resources = (
            lovelace.get("resources")
            if isinstance(lovelace, dict)
            else getattr(lovelace, "resources", None)
        )
        # YAML-mode collections have no create/update methods.
        if resources is None or not hasattr(resources, "async_create_item"):
            return False

        if not getattr(resources, "loaded", True):
            await resources.async_load()
            resources.loaded = True

        base = url.split("?", 1)[0]
        for item in resources.async_items():
            if item.get("url", "").split("?", 1)[0] == base:
                if item.get("url") != url:
                    await resources.async_update_item(item["id"], {"url": url})
                return True

        await resources.async_create_item({"res_type": "module", "url": url})
        return True
    except Exception:  # noqa: BLE001 — never let this break setup
        _LOGGER.exception("Failed to register the Catchable Lovelace resource")
        return False


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
