"""Circadian Hue Scene Sync integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)


def _get_runtime(hass: HomeAssistant, entry_id: str) -> dict[str, Any] | None:
    return hass.data.get(DOMAIN, {}).get(entry_id)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up integration services."""
    hass.data.setdefault(DOMAIN, {})
    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up integration from a config entry."""
    from homeassistant.core import Event, callback
    from homeassistant.exceptions import ConfigEntryNotReady
    from homeassistant.helpers.event import async_track_state_change_event

    from .const import EVENT_AREA_REGISTRY_UPDATED
    from .scene_manager import MultiBridgeSceneSyncManager, SceneSyncError

    manager = MultiBridgeSceneSyncManager(hass, entry)
    try:
        await manager.async_validate_connections()
    except SceneSyncError as err:
        raise ConfigEntryNotReady(f"Hue integrations unavailable: {err}") from err

    _async_register_services(hass)

    runtime: dict[str, Any] = {"manager": manager, "unsubscribers": []}

    if manager.auto_update_on_circadian_change:

        @callback
        def _handle_circadian_change(event: Event) -> None:
            if event.data.get("old_state") == event.data.get("new_state"):
                return
            hass.async_create_task(_run_sync_safe(manager, "circadian_state_change"))

        runtime["unsubscribers"].append(
            async_track_state_change_event(hass, [manager.circadian_sensor_entity], _handle_circadian_change)
        )

    if manager.auto_create_on_area_change:

        @callback
        def _handle_area_registry_updated(_: Event) -> None:
            hass.async_create_task(_run_create_safe(manager, "area_registry_updated"))

        runtime["unsubscribers"].append(
            hass.bus.async_listen(EVENT_AREA_REGISTRY_UPDATED, _handle_area_registry_updated)
        )

    if manager.update_on_startup:
        hass.async_create_task(_run_full_sync_safe(manager, "startup"))

    hass.data[DOMAIN][entry.entry_id] = runtime
    entry.async_on_unload(entry.add_update_listener(async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    from .const import (
        SERVICE_CREATE_MISSING_SCENES,
        SERVICE_FULL_SYNC,
        SERVICE_REMOVE_CIRCADIAN_SCENES,
        SERVICE_SYNC_SCENES,
    )

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    runtime = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if runtime:
        for unsub in runtime.get("unsubscribers", []):
            unsub()

    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_CREATE_MISSING_SCENES)
        hass.services.async_remove(DOMAIN, SERVICE_SYNC_SCENES)
        hass.services.async_remove(DOMAIN, SERVICE_FULL_SYNC)
        hass.services.async_remove(DOMAIN, SERVICE_REMOVE_CIRCADIAN_SCENES)

    return unload_ok


async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _run_full_sync_safe(manager: Any, reason: str) -> None:
    try:
        await manager.async_full_sync(reason=reason)
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Full sync failed (%s): %s", reason, err)


async def _run_create_safe(manager: Any, reason: str) -> None:
    try:
        await manager.async_create_missing_scenes(reason=reason)
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Create-missing-scenes failed (%s): %s", reason, err)


async def _run_sync_safe(manager: Any, reason: str) -> None:
    try:
        await manager.async_sync_scenes(reason=reason)
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Scene sync failed (%s): %s", reason, err)


def _async_register_services(hass: HomeAssistant) -> None:
    import voluptuous as vol

    from homeassistant.core import ServiceCall, callback

    from .const import (
        ATTR_ENTRY_ID,
        SERVICE_CREATE_MISSING_SCENES,
        SERVICE_FULL_SYNC,
        SERVICE_REMOVE_CIRCADIAN_SCENES,
        SERVICE_SYNC_SCENES,
    )

    service_schema = vol.Schema({vol.Optional(ATTR_ENTRY_ID): str})

    @callback
    def _select_entry_ids(service_call: ServiceCall) -> list[str]:
        entry_id = service_call.data.get("entry_id") or service_call.data.get(ATTR_ENTRY_ID)
        if entry_id:
            return [entry_id]
        return list(hass.data.get(DOMAIN, {}).keys())

    async def _for_each_target(service_call: ServiceCall, action: str) -> None:
        for entry_id in _select_entry_ids(service_call):
            runtime = _get_runtime(hass, entry_id)
            if runtime is None:
                _LOGGER.warning("Config entry %s not loaded; skipping service %s", entry_id, action)
                continue

            manager = runtime.get("manager")
            if manager is None:
                _LOGGER.warning("Config entry %s has no runtime manager; skipping service %s", entry_id, action)
                continue

            try:
                if action == SERVICE_CREATE_MISSING_SCENES:
                    await manager.async_create_missing_scenes(reason="service")
                elif action == SERVICE_SYNC_SCENES:
                    await manager.async_sync_scenes(reason="service")
                elif action == SERVICE_FULL_SYNC:
                    await manager.async_full_sync(reason="service")
                elif action == SERVICE_REMOVE_CIRCADIAN_SCENES:
                    await manager.async_remove_circadian_scenes(reason="service")
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Service %s failed for entry %s: %s", action, entry_id, err)

    async def _handle_create_missing(call: ServiceCall) -> None:
        await _for_each_target(call, SERVICE_CREATE_MISSING_SCENES)

    async def _handle_sync(call: ServiceCall) -> None:
        await _for_each_target(call, SERVICE_SYNC_SCENES)

    async def _handle_full_sync(call: ServiceCall) -> None:
        await _for_each_target(call, SERVICE_FULL_SYNC)

    async def _handle_remove_scenes(call: ServiceCall) -> None:
        await _for_each_target(call, SERVICE_REMOVE_CIRCADIAN_SCENES)

    if not hass.services.has_service(DOMAIN, SERVICE_CREATE_MISSING_SCENES):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CREATE_MISSING_SCENES,
            _handle_create_missing,
            schema=service_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SYNC_SCENES):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SYNC_SCENES,
            _handle_sync,
            schema=service_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_FULL_SYNC):
        hass.services.async_register(
            DOMAIN,
            SERVICE_FULL_SYNC,
            _handle_full_sync,
            schema=service_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_REMOVE_CIRCADIAN_SCENES):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REMOVE_CIRCADIAN_SCENES,
            _handle_remove_scenes,
            schema=service_schema,
        )
