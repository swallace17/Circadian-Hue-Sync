"""Constants for Circadian Hue Scene Sync."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "circadian_hue_scene_sync"

CONF_APPLICATION_KEY: Final = "application_key"
CONF_BRIDGE_HOST: Final = "bridge_host"
CONF_CIRCADIAN_SENSOR_ENTITY: Final = "circadian_sensor_entity"
CONF_BRIGHTNESS_ENTITY: Final = "brightness_entity"
CONF_SCENE_NAME: Final = "scene_name"
CONF_SCENES_PER_SECOND: Final = "scenes_per_second"
CONF_AUTO_UPDATE_ON_CIRCADIAN_CHANGE: Final = "auto_update_on_circadian_change"
CONF_AUTO_CREATE_ON_AREA_CHANGE: Final = "auto_create_on_area_change"
CONF_UPDATE_ON_STARTUP: Final = "update_on_startup"
CONF_INCLUDE_ON_ACTION: Final = "include_on_action"
CONF_VERIFY_SSL: Final = "verify_ssl"

DEFAULT_CIRCADIAN_SENSOR_ENTITY: Final = "sensor.circadian_values"
DEFAULT_BRIGHTNESS_ENTITY: Final = DEFAULT_CIRCADIAN_SENSOR_ENTITY
DEFAULT_SCENE_NAME: Final = "circadian"
DEFAULT_SCENES_PER_SECOND: Final = 5
DEFAULT_AUTO_UPDATE_ON_CIRCADIAN_CHANGE: Final = True
DEFAULT_AUTO_CREATE_ON_AREA_CHANGE: Final = True
DEFAULT_UPDATE_ON_STARTUP: Final = True
DEFAULT_INCLUDE_ON_ACTION: Final = True
DEFAULT_VERIFY_SSL: Final = False

PLATFORMS: Final[list[str]] = ["button"]

SERVICE_CREATE_MISSING_SCENES: Final = "create_missing_scenes"
SERVICE_SYNC_SCENES: Final = "sync_scenes"
SERVICE_FULL_SYNC: Final = "full_sync"
SERVICE_REMOVE_CIRCADIAN_SCENES: Final = "remove_circadian_scenes"
ATTR_ENTRY_ID: Final = "entry_id"

EVENT_AREA_REGISTRY_UPDATED: Final = "area_registry_updated"

REQUEST_TIMEOUT_SECONDS: Final = 15
