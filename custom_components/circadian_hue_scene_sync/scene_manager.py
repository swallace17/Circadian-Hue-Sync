"""Scene synchronization runtime."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_APPLICATION_KEY,
    CONF_AUTO_CREATE_ON_AREA_CHANGE,
    CONF_AUTO_UPDATE_ON_CIRCADIAN_CHANGE,
    CONF_BRIGHTNESS_ENTITY,
    CONF_BRIDGE_HOST,
    CONF_CIRCADIAN_SENSOR_ENTITY,
    CONF_INCLUDE_ON_ACTION,
    CONF_SCENE_NAME,
    CONF_SCENES_PER_SECOND,
    CONF_UPDATE_ON_STARTUP,
    CONF_VERIFY_SSL,
    DEFAULT_AUTO_CREATE_ON_AREA_CHANGE,
    DEFAULT_AUTO_UPDATE_ON_CIRCADIAN_CHANGE,
    DEFAULT_BRIGHTNESS_ENTITY,
    DEFAULT_CIRCADIAN_SENSOR_ENTITY,
    DEFAULT_INCLUDE_ON_ACTION,
    DEFAULT_SCENE_NAME,
    DEFAULT_SCENES_PER_SECOND,
    DEFAULT_UPDATE_ON_STARTUP,
    DEFAULT_VERIFY_SSL,
)
from .hue_client import HueApiError, HueV2Client

_LOGGER = logging.getLogger(__name__)


class SceneSyncError(HomeAssistantError):
    """Raised when sync cannot complete."""


@dataclass(slots=True)
class SyncResult:
    """Result details for sync operations."""

    created: int = 0
    synced: int = 0
    skipped: int = 0
    deleted: int = 0


class SceneSyncManager:
    """Manage room-scene creation and updates."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: HueV2Client) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self._lock = asyncio.Lock()

    def _option(self, key: str, default):
        if key in self.entry.options:
            return self.entry.options[key]
        return self.entry.data.get(key, default)

    @property
    def scene_name(self) -> str:
        return self._option(CONF_SCENE_NAME, DEFAULT_SCENE_NAME)

    @property
    def circadian_sensor_entity(self) -> str:
        return self._option(CONF_CIRCADIAN_SENSOR_ENTITY, DEFAULT_CIRCADIAN_SENSOR_ENTITY)

    @property
    def brightness_entity(self) -> str:
        return self._option(CONF_BRIGHTNESS_ENTITY, DEFAULT_BRIGHTNESS_ENTITY)

    @property
    def scenes_per_second(self) -> int:
        value = int(self._option(CONF_SCENES_PER_SECOND, DEFAULT_SCENES_PER_SECOND))
        return max(1, value)

    @property
    def delay_seconds(self) -> float:
        return 1.0 / self.scenes_per_second

    @property
    def include_on_action(self) -> bool:
        return bool(self._option(CONF_INCLUDE_ON_ACTION, DEFAULT_INCLUDE_ON_ACTION))

    @property
    def auto_update_on_circadian_change(self) -> bool:
        return bool(
            self._option(
                CONF_AUTO_UPDATE_ON_CIRCADIAN_CHANGE,
                DEFAULT_AUTO_UPDATE_ON_CIRCADIAN_CHANGE,
            )
        )

    @property
    def auto_create_on_area_change(self) -> bool:
        return bool(
            self._option(
                CONF_AUTO_CREATE_ON_AREA_CHANGE,
                DEFAULT_AUTO_CREATE_ON_AREA_CHANGE,
            )
        )

    @property
    def update_on_startup(self) -> bool:
        return bool(self._option(CONF_UPDATE_ON_STARTUP, DEFAULT_UPDATE_ON_STARTUP))

    async def async_full_sync(self, *, reason: str = "manual") -> SyncResult:
        async with self._lock:
            _LOGGER.debug("Running full scene sync (%s)", reason)
            result = await self._async_create_missing_scenes_locked()
            update_result = await self._async_sync_scenes_locked()
            result.synced = update_result.synced
            result.skipped += update_result.skipped
            return result

    async def async_create_missing_scenes(self, *, reason: str = "manual") -> SyncResult:
        async with self._lock:
            _LOGGER.debug("Creating missing scenes (%s)", reason)
            return await self._async_create_missing_scenes_locked()

    async def async_sync_scenes(self, *, reason: str = "manual") -> SyncResult:
        async with self._lock:
            _LOGGER.debug("Syncing scenes (%s)", reason)
            return await self._async_sync_scenes_locked()

    async def async_remove_circadian_scenes(self, *, reason: str = "manual") -> SyncResult:
        async with self._lock:
            _LOGGER.debug("Removing circadian scenes (%s)", reason)
            return await self._async_remove_circadian_scenes_locked()

    async def _async_create_missing_scenes_locked(self) -> SyncResult:
        result = SyncResult()

        mirek = self._get_current_mirek()
        default_brightness = self._get_fallback_brightness()
        rooms = await self.client.async_get_rooms()
        brightness_by_room = self._get_room_brightness_by_name()
        if default_brightness is None and not brightness_by_room:
            raise SceneSyncError(
                "No usable brightness sources found: configure a fallback brightness entity "
                "or assign Circadian Lighting switches with brightness attributes to areas."
            )
        scenes = await self.client.async_get_scenes()
        devices = await self.client.async_get_devices()

        existing_group_ids = {
            scene.get("group", {}).get("rid")
            for scene in scenes
            if scene.get("metadata", {}).get("name") == self.scene_name
        }

        device_to_lights = _build_device_light_index(devices)

        for room in rooms:
            room_id = room.get("id")
            if room_id in existing_group_ids:
                result.skipped += 1
                continue

            children = room.get("children", [])
            if not children:
                result.skipped += 1
                continue

            light_ids = _extract_room_light_ids(children, device_to_lights)
            if not light_ids:
                result.skipped += 1
                continue

            room_name = _room_name(room)
            brightness = self._resolve_room_brightness(
                room_name=room_name,
                brightness_by_room=brightness_by_room,
                fallback_brightness=default_brightness,
            )
            if brightness is None:
                result.skipped += 1
                continue

            await self.client.async_create_scene(
                scene_name=self.scene_name,
                room_id=room_id,
                light_ids=light_ids,
                brightness=brightness,
                mirek=mirek,
                include_on_action=self.include_on_action,
            )
            result.created += 1
            await asyncio.sleep(self.delay_seconds)

        return result

    async def _async_sync_scenes_locked(self) -> SyncResult:
        result = SyncResult()

        mirek = self._get_current_mirek()
        default_brightness = self._get_fallback_brightness()
        rooms = await self.client.async_get_rooms()
        room_name_by_id = {
            room.get("id"): _room_name(room) for room in rooms if room.get("id")
        }
        brightness_by_room = self._get_room_brightness_by_name()
        if default_brightness is None and not brightness_by_room:
            raise SceneSyncError(
                "No usable brightness sources found: configure a fallback brightness entity "
                "or assign Circadian Lighting switches with brightness attributes to areas."
            )
        scenes = await self.client.async_get_scenes()
        circadian_scenes = [
            scene for scene in scenes if scene.get("metadata", {}).get("name") == self.scene_name
        ]

        for scene in circadian_scenes:
            scene_id = scene.get("id")
            if not scene_id:
                result.skipped += 1
                continue

            light_ids = _extract_scene_light_ids(scene)
            if not light_ids:
                result.skipped += 1
                continue

            room_id = scene.get("group", {}).get("rid")
            room_name = room_name_by_id.get(room_id, "")
            brightness = self._resolve_room_brightness(
                room_name=room_name,
                brightness_by_room=brightness_by_room,
                fallback_brightness=default_brightness,
            )
            if brightness is None:
                result.skipped += 1
                continue

            await self.client.async_update_scene(
                scene_id=scene_id,
                light_ids=light_ids,
                brightness=brightness,
                mirek=mirek,
                include_on_action=self.include_on_action,
            )
            result.synced += 1
            await asyncio.sleep(self.delay_seconds)

        return result

    async def _async_remove_circadian_scenes_locked(self) -> SyncResult:
        result = SyncResult()

        scenes = await self.client.async_get_scenes()
        circadian_scenes = [
            scene for scene in scenes if scene.get("metadata", {}).get("name") == self.scene_name
        ]

        for scene in circadian_scenes:
            scene_id = scene.get("id")
            if not scene_id:
                result.skipped += 1
                continue

            await self.client.async_delete_scene(scene_id=scene_id)
            result.deleted += 1
            await asyncio.sleep(self.delay_seconds)

        return result

    def _get_current_mirek(self) -> int:
        circadian_state = self.hass.states.get(self.circadian_sensor_entity)
        if circadian_state is None:
            raise SceneSyncError(
                f"Circadian sensor entity not found: {self.circadian_sensor_entity}"
            )

        colortemp_kelvin = circadian_state.attributes.get("colortemp")
        if colortemp_kelvin is None:
            raise SceneSyncError(
                f"Circadian sensor missing 'colortemp' attribute: {self.circadian_sensor_entity}"
            )

        return int(round(1_000_000 / float(colortemp_kelvin)))

    def _get_fallback_brightness(self) -> int | None:
        brightness_state = self.hass.states.get(self.brightness_entity)
        if brightness_state is None:
            return None

        brightness = brightness_state.attributes.get("brightness")
        if brightness is None:
            return None

        return int(round(float(brightness)))

    def _get_room_brightness_by_name(self) -> dict[str, int]:
        entity_registry = er.async_get(self.hass)
        area_registry = ar.async_get(self.hass)
        device_registry = dr.async_get(self.hass)

        brightness_by_room: dict[str, int] = {}
        for entity_entry in entity_registry.entities.values():
            if entity_entry.domain != "switch":
                continue

            if not _is_circadian_switch(entity_entry):
                continue

            area_id = entity_entry.area_id
            if not area_id and entity_entry.device_id:
                device = device_registry.async_get(entity_entry.device_id)
                area_id = device.area_id if device else None

            if not area_id:
                continue

            area = area_registry.async_get_area(area_id)
            if not area:
                continue

            state = self.hass.states.get(entity_entry.entity_id)
            if state is None:
                continue

            brightness = state.attributes.get("brightness")
            if brightness is None:
                continue

            area_key = _normalize_name(area.name)
            if area_key in brightness_by_room:
                _LOGGER.debug(
                    "Ignoring additional Circadian switch '%s' for area '%s'; using first discovered brightness source",
                    entity_entry.entity_id,
                    area.name,
                )
                continue

            brightness_by_room[area_key] = int(round(float(brightness)))

        return brightness_by_room

    def _resolve_room_brightness(
        self,
        *,
        room_name: str,
        brightness_by_room: dict[str, int],
        fallback_brightness: int | None,
    ) -> int | None:
        brightness = brightness_by_room.get(_normalize_name(room_name))
        if brightness is not None:
            return brightness

        if fallback_brightness is not None:
            return fallback_brightness

        _LOGGER.warning(
            "Skipping scene for room '%s': no matching Circadian Lighting switch brightness and fallback entity '%s' is unavailable",
            room_name or "<unknown>",
            self.brightness_entity,
        )
        return None


