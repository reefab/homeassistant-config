"""
Adds support for generic thermostat units.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/climate.generic_thermostat/
"""
import asyncio
import logging

import voluptuous as vol

from homeassistant.core import callback
from homeassistant.core import DOMAIN as HA_DOMAIN
from homeassistant.components.climate import (
    STATE_DRY, STATE_IDLE, STATE_AUTO, ClimateDevice,
    ATTR_OPERATION_MODE, ATTR_AWAY_MODE, ATTR_HUMIDITY, ATTR_MIN_HUMIDITY,
    ATTR_MAX_HUMIDITY, ATTR_CURRENT_HUMIDITY, ATTR_OPERATION_LIST,
    SUPPORT_OPERATION_MODE, SUPPORT_AWAY_MODE, SUPPORT_TARGET_HUMIDITY,
    PLATFORM_SCHEMA)
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT, STATE_ON, STATE_OFF, CONF_NAME, ATTR_ENTITY_ID,
    SERVICE_TURN_ON, SERVICE_TURN_OFF)
from homeassistant.helpers import condition
from homeassistant.helpers.event import (
    async_track_state_change, async_track_time_interval)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import async_get_last_state

_LOGGER = logging.getLogger(__name__)

DEPENDENCIES = ['switch', 'sensor']

DEFAULT_TOLERANCE = 1
DEFAULT_NAME = 'Generic Humidity Control'
DEFAULT_AWAY_HUMIDITY = 50

CONF_HUMIDIFIER = 'humidifier'
CONF_SENSOR = 'target_sensor'
CONF_MIN_HUMIDITY = 'min_humidity'
CONF_MAX_HUMIDITY = 'max_humidity'
CONF_TARGET_HUMIDITY = 'target_humidity'
CONF_DEHUMIDIFIER_MODE = 'dehumidifier_mode'
CONF_MIN_DUR = 'min_cycle_duration'
CONF_DRY_TOLERANCE = 'dry_tolerance'
CONF_DAMP_TOLERANCE = 'damp_tolerance'
CONF_KEEP_ALIVE = 'keep_alive'
CONF_INITIAL_OPERATION_MODE = 'initial_operation_mode'
CONF_AWAY_HUMIDITY = 'away_humidity'
SUPPORT_FLAGS = (SUPPORT_TARGET_HUMIDITY | SUPPORT_AWAY_MODE |
                 SUPPORT_OPERATION_MODE)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_HUMIDIFIER): cv.entity_id,
    vol.Required(CONF_SENSOR): cv.entity_id,
    vol.Optional(CONF_DEHUMIDIFIER_MODE): cv.boolean,
    vol.Optional(CONF_MAX_HUMIDITY): vol.Coerce(float),
    vol.Optional(CONF_MIN_DUR): vol.All(cv.time_period, cv.positive_timedelta),
    vol.Optional(CONF_MIN_HUMIDITY): vol.Coerce(float),
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_DRY_TOLERANCE, default=DEFAULT_TOLERANCE): vol.Coerce(
        float),
    vol.Optional(CONF_DAMP_TOLERANCE, default=DEFAULT_TOLERANCE): vol.Coerce(
        float),
    vol.Optional(CONF_TARGET_HUMIDITY,
                 default=DEFAULT_AWAY_HUMIDITY): vol.Coerce(float),
    vol.Optional(CONF_KEEP_ALIVE): vol.All(
        cv.time_period, cv.positive_timedelta),
    vol.Optional(CONF_INITIAL_OPERATION_MODE):
        vol.In([STATE_AUTO, STATE_OFF]),
    vol.Optional(CONF_AWAY_HUMIDITY,
                 default=DEFAULT_AWAY_HUMIDITY): vol.Coerce(float)
})


