'''
Test module for climate entities
'''

from homeassistant.const import (
    CONF_DEVICES,
    CONF_NAME,
    CONF_TYPE
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers import device_registry as dr

from homeassistant.components.climate import (
    HVAC_MODE_HEAT
)

from pytest_homeassistant_custom_component.common import (
  MockConfigEntry
)

from custom_components.maxcul._vendor.pymaxcul._const import (
    ATTR_DEVICE_ID,
    ATTR_BATTERY_LOW,
    ATTR_MEASURED_TEMPERATURE,
    ATTR_DESIRED_TEMPERATURE,
    ATTR_VALVE_POSITION,
    ATTR_MODE,
    HEATING_THERMOSTAT,
    MODE_MANUAL
)

from custom_components.maxcul import (
    CONF_DEVICE_PATH,
    DOMAIN,
    SIGNAL_THERMOSTAT_UPDATE,
    async_setup_entry
)


async def test_update(hass: HomeAssistant):
    ''' Test updating of thermostat state '''
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

    assert hass.states.get('climate.thermostat1').state == 'unknown'

    async_dispatcher_send(
        hass,
        SIGNAL_THERMOSTAT_UPDATE,
        {
            ATTR_DEVICE_ID: 12345,
            ATTR_MEASURED_TEMPERATURE: 23.0,
            ATTR_DESIRED_TEMPERATURE: 21.5,
            ATTR_VALVE_POSITION: 0,
            ATTR_MODE: MODE_MANUAL,
            ATTR_BATTERY_LOW: False
        }
    )

    await hass.async_block_till_done()

    state = hass.states.get('climate.thermostat1')
    assert state.state == HVAC_MODE_HEAT
    assert state.attributes.get('temperature') == 21.5
    assert state.attributes.get('current_temperature') == 23.0


async def test_wall_thermostat_plus_update(hass: HomeAssistant):
    '''Test the Max! Wall Thermostat Plus device type.'''
    config = {
        CONF_DEVICE_PATH: '/dev/tty0',
        CONF_DEVICES: {
            '67890': {
                CONF_NAME: 'WallThermostatPlus',
                CONF_TYPE: 'WALL_THERMOSTAT_PLUS'
            }
        }
    }
    config_entry = MockConfigEntry(domain=DOMAIN, data=config, entry_id='test-wall')

    assert await async_setup_entry(hass, config_entry)
    await hass.async_block_till_done()

    assert hass.states.get('climate.wallthermostatplus').state == 'unknown'

    async_dispatcher_send(
        hass,
        SIGNAL_THERMOSTAT_UPDATE,
        {
            ATTR_DEVICE_ID: 67890,
            ATTR_MEASURED_TEMPERATURE: 20.5,
            ATTR_DESIRED_TEMPERATURE: 19.0,
            ATTR_VALVE_POSITION: 35,
            ATTR_MODE: MODE_MANUAL,
            ATTR_BATTERY_LOW: False
        }
    )

    await hass.async_block_till_done()

    state = hass.states.get('climate.wallthermostatplus')
    assert state is not None
    assert state.state == HVAC_MODE_HEAT
    assert state.attributes.get('temperature') == 19.0
    assert state.attributes.get('current_temperature') == 20.5
    assert state.attributes.get('icon') == 'mdi:thermometer'

    device = dr.async_get(hass).async_get_device({(DOMAIN, '67890')})
    assert device is not None
    assert device.model == 'Wall thermostat Plus'


async def test_heating_thermostat_plus_uses_heating_thermostat_presentation(
    hass: HomeAssistant,
):
    config = {
        CONF_DEVICE_PATH: '/dev/tty0',
        CONF_DEVICES: {
            '67891': {
                CONF_NAME: 'HeatingThermostatPlus',
                CONF_TYPE: 'HEATING_THERMOSTAT_PLUS'
            }
        }
    }
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=config, entry_id='test-heating-plus'
    )

    assert await async_setup_entry(hass, config_entry)
    await hass.async_block_till_done()

    state = hass.states.get('climate.heatingthermostatplus')
    assert state is not None
    assert state.attributes.get('icon') == 'mdi:home-thermometer'

    device = dr.async_get(hass).async_get_device({(DOMAIN, '67891')})
    assert device is not None
    assert device.model == 'Heating thermostat Plus'
