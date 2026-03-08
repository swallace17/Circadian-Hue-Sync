"""Scene synchronization runtime."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

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


@dataclass(slots=True, frozen=True)
class BrightnessValue:
    """Normalized brightness in Hue's 0-100 scale plus source details."""

    percent: float
    attribute: str
    scale: str


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
        _LOGGER.debug(
            "Resolved %d per-room Circadian brightness mapping(s): %s",
            len(brightness_by_room),
            sorted(brightness_by_room.keys()),
        )
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
        _LOGGER.debug(
            "Resolved %d per-room Circadian brightness mapping(s): %s",
            len(brightness_by_room),
            sorted(brightness_by_room.keys()),
        )
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

    def _get_fallback_brightness(self) -> float | None:
        brightness_state = self.hass.states.get(self.brightness_entity)
        if brightness_state is None:
            _LOGGER.debug(
                "Configured fallback brightness entity '%s' not found; auto-detecting Circadian Lighting switch",
                self.brightness_entity,
            )
        else:
            brightness = _extract_brightness_from_state(
                self.brightness_entity,
                brightness_state.attributes,
            )
            if brightness is not None:
                _LOGGER.debug(
                    "Using configured fallback brightness entity '%s' with brightness=%s via %s (%s)",
                    self.brightness_entity,
                    brightness.percent,
                    brightness.attribute,
                    brightness.scale,
                )
                return brightness.percent

            _LOGGER.debug(
                "Configured fallback brightness entity '%s' has no usable brightness attribute; auto-detecting Circadian Lighting switch",
                self.brightness_entity,
            )

        brightness = self._get_global_circadian_switch_brightness()
        if brightness is not None:
            return brightness

        # Final fallback: use circadian sensor brightness when available.
        circadian_state = self.hass.states.get(self.circadian_sensor_entity)
        if circadian_state is not None:
            circadian_brightness = _extract_brightness_from_state(
                self.circadian_sensor_entity,
                circadian_state.attributes,
            )
            if circadian_brightness is not None:
                _LOGGER.debug(
                    "Using circadian sensor '%s' brightness=%s via %s (%s) as fallback source",
                    self.circadian_sensor_entity,
                    circadian_brightness.percent,
                    circadian_brightness.attribute,
                    circadian_brightness.scale,
                )
                return circadian_brightness.percent

        _LOGGER.debug("No global fallback brightness source was found")
        return None

    def _get_room_brightness_by_name(self) -> dict[str, float]:
        area_registry = ar.async_get(self.hass)

        brightness_by_room: dict[str, float] = {}
        for entity_id, state, area_id in self._iter_circadian_switch_states():
            if state is None or not area_id:
                _LOGGER.debug(
                    "Skipping Circadian switch '%s' for room mapping: state=%s area_id=%s",
                    entity_id,
                    "present" if state is not None else "missing",
                    area_id,
                )
                continue

            area = area_registry.async_get_area(area_id)
            if not area:
                _LOGGER.debug(
                    "Skipping Circadian switch '%s': area_id '%s' not found in area registry",
                    entity_id,
                    area_id,
                )
                continue

            brightness = _extract_brightness_from_state(entity_id, state.attributes)
            if brightness is None:
                _LOGGER.debug(
                    "Skipping Circadian switch '%s': no usable brightness attributes (keys=%s)",
                    entity_id,
                    sorted(state.attributes.keys()),
                )
                continue

            area_key = _normalize_name(area.name)
            if area_key in brightness_by_room:
                _LOGGER.debug(
                    "Ignoring additional Circadian switch '%s' for area '%s'; using first discovered brightness source",
                    entity_id,
                    area.name,
                )
                continue

            brightness_by_room[area_key] = brightness.percent
            _LOGGER.debug(
                "Mapped Circadian switch '%s' -> area '%s' (normalized '%s') -> brightness=%s via %s (%s)",
                entity_id,
                area.name,
                area_key,
                brightness.percent,
                brightness.attribute,
                brightness.scale,
            )

        return brightness_by_room

    def _resolve_room_brightness(
        self,
        *,
        room_name: str,
        brightness_by_room: dict[str, float],
        fallback_brightness: float | None,
    ) -> float | None:
        brightness = brightness_by_room.get(_normalize_name(room_name))
        if brightness is not None:
            _LOGGER.debug(
                "Using room-matched brightness=%s for Hue room '%s'",
                brightness,
                room_name or "<unknown>",
            )
            return brightness

        if fallback_brightness is not None:
            _LOGGER.debug(
                "Using fallback brightness=%s for Hue room '%s' (no room match)",
                fallback_brightness,
                room_name or "<unknown>",
            )
            return fallback_brightness

        _LOGGER.warning(
            "Skipping scene for room '%s': no matching Circadian Lighting switch brightness and fallback entity '%s' is unavailable",
            room_name or "<unknown>",
            self.brightness_entity,
        )
        return None

    def _get_global_circadian_switch_brightness(self) -> float | None:
        for entity_id, state, area_id in self._iter_circadian_switch_states():
            if state is None:
                continue

            if area_id:
                continue

            brightness = _extract_brightness_from_state(entity_id, state.attributes)
            if brightness is None:
                _LOGGER.debug(
                    "Ignoring Circadian switch '%s' for fallback: no usable brightness attributes (keys=%s)",
                    entity_id,
                    sorted(state.attributes.keys()),
                )
                continue

            _LOGGER.debug(
                "Using auto-detected global Circadian switch '%s' brightness=%s via %s (%s) as fallback source",
                entity_id,
                brightness.percent,
                brightness.attribute,
                brightness.scale,
            )
            return brightness.percent

        return None

    def _iter_circadian_switch_states(self):
        entity_registry = er.async_get(self.hass)
        device_registry = dr.async_get(self.hass)
        yielded_entity_ids: set[str] = set()
        discovered_registry = 0

        for entity_id in sorted(entity_registry.entities):
            entity_entry = entity_registry.entities[entity_id]
            if entity_entry.domain != "switch":
                continue

            if not _is_circadian_switch(entity_entry):
                continue

            area_id = entity_entry.area_id
            if not area_id and entity_entry.device_id:
                device = device_registry.async_get(entity_entry.device_id)
                area_id = device.area_id if device else None

            yielded_entity_ids.add(entity_entry.entity_id)
            discovered_registry += 1
            _LOGGER.debug(
                "Discovered Circadian switch from registry: entity_id='%s' platform='%s' area_id='%s' device_id='%s'",
                entity_entry.entity_id,
                entity_entry.platform,
                area_id,
                entity_entry.device_id,
            )
            yield entity_entry.entity_id, self.hass.states.get(entity_entry.entity_id), area_id

        # Some environments may have runtime switch states that are not discoverable
        # via entity registry lookups. Include those based on entity_id prefix.
        for entity_id in sorted(self.hass.states.async_entity_ids("switch")):
            if entity_id in yielded_entity_ids:
                continue
            if not _is_circadian_switch_entity_id(entity_id):
                continue

            area_id: str | None = None
            entity_entry = entity_registry.async_get(entity_id)
            if entity_entry:
                area_id = entity_entry.area_id
                if not area_id and entity_entry.device_id:
                    device = device_registry.async_get(entity_entry.device_id)
                    area_id = device.area_id if device else None

            _LOGGER.debug(
                "Discovered Circadian switch from runtime state: entity_id='%s' area_id='%s' registry_entry=%s",
                entity_id,
                area_id,
                entity_entry is not None,
            )
            yield entity_id, self.hass.states.get(entity_id), area_id

        _LOGGER.debug(
            "Circadian switch discovery complete: registry_matches=%d runtime_total_switches=%d",
            discovered_registry,
            len(self.hass.states.async_entity_ids("switch")),
        )


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


