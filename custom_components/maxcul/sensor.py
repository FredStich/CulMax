"""Sensor platform for CUL information."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import (
    ATTR_CONNECTION_DEVICE_PATH,
    CONF_DEVICE_PATH,
    DOMAIN,
    SIGNAL_CUL_CONNECTION_STATUS,
    SIGNAL_FHT_ID_UPDATE,
    MaxCulConfigEntry,
    MaxCulConnection,
)

LOGGER = logging.getLogger(__name__)

BUSWARE_CUL_V3_MANIFEST_URL = "https://install.busware.de/cul/manifest.json"
BUSWARE_CUL_UPDATER_URL = "https://install.busware.de/cul/"
FIRMWARE_CHECK_INTERVAL = timedelta(days=1)
FIRMWARE_CHECK_TIMEOUT = 10

_A_CULFW_VERSION_PATTERN = re.compile(
    r"^V\s+(\d+(?:\.\d+)*)\s+a-culfw(?:\s+.*)?$", re.IGNORECASE
)
_RELEASE_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+)*$")


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MaxCulConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up diagnostic information and firmware update sensors."""
    async_add_entities(
        [
            MaxFhtIdSensor(config_entry, config_entry.runtime_data),
            CulFirmwareUpdateSensor(
                hass, config_entry, config_entry.runtime_data
            ),
        ]
    )


class CulFirmwareCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch the latest CUL V3 firmware version from Busware."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            LOGGER,
            name="CulMax CUL V3 firmware",
            update_interval=FIRMWARE_CHECK_INTERVAL,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch and validate the published CUL V3 firmware manifest."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                BUSWARE_CUL_V3_MANIFEST_URL,
                timeout=aiohttp.ClientTimeout(total=FIRMWARE_CHECK_TIMEOUT),
            ) as response:
                response.raise_for_status()
                manifest = await response.json()
            latest_version = _manifest_cul_v3_version(manifest)
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ValueError,
        ) as err:
            LOGGER.warning("Unable to check for CUL V3 firmware updates: %s", err)
            return {"error": str(err), "latest_version": None}

        return {"error": None, "latest_version": latest_version}


def _manifest_cul_v3_version(manifest: Any) -> str:
    """Extract the version only when the manifest offers a CUL V3 app image."""
    if not isinstance(manifest, dict):
        raise ValueError("Firmware manifest must be a JSON object")
    cul_v3 = manifest.get("CUL_V3")
    if not isinstance(cul_v3, dict):
        raise ValueError("Firmware manifest has no CUL_V3 entry")

    version = cul_v3.get("version")
    if not isinstance(version, str) or not _RELEASE_VERSION_PATTERN.fullmatch(
        version
    ):
        raise ValueError("Firmware manifest has an invalid CUL_V3 version")

    artifacts = cul_v3.get("artifacts")
    if not isinstance(artifacts, list) or not any(
        isinstance(artifact, dict)
        and artifact.get("file") == "CUL_V3.hex"
        and artifact.get("type") == "app"
        for artifact in artifacts
    ):
        raise ValueError("Firmware manifest has no CUL_V3 application image")

    return version


def _version_parts(version: str) -> tuple[int, ...] | None:
    """Parse numeric dotted firmware versions for safe comparison."""
    if not _RELEASE_VERSION_PATTERN.fullmatch(version):
        return None
    return tuple(int(part) for part in version.split("."))


def _current_cul_firmware_version(firmware_response: str | None) -> str | None:
    """Extract the a-culfw version from the CUL response."""
    if firmware_response is None:
        return None
    match = _A_CULFW_VERSION_PATTERN.fullmatch(firmware_response)
    return match.group(1) if match else None


def _is_supported_cul_firmware(
    hardware_revision: str | None, current_firmware: str | None
) -> bool:
    """Require a positive CUL V3 and a-culfw identification before checking."""
    return (
        hardware_revision == "CUL_V3"
        and current_firmware is not None
        and _A_CULFW_VERSION_PATTERN.fullmatch(current_firmware) is not None
    )


def _firmware_update_state(
    hardware_revision: str | None,
    current_firmware: str | None,
    release_data: dict[str, Any] | None,
) -> str:
    """Return a status only when hardware and firmware are positively identified."""
    current_match = (
        _A_CULFW_VERSION_PATTERN.fullmatch(current_firmware)
        if current_firmware
        else None
    )
    if hardware_revision is None:
        return "hardware_unidentified"
    if hardware_revision != "CUL_V3":
        return "unsupported"
    if current_match is None:
        return "firmware_unrecognized"
    if release_data is None:
        return "checking"
    if release_data.get("error"):
        return "check_failed"

    current_version = _version_parts(current_match.group(1))
    latest = release_data.get("latest_version")
    latest_version = _version_parts(latest) if isinstance(latest, str) else None
    if current_version is None or latest_version is None:
        return "check_failed"

    normalized_current = _trim_version_zeros(current_version)
    normalized_latest = _trim_version_zeros(latest_version)
    return (
        "update_available"
        if normalized_current < normalized_latest
        else "up_to_date"
    )


