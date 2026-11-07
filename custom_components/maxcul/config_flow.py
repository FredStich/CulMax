"""Config flow for the MaxCUL integration."""

from __future__ import annotations

import ipaddress
import logging
import time
from typing import Any

import serial.tools.list_ports
import voluptuous as vol
from homeassistant.components import usb
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import (
    CONF_DEVICES,
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_TYPE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from serial import Serial, SerialException

from . import (
    CONF_DEVICE_PATH,
    CONF_DEVICE_TYPE_OVERRIDES,
    CONF_SENDER_ID,
    DOMAIN,
    normalize_device_path,
)
from ._vendor.pymaxcul._const import (
    CUL_BAUDRATE,
    HEATING_THERMOSTAT,
    HEATING_THERMOSTAT_PLUS,
    PLUG_ADAPTER,
    PUSH_BUTTON,
    SHUTTER_CONTACT,
    WALL_MOUNTED_THERMOSTAT,
)
from ._vendor.pymaxcul._io import (
    is_cul_version_response,
    is_serial_port_in_use,
)
from ._vendor.pymaxcul._telnet import TelnetSerial

LOGGER = logging.getLogger(__name__)

CONF_MANUAL_PATH = "enter-manually"
CONF_DEVICE_TYPE = "device_type"
CONF_OVERRIDE_DEVICE = "override_device"
CONF_OVERRIDE_TYPE = "override_type"
USE_REPORTED_DEVICE_TYPE = "__reported__"

_OVERRIDE_DEVICE_TYPES = (
    HEATING_THERMOSTAT,
    HEATING_THERMOSTAT_PLUS,
    WALL_MOUNTED_THERMOSTAT,
    SHUTTER_CONTACT,
    PUSH_BUTTON,
    PLUG_ADAPTER,
)


class MaxculFlowHandler(ConfigFlow, domain=DOMAIN):
    """Configure a MaxCUL serial or network connection."""

    def __init__(self) -> None:
        self._com_ports: list[Any] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose a serial or network connection."""
        try:
            self._com_ports = await self.hass.async_add_executor_job(
                serial.tools.list_ports.comports
            )
        except (OSError, SerialException) as err:
            LOGGER.warning("Unable to list serial ports: %s", err)
            self._com_ports = []

        if user_input is not None:
            if user_input[CONF_DEVICE_TYPE] == "serial port":
                return await self.async_step_device_picker()
            return await self.async_step_pick_host()

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_TYPE): SelectSelector(
                    SelectSelectorConfig(
                        options=["serial port", "network"],
                        translation_key="connection_type",
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_device_picker(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Select a serial device."""
        port_options = []
        for port in self._com_ports:
            label = f"{port.description or port.device} ({port.device})"
            label += f", s/n: {port.serial_number or 'n/a'}"
            if port.manufacturer:
                label += f" - {port.manufacturer}"
            port_options.append({"value": port.device, "label": label})

        if not port_options:
            return await self.async_step_manual_device_path()

        port_options.append(CONF_MANUAL_PATH)
        errors: dict[str, str] = {}

        if user_input is not None:
            user_selection = user_input[CONF_DEVICE_PATH]
            if user_selection == CONF_MANUAL_PATH:
                return await self.async_step_manual_device_path()

            device_path = await self.hass.async_add_executor_job(
                usb.get_serial_by_id, user_selection
            )
            device_path = device_path or user_selection

            connection_error = await test_connection(self.hass, device_path)
            if connection_error is None:
                selected_port = next(
                    port
                    for port in self._com_ports
                    if port.device == user_selection
                )
                title = selected_port.description or selected_port.device
                title += f" (s/n: {selected_port.serial_number or 'n/a'})"
                if selected_port.manufacturer:
                    title += f" - {selected_port.manufacturer}"
                return await self._async_create_entry(device_path, title)

            errors["base"] = connection_error

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_PATH): SelectSelector(
                    SelectSelectorConfig(
                        options=port_options,
                        translation_key="serial_device",
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="device_picker", data_schema=schema, errors=errors
        )

    async def async_step_manual_device_path(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Enter a serial device path or telnet URL manually."""
        errors: dict[str, str] = {}

        if user_input is not None:
            device_path = user_input[CONF_DEVICE_PATH].strip()
            connection_error = await test_connection(self.hass, device_path)
            if connection_error is None:
                return await self._async_create_entry(device_path, device_path)
            errors["base"] = connection_error

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_PATH): vol.All(
                    cv.string, vol.Length(min=1)
                )
            }
        )
        return self.async_show_form(
            step_id="manual_device_path", data_schema=schema, errors=errors
        )

    async def async_step_pick_host(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Enter a host and port for a network-connected CUL device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            formatted_host = host
            try:
                address = ipaddress.ip_address(host.strip("[]"))
            except ValueError:
                pass
            else:
                if address.version == 6:
                    formatted_host = f"[{address.compressed}]"

            device_path = f"telnet://{formatted_host}:{port}"

            connection_error = await test_connection(self.hass, device_path)
            if connection_error is None:
                return await self._async_create_entry(device_path, f"{host}:{port}")
            errors["base"] = connection_error

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): vol.All(cv.string, vol.Length(min=1)),
                vol.Optional(CONF_PORT, default=2323): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
            }
        )
        return self.async_show_form(
            step_id="pick_host", data_schema=schema, errors=errors
        )

    async def _async_create_entry(self, device_path: str, title: str) -> FlowResult:
        """Create an entry after ensuring its connection path is unique."""
        unique_id = normalize_device_path(device_path)
        if any(
            existing.data.get(CONF_DEVICE_PATH)
            and normalize_device_path(existing.data[CONF_DEVICE_PATH]) == unique_id
            for existing in self.hass.config_entries.async_entries(DOMAIN)
        ):
            return self.async_abort(reason="already_configured")

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=title,
            data={CONF_DEVICE_PATH: device_path, CONF_DEVICES: {}},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the sender-ID options flow."""
        return MaxculOptionsFlowHandler(config_entry)


async def test_connection(hass: HomeAssistant, device_path: str) -> str | None:
    """Return a config-flow error if a CUL cannot be reached."""
    return await hass.async_add_executor_job(_test_connection, device_path)


def _test_connection(device_path: str) -> str | None:
    """Open, probe, and close a CUL device outside the event loop."""
    connection: Serial | TelnetSerial | None = None
    try:
        if device_path.casefold().startswith("telnet://"):
            telnet_path = f"telnet://{device_path[len('telnet://'):]}"
            connection = TelnetSerial(telnet_path, 0.5)
        else:
            connection = Serial(
                device_path,
                baudrate=CUL_BAUDRATE,
                timeout=0.5,
                exclusive=True,
            )

        # Some CUL devices need time to initialize before they answer commands.
        if _get_cul_version(connection):
            return None
        return "cul_no_response"
    except (OSError, SerialException, ValueError) as err:
        LOGGER.debug("Unable to connect to MaxCUL device %s: %s", device_path, err)
        if is_serial_port_in_use(err):
            return "cul_in_use"
        return "cannot_connect_to_device"
    finally:
        if connection is not None:
            try:
                connection.close()
            except (OSError, SerialException) as err:
                LOGGER.debug("Unable to close MaxCUL device %s: %s", device_path, err)


def _get_cul_version(connection: Serial | TelnetSerial) -> bool:
    """Wait for the CUL firmware response to a version request."""
    time.sleep(2)
    for _ in range(10):
        connection.write(b"V\r\n")
        time.sleep(1)
        response = connection.readline()
        if response and is_cul_version_response(
            response.decode("utf-8", errors="replace")
        ):
            return True
    return False


class MaxculOptionsFlowHandler(OptionsFlow):
    """Allow configuring the radio address and paired-device type overrides."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._selected_device_id: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask which setting the user wants to change."""
        known_devices = self._config_entry.data.get(CONF_DEVICES) or {}
        if not known_devices:
            return await self.async_step_radio_settings()

        return self.async_show_menu(
            step_id="init",
            menu_options=["radio_settings", "override_device"],
        )

    async def async_step_radio_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Change the CUL radio address."""
        if user_input is not None:
            options = dict(self._config_entry.options)
            options[CONF_SENDER_ID] = user_input[CONF_SENDER_ID]
            return self.async_create_entry(title="", data=options)

        sender_id = self._config_entry.options.get(CONF_SENDER_ID)
        if sender_id is None:
            sender_id = 0x123456

        schema = vol.Schema(
            {
                vol.Optional(CONF_SENDER_ID, default=sender_id): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=0xFFFFFF)
                )
            }
        )
        return self.async_show_form(
            step_id="radio_settings", data_schema=schema
        )

    async def async_step_override_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Select the paired device whose type needs correcting."""
        known_devices = self._config_entry.data.get(CONF_DEVICES) or {}
        device_options = []
        for device_id, device in known_devices.items():
            if not isinstance(device, dict):
                continue
            name = device.get(CONF_NAME, "MAX! device")
            device_options.append(
                {
                    "value": str(device_id),
                    "label": f"{name} ({device_id})",
                }
            )

        if user_input is not None:
            self._selected_device_id = user_input[CONF_OVERRIDE_DEVICE]
            return await self.async_step_override_type()

        if not device_options:
            return self.async_abort(reason="no_paired_devices")

        schema = vol.Schema(
            {
                vol.Required(CONF_OVERRIDE_DEVICE): SelectSelector(
                    SelectSelectorConfig(
                        options=device_options, mode=SelectSelectorMode.LIST
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="override_device", data_schema=schema
        )

    async def async_step_override_type(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Set or remove the selected device's type override."""
        device_id = self._selected_device_id
        if device_id is None:
            return await self.async_step_override_device()

        if user_input is not None:
            options = dict(self._config_entry.options)
            overrides = options.get(CONF_DEVICE_TYPE_OVERRIDES)
            overrides = dict(overrides) if isinstance(overrides, dict) else {}
            device_type = user_input[CONF_OVERRIDE_TYPE]
            if device_type == USE_REPORTED_DEVICE_TYPE:
                overrides.pop(device_id, None)
            else:
                overrides[device_id] = device_type
            options[CONF_DEVICE_TYPE_OVERRIDES] = overrides
            return self.async_create_entry(title="", data=options)

        known_devices = self._config_entry.data.get(CONF_DEVICES) or {}
        device = known_devices.get(device_id, {})
        reported_type = (
            device.get(CONF_TYPE) if isinstance(device, dict) else None
        )
        type_options = list(_OVERRIDE_DEVICE_TYPES)
        if isinstance(reported_type, str):
            type_options.append(USE_REPORTED_DEVICE_TYPE)

        schema = vol.Schema(
            {
                vol.Required(CONF_OVERRIDE_TYPE): SelectSelector(
                    SelectSelectorConfig(
                        options=type_options,
                        translation_key="device_type_override",
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="override_type", data_schema=schema
        )
