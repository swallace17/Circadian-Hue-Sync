"""Minimal config flow for Circadian Hue Scene Sync."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries

from .const import CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL, DOMAIN


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Circadian Hue Scene Sync."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            await self.async_set_unique_id("singleton")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="Circadian Hue Scene Sync",
                data={
                    CONF_VERIFY_SSL: bool(user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)),
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
                }
            ),
        )