class MultiBridgeSceneSyncManager:
    """Run scene synchronization across all configured Hue bridges."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._lock = asyncio.Lock()

    def _option(self, key: str, default):
        if key in self.entry.options:
            return self.entry.options[key]
        return self.entry.data.get(key, default)

    @property
    def circadian_sensor_entity(self) -> str:
        return self._option(CONF_CIRCADIAN_SENSOR_ENTITY, DEFAULT_CIRCADIAN_SENSOR_ENTITY)

    @property
    def auto_update_on_circadian_change(self) -> bool:
        return bool(
            self._option(
                CONF_AUTO_UPDATE_ON_CIRCADIAN_CHANGE,
                DEFAULT_AUTO_UPDATE_ON_CIRCADIAN_CHANGE,
            )
        )

    @property
    def auto_create_on_area_change(self) -> bool:
        return bool(
            self._option(
                CONF_AUTO_CREATE_ON_AREA_CHANGE,
                DEFAULT_AUTO_CREATE_ON_AREA_CHANGE,
            )
        )

    @property
    def update_on_startup(self) -> bool:
        return bool(self._option(CONF_UPDATE_ON_STARTUP, DEFAULT_UPDATE_ON_STARTUP))

    async def async_validate_connections(self) -> None:
        """Ensure at least one Hue bridge has usable credentials."""
        if not self._build_clients():
            raise SceneSyncError("No Hue integrations with host/api_key were found")

    async def async_full_sync(self, *, reason: str = "manual") -> SyncResult:
        async with self._lock:
            return await self._run_for_all_bridges("full_sync", reason)

    async def async_create_missing_scenes(self, *, reason: str = "manual") -> SyncResult:
        async with self._lock:
            return await self._run_for_all_bridges("create", reason)

    async def async_sync_scenes(self, *, reason: str = "manual") -> SyncResult:
        async with self._lock:
            return await self._run_for_all_bridges("sync", reason)

    async def async_remove_circadian_scenes(self, *, reason: str = "manual") -> SyncResult:
        async with self._lock:
            return await self._run_for_all_bridges("remove", reason)

    async def _run_for_all_bridges(self, mode: str, reason: str) -> SyncResult:
        clients = self._build_clients()
        if not clients:
            raise SceneSyncError("No Hue integrations with host/api_key were found")

        _LOGGER.debug("Running %s across %s Hue bridge(s) (%s)", mode, len(clients), reason)
        combined = SyncResult()
        seen_success = False
        errors: list[str] = []

        for client in clients:
            manager = SceneSyncManager(self.hass, self.entry, client)
            try:
                if mode == "create":
                    result = await manager.async_create_missing_scenes(reason=reason)
                elif mode == "sync":
                    result = await manager.async_sync_scenes(reason=reason)
                elif mode == "remove":
                    result = await manager.async_remove_circadian_scenes(reason=reason)
                else:
                    result = await manager.async_full_sync(reason=reason)

                seen_success = True
                combined.created += result.created
                combined.synced += result.synced
                combined.skipped += result.skipped
                combined.deleted += result.deleted
            except (HueApiError, SceneSyncError) as err:
                errors.append(str(err))
                _LOGGER.warning("Bridge sync failed (%s): %s", reason, err)

        if not seen_success:
            raise SceneSyncError(f"All Hue bridge operations failed: {'; '.join(errors)}")

        return combined

    def _build_clients(self) -> list[HueV2Client]:
        session = async_get_clientsession(self.hass)
        verify_ssl = bool(self._option(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL))
        clients: list[HueV2Client] = []

        for hue_entry in self.hass.config_entries.async_entries("hue"):
            # HA Hue integration stores credentials as host/api_key.
            # Keep fallbacks for older/manual keys to be resilient.
            host = hue_entry.data.get("host") or hue_entry.data.get(CONF_BRIDGE_HOST)
            api_key = hue_entry.data.get("api_key") or hue_entry.data.get(CONF_APPLICATION_KEY)
            if not host or not api_key:
                _LOGGER.debug(
                    "Skipping Hue entry %s (%s): missing host or api key",
                    hue_entry.entry_id,
                    hue_entry.title,
                )
                continue

            clients.append(
                HueV2Client(
                    session=session,
                    bridge_host=str(host),
                    application_key=str(api_key),
                    verify_ssl=verify_ssl,
                )
            )

        return clients


def _build_device_light_index(devices: list[dict]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for device in devices:
        device_id = device.get("id")
        if not device_id:
            continue

        light_ids = [
            service.get("rid")
            for service in device.get("services", [])
            if service.get("rtype") == "light" and service.get("rid")
        ]
        index[device_id] = light_ids
    return index


def _extract_room_light_ids(children: list[dict], device_to_lights: dict[str, list[str]]) -> list[str]:
    lights: list[str] = []
    for child in children:
        if child.get("rtype") != "device":
            continue
        lights.extend(device_to_lights.get(child.get("rid"), []))
    return lights


def _extract_scene_light_ids(scene: dict) -> list[str]:
    light_ids: list[str] = []
    for action in scene.get("actions", []):
        target = action.get("target", {})
        if target.get("rtype") == "light" and target.get("rid"):
            light_ids.append(target["rid"])
    return light_ids


def _normalize_name(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _room_name(room: dict) -> str:
    return str(room.get("metadata", {}).get("name") or "")


def _is_circadian_switch(entity_entry: er.RegistryEntry) -> bool:
    if entity_entry.platform == "circadian_lighting":
        return True

    return entity_entry.entity_id.startswith("switch.circadian_lighting_")
