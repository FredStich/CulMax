from __future__ import annotations

import pytest

from homeassistant.const import CONF_DEVICES, CONF_NAME, CONF_TYPE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maxcul import (
    CONF_DEVICE_PATH,
    CONF_DEVICE_TYPE_OVERRIDES,
    DOMAIN,
    MaxCulConnection,
)
from custom_components.maxcul._vendor.pymaxcul import (
    EVENT_DEVICE_PAIRED,
    EVENT_THERMOSTAT_UPDATE,
)
from custom_components.maxcul._vendor.pymaxcul._const import (
    ATTR_DEVICE_ID,
    ATTR_DEVICE_SERIAL,
    ATTR_DEVICE_TYPE,
    ATTR_VALVE_POSITION,
    HEATING_THERMOSTAT,
    MODE_MANUAL,
    PLUG_ADAPTER,
)
from custom_components.maxcul._vendor.pymaxcul._messages import (
    PairPingMessage,
    SetTemperatureMessage,
)


async def test_pairing_creates_and_controls_plug_adapter(
    hass: HomeAssistant, max_connection_factory
) -> None:
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="plug-adapter",
        data={CONF_DEVICE_PATH: "/dev/tty0", CONF_DEVICES: {}},
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    connection = max_connection_factory.connections["/dev/tty0"]
    connection.call_callback(
        EVENT_DEVICE_PAIRED,
        {
            ATTR_DEVICE_ID: 12345,
            ATTR_DEVICE_TYPE: PLUG_ADAPTER,
            ATTR_DEVICE_SERIAL: "Kitchen Plug",
        },
    )
    await hass.async_block_till_done()

    switch_entry = next(
        entry
        for entry in hass.data["entity_registry"].entities.values()
        if entry.platform == DOMAIN and entry.unique_id == "12345-plug-adapter"
    )
    switch_id = switch_entry.entity_id
    assert hass.states.get(switch_id).state == "unknown"
    assert config_entry.data[CONF_DEVICES]["12345"] == {
        CONF_NAME: "Kitchen Plug",
        CONF_TYPE: PLUG_ADAPTER,
    }

    device = dr.async_get(hass).async_get_device({(DOMAIN, "12345")})
    assert device is not None
    assert device.model == "Plug Adapter"

    calls: list[tuple[int, bool]] = []
    connection.set_plug_adapter_state = lambda device_id, is_on: calls.append(
        (device_id, is_on)
    )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch_id}, blocking=True
    )
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch_id}, blocking=True
    )
    assert calls == [(12345, True), (12345, False)]

    connection.call_callback(
        EVENT_THERMOSTAT_UPDATE,
        {ATTR_DEVICE_ID: 12345, ATTR_VALVE_POSITION: 0},
    )
    await hass.async_block_till_done()
    assert hass.states.get(switch_id).state == "on"

    connection.call_callback(
        EVENT_THERMOSTAT_UPDATE,
        {ATTR_DEVICE_ID: 12345, ATTR_VALVE_POSITION: 100},
    )
    await hass.async_block_till_done()
    assert hass.states.get(switch_id).state == "off"


@pytest.mark.parametrize(
    ("is_on", "temperature"),
    [(True, 30.5), (False, 4.5)],
)
def test_plug_adapter_control_uses_temperature_sentinels(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    is_on: bool,
    temperature: float,
) -> None:
    connection = MaxCulConnection(hass, "/dev/tty0")
    calls = []
    monkeypatch.setattr(
        connection._connection,
        "set_temperature",
        lambda device_id, target_temperature, mode: calls.append(
            (device_id, target_temperature, mode)
        )
        or True,
    )

    connection.set_plug_adapter_state(12345, is_on)

    assert calls == [(12345, temperature, MODE_MANUAL)]


def test_plug_adapter_control_reports_queue_failure(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = MaxCulConnection(hass, "/dev/tty0")
    monkeypatch.setattr(
        connection._connection, "set_temperature", lambda *_args: False
    )

    with pytest.raises(HomeAssistantError, match="Could not queue state change"):
        connection.set_plug_adapter_state(12345, True)


def test_pair_ping_decodes_plug_adapter_type() -> None:
    assert PairPingMessage.decode_payload("100800") == {
        "firmware_version": "V1.0",
        "device_type": PLUG_ADAPTER,
        "selftest_result": 0,
        "device_serial": "",
    }


async def test_device_type_override_routes_reported_thermostat_to_switch(
    hass: HomeAssistant,
) -> None:
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="misreported-plug",
        data={
            CONF_DEVICE_PATH: "/dev/tty0",
            CONF_DEVICES: {
                "12345": {
                    CONF_NAME: "KEQ9010666",
                    CONF_TYPE: HEATING_THERMOSTAT,
                }
            },
        },
        options={
            CONF_DEVICE_TYPE_OVERRIDES: {"12345": PLUG_ADAPTER},
        },
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    switch_entities = [
        entity_id
        for entity_id in hass.states.async_entity_ids("switch")
        if entity_id.startswith("switch.")
    ]
    assert len(switch_entities) == 1
    assert hass.states.get(switch_entities[0]).state == "unknown"
    assert not hass.states.async_entity_ids("climate")


@pytest.mark.parametrize(
    ("temperature", "payload"),
    [(30.5, "7D"), (4.5, "49")],
)
def test_plug_adapter_command_wire_payload(
    temperature: float, payload: str
) -> None:
    message = SetTemperatureMessage(
        counter=1,
        sender_id=0x123456,
        receiver_id=12345,
        desired_temperature=temperature,
        mode=MODE_MANUAL,
    )

    assert message.encode_payload() == payload