def _room_name(room: dict[str, Any]) -> str:
    return str(room.get("metadata", {}).get("name") or "")


def _is_circadian_switch(entity_entry: er.RegistryEntry) -> bool:
    if entity_entry.platform == "circadian_lighting":
        return True

    return _is_circadian_switch_entity_id(entity_entry.entity_id)


def _is_circadian_switch_entity_id(entity_id: str) -> bool:
    return entity_id.startswith("switch.circadian_lighting")


def _extract_brightness_from_state(
    entity_id: str,
    attributes: dict[str, Any],
) -> BrightnessValue | None:
    percent_keys = ("brightness_pct", "brightness_percent", "percent")
    for key in percent_keys:
        percent = _coerce_percent(attributes.get(key))
        if percent is not None:
            return BrightnessValue(percent=percent, attribute=key, scale="percent")

    direct_keys = ("brightness", "bri")
    for key in direct_keys:
        raw_value = _coerce_float(attributes.get(key))
        if raw_value is None:
            continue

        scale = _infer_direct_brightness_scale(entity_id, attributes, key, raw_value)
        percent = _normalize_brightness_to_percent(raw_value, scale)
        return BrightnessValue(percent=percent, attribute=key, scale=scale)

    return None


def _infer_direct_brightness_scale(
    entity_id: str,
    attributes: dict[str, Any],
    attribute: str,
    raw_value: float,
) -> str:
    unit = str(attributes.get("unit_of_measurement") or "").strip().lower()
    if unit in {"%", "percent"}:
        return "percent"

    max_value = _coerce_float(attributes.get("max"))
    if max_value is not None:
        if max_value <= 100.0:
            return "percent"
        if max_value <= 255.0:
            return "ha255"

    if attribute == "bri":
        return "ha255"

    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    if domain == "light":
        return "ha255"

    if _is_circadian_switch_entity_id(entity_id) or domain == "sensor":
        return "percent" if raw_value <= 100.0 else "ha255"

    return "ha255" if raw_value > 100.0 else "percent"


def _normalize_brightness_to_percent(value: float, scale: str) -> float:
    if scale == "ha255":
        clamped = min(255.0, max(0.0, value))
        return (clamped / 255.0) * 100.0

    return min(100.0, max(0.0, value))


def _coerce_percent(value: Any) -> float | None:
    numeric = _coerce_float(value)
    if numeric is None:
        return None
    return min(100.0, max(0.0, numeric))


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
