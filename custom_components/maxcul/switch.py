"""Switch platform for MAX! Plug Adapter devices."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import CONF_DEVICES, CONF_NAME, CONF_TYPE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ._vendor.pymaxcul._const import (
    ATTR_DEVICE_ID,
    ATTR_DEVICE_SERIAL,
    ATTR_DEVICE_TYPE,
    ATTR_VALVE_POSITION,
    PLUG_ADAPTER,
)
from . import (
    ATTR_CONNECTION_DEVICE_PATH,
    CONF_DEVICE_PATH,
    DOMAIN,
    SIGNAL_DEVICE_PAIRED,
    SIGNAL_DEVICE_REPAIRED,
    SIGNAL_THERMOSTAT_UPDATE,
    MaxCulConfigEntry,
    MaxCulConnection,
    register_paired_device,
)


def _is_plug_adapter(device_type: object) -> bool:
    """Return whether a paired device is a MAX! Plug Adapter."""
    return (
        isinstance(device_type, str)
        and device_type.casefold() == PLUG_ADAPTER.casefold()
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MaxCulConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Restore known Plug Adapters and add newly paired ones."""
    device_path = config_entry.data[CONF_DEVICE_PATH]
    connection = config_entry.runtime_data
    known_devices = config_entry.data.get(CONF_DEVICES) or {}
    added_device_ids = set(known_devices)

    async_add_entities(
        [
            MaxPlugAdapter(connection, device_id, device[CONF_NAME])
            for device_id, device in known_devices.items()
            if _is_plug_adapter(device.get(CONF_TYPE))
        ]
    )

    @callback
    def paired_callback(payload: dict[str, Any]) -> None:
        if payload.get(ATTR_CONNECTION_DEVICE_PATH) != device_path:
            return
        device_type = payload.get(ATTR_DEVICE_TYPE)
        device_id = payload.get(ATTR_DEVICE_ID)
        if not _is_plug_adapter(device_type) or device_id is None:
            return

        device_id = str(device_id)
        if device_id in added_device_ids:
            return
        added_device_ids.add(device_id)

        name = payload.get(ATTR_DEVICE_SERIAL)
        if not isinstance(name, str) or not name:
            name = "Plug Adapter"
        register_paired_device(hass, config_entry, device_id, name, device_type)
        async_add_entities([MaxPlugAdapter(connection, device_id, name)])

    config_entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DEVICE_PAIRED, paired_callback)
    )
    config_entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DEVICE_REPAIRED, paired_callback)
    )


class MaxPlugAdapter(SwitchEntity):
    """Control and report a MAX! Plug Adapter relay."""

    _attr_device_class = SwitchDeviceClass.OUTLET
    _attr_has_entity_name = True
    _attr_icon = "mdi:power-socket-eu"
    _attr_translation_key = "plug_adapter"

    def __init__(
        self, connection: MaxCulConnection, device_id: str, name: str
    ) -> None:
        self._connection = connection
        self._device_id = device_id
        self._name = name
        self._is_on: bool | None = None
        self._connection.add_paired_device(self.sender_id)

    @property
    def device_info(self) -> DeviceInfo:
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._name,
            "manufacturer": "eQ-3 / MAX!",
            "model": "Plug Adapter",
            "configuration_url": "https://github.com/powerpaul17/maxcul",
        }

    @property
    def unique_id(self) -> str:
        return f"{self._device_id}-plug-adapter"

    @property
    def sender_id(self) -> int:
        """Return the device's MAX! radio address."""
        return int(self._device_id)

    async def async_added_to_hass(self) -> None:
        """Listen for thermostat-state reports from this adapter."""

        @callback
        def update(payload: dict[str, Any]) -> None:
            if payload.get(ATTR_DEVICE_ID) != self.sender_id:
                return

            valve_position = payload.get(ATTR_VALVE_POSITION)
            if isinstance(valve_position, (int, float)):
                # FHEM reports the Plug Adapter output as off at position 100,
                # and on at other reported positions.
                self._is_on = valve_position != 100
                self.async_schedule_update_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_THERMOSTAT_UPDATE, update)
        )

    @property
    def is_on(self) -> bool | None:
        return self._is_on

    @property
    def should_poll(self) -> bool:
        return False

    def turn_on(self, **kwargs: Any) -> None:
        """Turn the Plug Adapter relay on."""
        self._connection.set_plug_adapter_state(self.sender_id, True)

    def turn_off(self, **kwargs: Any) -> None:
        """Turn the Plug Adapter relay off."""
        self._connection.set_plug_adapter_state(self.sender_id, False)