@asyncio.coroutine
def async_setup_platform(hass, config, async_add_devices, discovery_info=None):
    """Set up the generic humidity control platform."""
    name = config.get(CONF_NAME)
    humidifier_entity_id = config.get(CONF_HUMIDIFIER)
    sensor_entity_id = config.get(CONF_SENSOR)
    min_humidity = config.get(CONF_MIN_HUMIDITY)
    max_humidity = config.get(CONF_MAX_HUMIDITY)
    target_humidity = config.get(CONF_TARGET_HUMIDITY)
    dehumidifier_mode = config.get(CONF_DEHUMIDIFIER_MODE)
    min_cycle_duration = config.get(CONF_MIN_DUR)
    dry_tolerance = config.get(CONF_DRY_TOLERANCE)
    damp_tolerance = config.get(CONF_DAMP_TOLERANCE)
    keep_alive = config.get(CONF_KEEP_ALIVE)
    initial_operation_mode = config.get(CONF_INITIAL_OPERATION_MODE)
    away_humidity = config.get(CONF_AWAY_HUMIDITY)

    async_add_devices([GenericHumidityControl(
        hass, name, humidifier_entity_id, sensor_entity_id, min_humidity,
        max_humidity, target_humidity, dehumidifier_mode, min_cycle_duration,
        dry_tolerance, damp_tolerance, keep_alive, initial_operation_mode,
        away_humidity)])


