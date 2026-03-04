"""Diagnostics support for Circadian Hue Scene Sync."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_APPLICATION_KEY

TO_REDACT = {CONF_APPLICATION_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    return {
        "entry": async_redact_data(
            {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            TO_REDACT,
        )
    }
