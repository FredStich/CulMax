import pytest

from homeassistant.const import CONF_DEVICES, CONF_NAME, CONF_TYPE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from pytest_homeassistant_custom_component.common import (
  MockConfigEntry
)

from custom_components.maxcul import (
    CONF_DEVICE_PATH,
    DOMAIN,
    MaxCulConnection,
    SIGNAL_CUL_CONNECTION_STATUS,
    SERVICE_ENABLE_PAIRING,
    SIGNAL_THERMOSTAT_UPDATE,
    async_setup_entry,
    normalize_device_path,
)

from custom_components.maxcul._vendor.pymaxcul import (
    EVENT_DEVICE_PAIRED,
    EVENT_DEVICE_REPAIRED,
    EVENT_FHT_ID_UPDATE,
)


from custom_components.maxcul._vendor.pymaxcul._const import (
    ATTR_DEVICE_ID,
    ATTR_DESIRED_TEMPERATURE,
    ATTR_DEVICE_TYPE,
    ATTR_DEVICE_SERIAL,
    ATTR_FIRMWARE_VERSION,
    ATTR_MEASURED_TEMPERATURE,
    EVENT_CONNECTION_STATUS,
    HEATING_THERMOSTAT
)
from custom_components.maxcul._vendor.pymaxcul._messages import (
    WallThermostatControlMessage,
)

from .conftest import MockConnection, MockConnectionFactory, max_connection_factory


def test_normalize_device_path():
    assert normalize_device_path("TELNET://MAXCUL.local:2323") == "maxcul.local:2323"
    assert normalize_device_path("maxcul.local:2323") == "maxcul.local:2323"
    assert normalize_device_path("/dev/ttyUSB0") == "/dev/ttyUSB0"


async def test_setup_fails_when_cul_does_not_report_firmware(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Surface an unavailable CUL as a config-entry setup error."""
    monkeypatch.setattr(
        MockConnection,
        "wait_for_initial_connection",
        lambda _self, _timeout: False,
    )
    monkeypatch.setattr(
        MockConnection,
        "connection_error",
        property(lambda _self: "CUL did not return a valid firmware version"),
    )
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_DEVICE_PATH: "/dev/ttyACM0", CONF_DEVICES: {}},
    )

    with pytest.raises(ConfigEntryNotReady, match="valid firmware version"):
        await async_setup_entry(hass, config_entry)


async def test_wall_thermostat_control_message_dispatches_temperature(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Translate wall thermostat fields to climate attributes."""
    class FakeMaxConnection:
        def __init__(self, **kwargs):
            self._handle_message = lambda message, signal_strength: None
            self._resend_message = lambda: None

    monkeypatch.setattr(
        "custom_components.maxcul.MaxConnection", FakeMaxConnection
    )
    updates = []
    async_dispatcher_connect(hass, SIGNAL_THERMOSTAT_UPDATE, updates.append)
    connection = MaxCulConnection(hass, "/dev/tty0")
    message = WallThermostatControlMessage(
        sender_id=0x6A285,
        desired_temperature=30.0,
        temperature=22.6,
    )

    connection._connection._handle_message(message, 254)
    await hass.async_block_till_done()

    assert updates == [
        {
            ATTR_DEVICE_ID: 0x6A285,
            ATTR_DESIRED_TEMPERATURE: 30.0,
            ATTR_MEASURED_TEMPERATURE: 22.6,
        }
    ]


async def test_connection_status_dispatches_device_path(
    hass: HomeAssistant,
) -> None:
    updates = []
    async_dispatcher_connect(
        hass, SIGNAL_CUL_CONNECTION_STATUS, updates.append
    )
    connection = MaxCulConnection(hass, "/dev/ttyACM0")

    connection._callback(
        EVENT_CONNECTION_STATUS,
        {"connected": True, "error": None},
    )
    await hass.async_block_till_done()

    assert updates == [
        {
            "connected": True,
            "error": None,
            "connection_device_path": "/dev/ttyACM0",
        }
    ]


async def test_setup_entry(hass):
    ''' Test setting up the component from a config entry '''

    config = {
        CONF_DEVICE_PATH: '/dev/tty0',
        CONF_DEVICES: {
            '12345': {
                CONF_NAME: 'Thermostat1',
                CONF_TYPE: HEATING_THERMOSTAT
            }
        }
    }
    config_entry = MockConfigEntry(domain=DOMAIN, data=config, entry_id='test')
    assert await async_setup_entry(hass, config_entry)

    await hass.async_block_till_done()

    assert hass.data.get('entity_registry').entities.get('climate.thermostat1')


async def test_setup_removes_registry_devices_missing_from_entry(
    hass: HomeAssistant,
) -> None:
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_DEVICE_PATH: "/dev/tty0", CONF_DEVICES: {}},
        entry_id="test-stale-device",
    )
    config_entry.add_to_hass(hass)
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "12345")},
        name="Old device",
    )

    assert await async_setup_entry(hass, config_entry)

    assert device_registry.async_get_device({(DOMAIN, "12345")}) is None