class GenericHumidityControl(ClimateDevice):
    """Representation of a Generic Humidity Control device."""

    def __init__(self, hass, name, humidifier_entity_id, sensor_entity_id,
                 min_humidity, max_humidity, target_humidity,
                 dehumidifier_mode, min_cycle_duration, dry_tolerance,
                 damp_tolerance, keep_alive, initial_operation_mode,
                 away_humidity):
        """Initialize the thermostat."""
        self.hass = hass
        self._name = name
        self.humidifier_entity_id = humidifier_entity_id
        self._sensor_entity_id = sensor_entity_id
        self.dehumidifier_mode = dehumidifier_mode
        self.min_cycle_duration = min_cycle_duration
        self._dry_tolerance = dry_tolerance
        self._damp_tolerance = damp_tolerance
        self._keep_alive = keep_alive
        self._initial_operation_mode = initial_operation_mode
        self._saved_target_humidity = target_humidity if target_humidity \
            is not None else away_humidity
        if self.dehumidifier_mode:
            self._current_operation = STATE_DRY
            self._operation_list = [STATE_DRY, STATE_OFF]
        else:
            self._current_operation = STATE_ON
            self._operation_list = [STATE_ON, STATE_OFF]
        if initial_operation_mode == STATE_OFF:
            self._enabled = False
        else:
            self._enabled = True
        self._active = False
        self._cur_humidity = None
        self._min_humidity = min_humidity
        self._max_humidity = max_humidity
        self._target_humidity = target_humidity
        self._unit = '%'
        self._away_humidity = away_humidity
        self._is_away = False

        async_track_state_change(
            hass, sensor_entity_id, self._async_sensor_changed)
        async_track_state_change(
            hass, humidifier_entity_id, self._async_switch_changed)

        if self._keep_alive:
            async_track_time_interval(
                hass, self._async_keep_alive, self._keep_alive)

    @asyncio.coroutine
    def async_added_to_hass(self):
        """Run when entity about to be added."""
        # Check If we have an old state
        old_state = yield from async_get_last_state(self.hass,
                                                    self.entity_id)
        if old_state is not None:
            # If we have no initial humidity, restore
            if self._target_humidity is None:
                # If we have a previously saved humidity
                if old_state.attributes[ATTR_HUMIDITY] is None:
                    if self.dehumidifier_mode:
                        self._target_humidity = self.max_humidity
                    else:
                        self._target_humidity = self.min_humidity
                    _LOGGER.warning('Undefined target humidity, \
                                    falling back to %s', self._target_humidity)
                else:
                    self._target_humidity = float(
                        old_state.attributes[ATTR_HUMIDITY])
            self._is_away = True if str(
                old_state.attributes[ATTR_AWAY_MODE]) == STATE_ON else False
            if old_state.attributes[ATTR_OPERATION_MODE] == STATE_OFF:
                self._current_operation = STATE_OFF
                self._enabled = False
            if self._initial_operation_mode is None:
                if old_state.attributes[ATTR_OPERATION_MODE] == STATE_OFF:
                    self._enabled = False

    @property
    def state_attributes(self):
        """Return the optional state attributes."""
        data = {}

        humidity = self.target_humidity
        if humidity is not None:
            data[ATTR_HUMIDITY] = humidity
            data[ATTR_CURRENT_HUMIDITY] = self.current_humidity
            data[ATTR_MIN_HUMIDITY] = self.min_humidity
            data[ATTR_MAX_HUMIDITY] = self.max_humidity

        operation_mode = self.current_operation
        if operation_mode is not None:
            data[ATTR_OPERATION_MODE] = operation_mode
            if self.operation_list:
                data[ATTR_OPERATION_LIST] = self.operation_list

        is_away = self.is_away_mode_on
        if is_away is not None:
            data[ATTR_AWAY_MODE] = STATE_ON if is_away else STATE_OFF

        return data

    @property
    def state(self):
        """Return the current state."""
        if self._is_device_active:
            return self.current_operation
        else:
            if self._enabled:
                return STATE_IDLE
            else:
                return STATE_OFF

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def name(self):
        """Return the name of the humidity controller."""
        return self._name

    @property
    def humidity_unit(self):
        """Return the unit of measurement."""
        return self._unit

    @property
    def current_humidity(self):
        """Return the sensor humidity."""
        return self._cur_humidity

    @property
    def current_operation(self):
        """Return current operation."""
        return self._current_operation

    @property
    def target_humidity(self):
        """Return the humidity we try to reach."""
        return self._target_humidity

    @property
    def operation_list(self):
        """List of available operation modes."""
        return self._operation_list

    def set_operation_mode(self, operation_mode):
        """Set operation mode."""
        if operation_mode == STATE_ON:
            self._current_operation = STATE_ON
            self._enabled = True
            self._async_control_humidity()
        elif operation_mode == STATE_DRY:
            self._current_operation = STATE_DRY
            self._enabled = True
            self._async_control_humidity()
        elif operation_mode == STATE_OFF:
            self._current_operation = STATE_OFF
            self._enabled = False
            if self._is_device_active:
                self._humidifier_turn_off()
        else:
            _LOGGER.error('Unrecognized operation mode: %s', operation_mode)
            return
        # Ensure we updae the current operation after changing the mode
        self.schedule_update_ha_state()

    @asyncio.coroutine
    def async_set_humidity(self, humidity):
        """Set new target humidity."""
        if humidity is None:
            return
        self._target_humidity = humidity
        self._async_control_humidity()
        yield from self.async_update_ha_state()

    @property
    def min_humidity(self):
        """Return the minimum humidity."""
        # pylint: disable=no-member
        if self._min_humidity:
            return self._min_humidity

        # get default humidity from super class
        return ClimateDevice.min_humidity.fget(self)

    @property
    def max_humidity(self):
        """Return the maximum humidity."""
        # pylint: disable=no-member
        if self._max_humidity:
            return self._max_humidity

        # Get default humidity from super class
        return ClimateDevice.max_humidity.fget(self)

    @asyncio.coroutine
    def _async_sensor_changed(self, entity_id, old_state, new_state):
        """Handle humidity changes."""
        if new_state is None:
            return

        self._async_update_humidity(new_state)
        self._async_control_humidity()
        yield from self.async_update_ha_state()

    @callback
    def _async_switch_changed(self, entity_id, old_state, new_state):
        """Handle humidifier switch state changes."""
        if new_state is None:
            return
        self.async_schedule_update_ha_state()

    @callback
    def _async_keep_alive(self, time):
        """Call at constant intervals for keep-alive purposes."""
        if self._is_device_active:
            self._humidifier_turn_on()
        else:
            self._humidifier_turn_off()

    @callback
    def _async_update_humidity(self, state):
        """Update humidity controller with latest state from sensor."""
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)

        if unit != self._unit:
            _LOGGER.error('Sensor: %s returned wrong unit',
                          self._sensor_entity_id)
            return

        try:
            self._cur_humidity = float(state.state)
        except ValueError as ex:
            _LOGGER.error('Unable to update from sensor: %s', ex)

    @callback
    def _async_control_humidity(self):
        """Check if we need to turn humidity controller on or off."""
        if not self._active and None not in (self._cur_humidity,
                                             self._target_humidity):
            self._active = True
            _LOGGER.info('Obtained current and target humidity. '
                         'Generic humidity controller active.')

        if not self._active:
            return

        if not self._enabled:
            return

        if self.min_cycle_duration:
            if self._is_device_active:
                current_state = STATE_ON
            else:
                current_state = STATE_OFF
            long_enough = condition.state(
                self.hass, self.humidifier_entity_id, current_state,
                self.min_cycle_duration)
            if not long_enough:
                return

        if self.dehumidifier_mode:
            is_drying = self._is_device_active
            if is_drying:
                too_dry = self._target_humidity - self._cur_humidity >= \
                    self._dry_tolerance
                if too_dry:
                    _LOGGER.info('Turning off dehumidifier %s',
                                 self.humidifier_entity_id)
                    self._humidifier_turn_off()
            else:
                too_damp = self._cur_humidity - self._target_humidity >= \
                    self._damp_tolerance
                if too_damp:
                    _LOGGER.info('Turning on dehumidifier %s',
                                 self.humidifier_entity_id)
                    self._humidifier_turn_on()
        else:
            is_humidifying = self._is_device_active
            if is_humidifying:
                too_damp = self._cur_humidity - self._target_humidity >= \
                    self._damp_tolerance
                if too_damp:
                    _LOGGER.info('Turning off humidifier %s',
                                 self.humidifier_entity_id)
                    self._humidifier_turn_off()
            else:
                too_dry = self._target_humidity - self._cur_humidity >= \
                    self._dry_tolerance
                if too_dry:
                    _LOGGER.info('Turning on humidifier %s',
                                 self.humidifier_entity_id)
                    self._humidifier_turn_on()

    @property
    def _is_device_active(self):
        """If the toggleable device is currently active."""
        return self.hass.states.is_state(self.humidifier_entity_id, STATE_ON)

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return SUPPORT_FLAGS

    @property
    def icon(self):
        return 'mdi:water-percent'

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement to display."""
        return "%"

    @callback
    def _humidifier_turn_on(self):
        """Turn humidifier toggleable device on."""
        data = {ATTR_ENTITY_ID: self.humidifier_entity_id}
        self.hass.async_add_job(
            self.hass.services.async_call(HA_DOMAIN, SERVICE_TURN_ON, data))

    @callback
    def _humidifier_turn_off(self):
        """Turn humidifier toggleable device off."""
        data = {ATTR_ENTITY_ID: self.humidifier_entity_id}
        self.hass.async_add_job(
            self.hass.services.async_call(HA_DOMAIN, SERVICE_TURN_OFF, data))

    @property
    def is_away_mode_on(self):
        """Return true if away mode is on."""
        return self._is_away

    def turn_away_mode_on(self):
        """Turn away mode on by setting it on away hold indefinitely."""
        self._is_away = True
        self._saved_target_humidity = self._target_humidity
        self._target_humidity = self._away_humidity
        self._async_control_humidity()
        self.schedule_update_ha_state()

    def turn_away_mode_off(self):
        """Turn away off."""
        self._is_away = False
        self._target_humidity = self._saved_target_humidity
        self._async_control_humidity()
        self.schedule_update_ha_state()
