"""Hue API v2 client."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT_SECONDS


class HueApiError(Exception):
    """Raised when a Hue API request fails."""


class HueAuthError(HueApiError):
    """Raised when Hue API authentication fails."""


@dataclass(slots=True)
class HueV2Client:
    """Minimal async client for Hue CLIP API v2."""

    session: aiohttp.ClientSession
    bridge_host: str
    application_key: str
    verify_ssl: bool

    @property
    def _base_url(self) -> str:
        return f"https://{self.bridge_host}/clip/v2/resource"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "hue-application-key": self.application_key,
            "Content-Type": "application/json",
        }

    async def _request(
        self, method: str, path: str, *, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                response = await self.session.request(
                    method,
                    url,
                    headers=self._headers,
                    json=payload,
                    ssl=self.verify_ssl,
                )
        except TimeoutError as err:
            raise HueApiError(f"Request timed out: {method} {path}") from err
        except aiohttp.ClientError as err:
            raise HueApiError(f"Request failed: {method} {path}: {err}") from err

        if response.status in (401, 403):
            text = await response.text()
            raise HueAuthError(f"Hue authentication failed: {response.status}: {text}")

        if response.status >= 400:
            text = await response.text()
            raise HueApiError(f"Hue request failed: {response.status}: {text}")

        if response.status == 204:
            return {}

        if response.content_length == 0:
            return {}

        try:
            return await response.json(content_type=None)
        except aiohttp.ContentTypeError as err:
            text = await response.text()
            if not text.strip():
                return {}
            raise HueApiError(f"Hue response was not JSON: {text}") from err

    async def async_get_rooms(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/room")
        return response.get("data", [])

    async def async_get_scenes(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/scene")
        return response.get("data", [])

    async def async_get_devices(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/device")
        return response.get("data", [])

    async def async_create_scene(
        self,
        *,
        scene_name: str,
        room_id: str,
        light_ids: list[str],
        brightness: int,
        mirek: int,
        include_on_action: bool,
    ) -> dict[str, Any]:
        actions = [_build_action(light_id, brightness, mirek, include_on_action) for light_id in light_ids]
        payload: dict[str, Any] = {
            "type": "scene",
            "actions": actions,
            "metadata": {"name": scene_name},
            "group": {"rid": room_id, "rtype": "room"},
            "palette": {
                "color": [],
                "dimming": [],
                "color_temperature": [],
                "effects": [],
            },
            "speed": 0.5,
            "auto_dynamic": False,
        }
        return await self._request("POST", "/scene", payload=payload)

    async def async_update_scene(
        self,
        *,
        scene_id: str,
        light_ids: list[str],
        brightness: int,
        mirek: int,
        include_on_action: bool,
    ) -> dict[str, Any]:
        actions = [_build_action(light_id, brightness, mirek, include_on_action) for light_id in light_ids]
        payload = {
            "type": "scene",
            "actions": actions,
        }
        return await self._request("PUT", f"/scene/{scene_id}", payload=payload)

    async def async_delete_scene(self, *, scene_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/scene/{scene_id}")


def _build_action(light_id: str, brightness: int, mirek: int, include_on_action: bool) -> dict[str, Any]:
    hue_brightness = _to_hue_brightness(brightness)
    action: dict[str, Any] = {
        "dimming": {"brightness": hue_brightness},
        "color_temperature": {"mirek": mirek},
    }
    if include_on_action:
        action["on"] = {"on": True}

    return {
        "target": {"rid": light_id, "rtype": "light"},
        "action": action,
    }


def _to_hue_brightness(brightness: int | float) -> float:
    # Treat source brightness as HA scale (0..255) and always convert to Hue v2 scale (0..100).
    value = max(0.0, min(255.0, float(brightness)))
    return (value / 255.0) * 100.0