async def test_fht_id_response_creates_sensor_and_home_matic_id(
    hass: HomeAssistant,
    max_connection_factory: MockConnectionFactory,
) -> None:
    config = {
        CONF_DEVICE_PATH: "/dev/tty0",
        CONF_DEVICES: {},
    }
    config_entry = MockConfigEntry(domain=DOMAIN, data=config, entry_id="test")
    assert await async_setup_entry(hass, config_entry)
    await hass.async_block_till_done()

    connection = max_connection_factory.connections[config[CONF_DEVICE_PATH]]
    connection.call_callback(EVENT_FHT_ID_UPDATE, {"fht_id": "1034"})
    await hass.async_block_till_done()

    sensor_ids = hass.states.async_entity_ids("sensor")
    assert len(sensor_ids) == 1
    state = hass.states.get(sensor_ids[0])
    assert state.state == "1034"
    assert state.attributes["homematic_id"] == "F11034"


@pytest.mark.parametrize(
    "pairing_event", [EVENT_DEVICE_PAIRED, EVENT_DEVICE_REPAIRED]
)
async def test_pairing_persists_device_and_creates_entities(
    hass: HomeAssistant,
    max_connection_factory: MockConnectionFactory,
    pairing_event: str,
):
    """Persist newly announced paired or repaired devices and create their entities."""

    config = {
        CONF_DEVICE_PATH: '/dev/tty0',
        CONF_DEVICES: {}
    }
    config_entry = MockConfigEntry(domain=DOMAIN, data=config, entry_id='test')

    assert await async_setup_entry(hass, config_entry)
    await hass.async_block_till_done()

    assert not hass.data.get('entity_registry').entities

    connection = max_connection_factory.connections[config[CONF_DEVICE_PATH]]
    assert connection

    paired_payload = {
        ATTR_DEVICE_ID: 12345,
        ATTR_DEVICE_TYPE: HEATING_THERMOSTAT,
        ATTR_DEVICE_SERIAL: 'Thermostat1',
        ATTR_FIRMWARE_VERSION: 1.0
    }
    connection.call_callback(pairing_event, paired_payload)
    connection.call_callback(pairing_event, paired_payload)

    await hass.async_block_till_done()
    await hass.async_block_till_done()

    assert hass.data.get('entity_registry').entities.get('climate.thermostat1')
    assert len(hass.states.async_entity_ids('climate')) == 1
    assert len(hass.states.async_entity_ids('binary_sensor')) == 1

    device_registry = dr.async_get(hass)
    device = next(
        device
        for device in dr.async_entries_for_config_entry(
            device_registry, config_entry.entry_id
        )
        if (DOMAIN, "12345") in device.identifiers
    )
    assert device.model == "Thermostat"

    device = config_entry.data.get(CONF_DEVICES).get('12345')
    assert device == {
        CONF_NAME: 'Thermostat1',
        CONF_TYPE: HEATING_THERMOSTAT
    }


async def test_enable_pairing_uses_only_loaded_cul_when_path_is_omitted(
    hass: HomeAssistant,
    max_connection_factory: MockConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_DEVICE_PATH: "/dev/tty0", CONF_DEVICES: {}},
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    calls = []
    monkeypatch.setattr(
        max_connection_factory.connections["/dev/tty0"],
        "enable_pairing",
        calls.append,
    )
    await hass.services.async_call(DOMAIN, SERVICE_ENABLE_PAIRING, {}, blocking=True)

    assert calls == [30]


async def test_enable_pairing_requires_path_when_multiple_culs_are_loaded(
    hass: HomeAssistant,
    max_connection_factory: MockConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serial_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_DEVICE_PATH: "/dev/tty0", CONF_DEVICES: {}},
    )
    network_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_DEVICE_PATH: "telnet://maxcul.local:2323", CONF_DEVICES: {}},
    )
    serial_entry.add_to_hass(hass)
    network_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(serial_entry.entry_id)
    assert await hass.config_entries.async_setup(network_entry.entry_id)
    await hass.async_block_till_done()

    calls = []
    monkeypatch.setattr(
        max_connection_factory.connections["telnet://maxcul.local:2323"],
        "enable_pairing",
        calls.append,
    )
    with pytest.raises(HomeAssistantError, match="specify device_path"):
        await hass.services.async_call(
            DOMAIN, SERVICE_ENABLE_PAIRING, {}, blocking=True
        )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_ENABLE_PAIRING,
        {"device_path": "MAXCUL.local:2323", "duration": 45},
        blocking=True,
    )

    assert calls == [45]