def _trim_version_zeros(version: tuple[int, ...]) -> tuple[int, ...]:
    """Make versions such as 1.30.0 and 1.30 compare equally."""
    while len(version) > 1 and version[-1] == 0:
        version = version[:-1]
    return version


class MaxFhtIdSensor(SensorEntity):
    """Report the FHT ID configured on a CUL."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_translation_key = "fht_id"

    def __init__(
        self, config_entry: MaxCulConfigEntry, connection: MaxCulConnection
    ) -> None:
        self._device_path = config_entry.data[CONF_DEVICE_PATH]
        self._connection = connection
        self._fht_id = connection.fht_id
        self._attr_unique_id = f"{config_entry.entry_id}-fht-id"

    @property
    def native_value(self) -> str | None:
        """Return the configured four-digit FHT ID."""
        return self._fht_id

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the default HomeMatic ID derived from the FHT ID."""
        return {
            "homematic_id": (
                f"F1{self._fht_id}"
                if self._fht_id is not None and self._fht_id != "0000"
                else None
            )
        }

    @property
    def device_info(self) -> DeviceInfo:
        """Associate this diagnostic entity with its CUL connection."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"cul-{self._device_path}")},
            name="MAX! CUL",
            manufacturer="CUL",
        )

    async def async_added_to_hass(self) -> None:
        """Listen for FHT ID responses from this CUL."""

        @callback
        def update(payload: dict[str, Any]) -> None:
            if payload.get(ATTR_CONNECTION_DEVICE_PATH) != self._device_path:
                return
            fht_id = payload.get("fht_id")
            if not isinstance(fht_id, str):
                return
            self._fht_id = fht_id
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_FHT_ID_UPDATE, update)
        )


class CulFirmwareUpdateSensor(SensorEntity):
    """Check for Busware CUL V3 firmware updates without flashing the CUL."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "cul_firmware_update"

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: MaxCulConfigEntry,
        connection: MaxCulConnection,
    ) -> None:
        self._device_path = config_entry.data[CONF_DEVICE_PATH]
        self._connection = connection
        self._hardware_revision = connection.hardware_revision
        self._firmware_version = connection.cul_version
        self._coordinator = CulFirmwareCoordinator(hass)
        self._firmware_check_started = False
        self._attr_unique_id = f"{config_entry.entry_id}-cul-firmware-update"

    @property
    def native_value(self) -> str | None:
        """Return the installed CUL firmware version."""
        return _current_cul_firmware_version(self._firmware_version)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose versions and the external updater for supported users."""
        update_status = _firmware_update_state(
            self._hardware_revision,
            self._firmware_version,
            self._coordinator.data,
        )
        release_data = self._coordinator.data or {}
        return {
            "hardware_revision": self._hardware_revision,
            "firmware_response": self._firmware_version,
            "current_version": self.native_value,
            "latest_version": release_data.get("latest_version"),
            "update_status": update_status,
            "update_available": update_status == "update_available",
            "update_url": BUSWARE_CUL_UPDATER_URL,
            "check_error": release_data.get("error"),
        }

    @property
    def device_info(self) -> DeviceInfo:
        """Associate this diagnostic entity with its CUL connection."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"cul-{self._device_path}")},
            name="MAX! CUL",
            manufacturer="CUL",
        )

    async def async_added_to_hass(self) -> None:
        """Track reconnections and check releases for identified CUL V3s."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CUL_CONNECTION_STATUS,
                self._handle_connection_status,
            )
        )
        self._start_firmware_check()

    @callback
    def _handle_connection_status(self, payload: dict[str, Any]) -> None:
        if (
            payload.get(ATTR_CONNECTION_DEVICE_PATH) != self._device_path
            or not payload.get("connected")
        ):
            return
        self._hardware_revision = self._connection.hardware_revision
        self._firmware_version = self._connection.cul_version
        self._start_firmware_check()
        self.async_write_ha_state()

    @callback
    def _start_firmware_check(self) -> None:
        if (
            not _is_supported_cul_firmware(
                self._hardware_revision, self._firmware_version
            )
            or self._firmware_check_started
        ):
            return
        self._firmware_check_started = True
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.hass.async_create_task(self._coordinator.async_refresh())

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
