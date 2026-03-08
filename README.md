# Circadian Hue Scene Sync

Home Assistant custom integration that precomputes and keeps Hue scenes in sync with current circadian lighting values (From the Circadian Lighting custom integration in HACS).

## What it does

- Auto-discovers all configured Home Assistant Hue bridges and uses their existing credentials.
- Creates missing Hue room scenes (default name: `circadian`).
- Updates existing scenes when circadian values change.
- Auto-detects Circadian Lighting switch entities assigned to Home Assistant Areas and applies each switch brightness to Hue rooms with matching names.
- Exposes manual services and button entities for testing/recovery.

## Requirements

- Home Assistant Hue integration must already be installed and working.
- Circadian sensor entity should exist (default): `sensor.circadian_values`
- Circadian Lighting switch entities should expose usable brightness information.

## Installation

### Option A: HACS (recommended once published)

1. HACS -> Integrations -> menu -> Custom repositories.
2. Add this repository URL (https://github.com/swallace17/Circadian-Hue-Sync/) with category `Integration`.
3. Install `Circadian Hue Scene Sync`.
4. Restart Home Assistant.
5. Add integration: `Settings -> Devices & Services -> Add Integration -> Circadian Hue Scene Sync`.

### Option B: Manual

1. Copy `custom_components/circadian_hue_scene_sync` into your Home Assistant `/config/custom_components` directory.
2. Restart Home Assistant.
3. Add integration: `Settings -> Devices & Services -> Add Integration -> Circadian Hue Scene Sync`.
4. Confirm setup (no bridge host/key required).

## Manual actions

Buttons:
- `Full Sync`
- `Remove Circadian Scenes`
- `Rebuild Circadian Scenes`

Services:
- `circadian_hue_scene_sync.create_missing_scenes`
- `circadian_hue_scene_sync.sync_scenes`
- `circadian_hue_scene_sync.full_sync`
- `circadian_hue_scene_sync.remove_circadian_scenes`

Each service optionally supports `entry_id` to target a specific integration entry.

## Recommended operation

- Keep automatic triggers enabled for day-to-day operation.
- Use buttons/services only for manual maintenance and testing.

## Notes

- This integration uses Hue CLIP API v2 endpoints.
- SSL verification defaults to disabled, matching many local Hue bridge environments.
- Per-room brightness comes from area-assigned Circadian Lighting switches matched to Hue room names.
- If no per-room switch matches a Hue room, the integration falls back to a valid global brightness source such as the configured `brightness_entity`, a global Circadian switch, or the circadian sensor.
