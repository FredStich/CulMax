'''
Test module for binary sensors
'''

from homeassistant.const import (
    CONF_DEVICES,
    CONF_NAME,
    CONF_TYPE
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from pytest_homeassistant_custom_component.common import (
  MockConfigEntry
)

from custom_components.maxcul._vendor.pymaxcul._const import (
    ATTR_DEVICE_ID,
    ATTR_DEVICE_SERIAL,
    ATTR_DEVICE_TYPE,
    ATTR_BATTERY_LOW,
    ATTR_STATE,
    PUSH_BUTTON,
    SHUTTER_CONTACT,
    SHUTTER_OPEN
)

from custom_components.maxcul import (
    CONF_DEVICE_PATH,
    DOMAIN,
    SIGNAL_SHUTTER_UPDATE,
    async_setup_entry
)
from custom_components.maxcul._vendor.pymaxcul import (
    EVENT_DEVICE_PAIRED,
    EVENT_PUSH_BUTTON_UPDATE,
)
from custom_components.maxcul._vendor.pymaxcul._messages import (
    PushButtonStateMessage,
)


async def test_update(hass: HomeAssistant):
    ''' Test state update of shutter contacts '''

    config = {
        CONF_DEVICE_PATH: '/dev/tty0',
        CONF_DEVICES: {
            '12345': {
                CONF_NAME: 'ShutterContact1',
                CONF_TYPE: SHUTTER_CONTACT
            }
        }
    }
    config_entry = MockConfigEntry(domain=DOMAIN, data=config, entry_id='test')

    assert await async_setup_entry(hass, config_entry)
    await hass.async_block_till_done()

    assert hass.states.get('binary_sensor.shuttercontact1').state == 'unknown'
    assert hass.states.get('binary_sensor.shuttercontact1_battery').state == 'unknown'

    async_dispatcher_send(
        hass,
        SIGNAL_SHUTTER_UPDATE,
        {
            ATTR_DEVICE_ID: 12345,
            ATTR_BATTERY_LOW: False,
            ATTR_STATE: SHUTTER_OPEN
        }
    )

    await hass.async_block_till_done()

    assert hass.states.get('binary_sensor.shuttercontact1').state == 'on'


async def test_push_button_state_and_battery_updates(
    hass: HomeAssistant, max_connection_factory
):
    """Expose the Eco Taster's reported switch position and battery state."""
    config = {
        CONF_DEVICE_PATH: '/dev/tty0',
        CONF_DEVICES: {},
    }
    config_entry = MockConfigEntry(domain=DOMAIN, data=config, entry_id='test-eco')

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    connection = max_connection_factory.connections[config[CONF_DEVICE_PATH]]
    connection.call_callback(
        EVENT_DEVICE_PAIRED,
        {
            ATTR_DEVICE_ID: 12346,
            ATTR_DEVICE_TYPE: PUSH_BUTTON,
            ATTR_DEVICE_SERIAL: 'EcoTaster1',
        },
    )
    await hass.async_block_till_done()

    state_entities = [
        entity_id
        for entity_id in hass.states.async_entity_ids('binary_sensor')
        if entity_id != 'binary_sensor.ecotaster1_battery'
    ]
    assert len(state_entities) == 1
    state_entity_id = state_entities[0]
    assert hass.states.get(state_entity_id).state == 'unknown'

    connection.call_callback(
        EVENT_PUSH_BUTTON_UPDATE,
        {
            ATTR_DEVICE_ID: 12346,
            ATTR_STATE: True,
            ATTR_BATTERY_LOW: False,
        },
    )
    await hass.async_block_till_done()

    assert hass.states.get(state_entity_id).state == 'on'
    assert hass.states.get('binary_sensor.ecotaster1_battery').state == 'off'
    assert hass.data.get('entity_registry').entities[state_entity_id].unique_id == (
        '12346-push-button'
    )
    assert config_entry.data[CONF_DEVICES]['12346'][CONF_TYPE] == PUSH_BUTTON


def test_push_button_message_decodes_toggle_and_battery_bits():
    """The MAX! state packet carries the toggle in byte two."""
    payload = PushButtonStateMessage.decode_payload("9001")

    assert payload["state"] is True
    assert payload["is_paired"] is True
    assert payload["battery_low"] is True
