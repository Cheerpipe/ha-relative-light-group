"""Provide the functionality to create relative light groups."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENTITIES, Platform
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from . import config_flow as config_flow_pre_import  # noqa: F401
from .const import CONF_HIDE_MEMBERS, DOMAIN
from .services import async_register_services
from .visibility import restore_member_visibility_if_hidden_by_integration


PLATFORMS = [Platform.LIGHT]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Relative Groups integration."""
    hass.data.setdefault(DOMAIN, {})
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove a config entry."""
    # Unhide the group members
    if not entry.options.get(CONF_HIDE_MEMBERS, False):
        return

    restore_member_visibility_if_hidden_by_integration(
        hass, list(entry.options.get(CONF_ENTITIES, []))
    )
