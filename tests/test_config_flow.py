from __future__ import annotations

import pytest

from homeassistant.const import CONF_DEVICES, CONF_NAME, CONF_TYPE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maxcul import (
    CONF_DEVICE_PATH,
    CONF_DEVICE_TYPE_OVERRIDES,
    CONF_SENDER_ID,
    DOMAIN,
)
from custom_components.maxcul import config_flow
from custom_components.maxcul._vendor.pymaxcul._const import (
    CUL_BAUDRATE,
    HEATING_THERMOSTAT,
    PLUG_ADAPTER,
)
from custom_components.maxcul._vendor.pymaxcul._io import (
    is_cul_version_response,
)


async def test_options_flow_saves_and_clears_device_type_override(
    hass: HomeAssistant,
) -> None:
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_DEVICE_PATH: "/dev/tty0",
            CONF_DEVICES: {
                "12345": {
                    CONF_NAME: "KEQ9010666",
                    CONF_TYPE: HEATING_THERMOSTAT,
                }
            },
        },
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] == "form"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SENDER_ID: 0x123456,
            config_flow.CONF_OVERRIDE_DEVICE: "12345",
            config_flow.CONF_OVERRIDE_TYPE: PLUG_ADAPTER,
        },
    )
    assert result["type"] == "create_entry"
    assert config_entry.options[CONF_DEVICE_TYPE_OVERRIDES] == {
        "12345": PLUG_ADAPTER
    }

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SENDER_ID: 0x123456,
            config_flow.CONF_OVERRIDE_DEVICE: "12345",
            config_flow.CONF_OVERRIDE_TYPE: config_flow.USE_REPORTED_DEVICE_TYPE,
        },
    )
    assert result["type"] == "create_entry"
    assert config_entry.options[CONF_DEVICE_TYPE_OVERRIDES] == {}


def test_serial_connection_probe_uses_cul_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    connections = []

    class FakeSerial:
        def __init__(self, *args, **kwargs) -> None:
            calls.append((args, kwargs))
            self.writes = []
            self.closed = False
            connections.append(self)

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        def readline(self) -> bytes:
            return b"V 1.0\r\n"

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(config_flow, "Serial", FakeSerial)
    monkeypatch.setattr(config_flow.time, "sleep", lambda _seconds: None)

    assert config_flow._test_connection("/dev/ttyACM0") is None

    assert calls == [
        (
            ("/dev/ttyACM0",),
            {
                "baudrate": CUL_BAUDRATE,
                "timeout": 0.5,
                "exclusive": True,
            },
        )
    ]
    assert connections[0].writes == [b"V\r\n"]
    assert connections[0].closed


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("V 1.54 CUL868", True),
        ("V 1.0", True),
        ("ready", False),
        ("", False),
    ],
)
def test_cul_version_response_validation(response: str, expected: bool) -> None:
    assert is_cul_version_response(response) is expected


def test_serial_connection_probe_reports_port_in_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def serial_in_use(*_args, **_kwargs):
        raise config_flow.SerialException(
            "Could not exclusively lock port /dev/ttyACM0: "
            "[Errno 11] Resource temporarily unavailable"
        )

    monkeypatch.setattr(config_flow, "Serial", serial_in_use)

    assert config_flow._test_connection("/dev/ttyACM0") == "cul_in_use"


def test_serial_connection_probe_rejects_missing_firmware_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SilentSerial:
        def __init__(self, *_args, **_kwargs) -> None:
            self.writes = []

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        def readline(self) -> bytes:
            return b""

        def close(self) -> None:
            pass

    connection = SilentSerial()
    monkeypatch.setattr(config_flow, "Serial", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(config_flow.time, "sleep", lambda _seconds: None)

    assert config_flow._test_connection("/dev/ttyACM0") == "cul_no_response"
    assert connection.writes == [b"V\r\n"] * 10
