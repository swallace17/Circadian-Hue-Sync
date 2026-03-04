"""Button entities for Circadian Hue Scene Sync."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

ButtonAction = Callable[[object], Awaitable[object]]


@dataclass(frozen=True, kw_only=True)
class CircadianButtonDescription(ButtonEntityDescription):
    """Circadian Hue button description."""

    press_action: ButtonAction


BUTTONS: tuple[CircadianButtonDescription, ...] = (
    CircadianButtonDescription(
        key="full_sync",
        name="Full Sync",
        icon="mdi:sync",
        press_action=lambda manager: manager.async_full_sync(reason="button"),
    ),
    CircadianButtonDescription(
        key="remove_circadian_scenes",
        name="Remove Circadian Scenes",
        icon="mdi:delete-sweep",
        press_action=lambda manager: manager.async_remove_circadian_scenes(reason="button"),
    ),
    CircadianButtonDescription(
        key="rebuild_circadian_scenes",
        name="Rebuild Circadian Scenes",
        icon="mdi:refresh-circle",
        press_action=lambda manager: _async_rebuild(manager),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button entities for a config entry."""
    async_add_entities(
        CircadianActionButton(hass, entry.entry_id, description)
        for description in BUTTONS
    )


class CircadianActionButton(ButtonEntity):
    """Button to run a Circadian Hue action."""

    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        description: CircadianButtonDescription,
    ) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def available(self) -> bool:
        return self._get_manager() is not None

    async def async_press(self) -> None:
        """Run button action."""
        manager = self._get_manager()
        if manager is None:
            raise HomeAssistantError("Integration manager is not available")
        await self.entity_description.press_action(manager)

    def _get_manager(self):
        return self.hass.data.get(DOMAIN, {}).get(self._entry_id, {}).get("manager")


async def _async_rebuild(manager: object) -> None:
    await manager.async_remove_circadian_scenes(reason="button")
    await manager.async_full_sync(reason="button")
