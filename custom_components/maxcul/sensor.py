"""Sensor platform for CUL information."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import (
    ATTR_CONNECTION_DEVICE_PATH,
    CONF_DEVICE_PATH,
    DOMAIN,
    SIGNAL_FHT_ID_UPDATE,
    MaxCulConfigEntry,
    MaxCulConnection,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MaxCulConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up CUL information sensors."""
    async_add_entities(
        [MaxFhtIdSensor(config_entry, config_entry.runtime_data)]
    )


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
