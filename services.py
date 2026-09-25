"""D-Bus services following the Victron Venus OS D-Bus API.

See https://github.com/victronenergy/venus/wiki/dbus-api and
https://github.com/victronenergy/venus/wiki/dbus for the paths of each service type.
"""

import collections
import inspect
import logging
import os
import re

import dbus
from settingsdevice import SettingsDevice
from vedbus import VeDbusItemExport, VeDbusService

import solaredge

log = logging.getLogger(__name__)

PROCESS_NAME = 'dbus-solaredge'

# Victron product ids, see dbus-fronius/software/src/products.h
PRODUCT_ID_SOLAREDGE_PV_INVERTER = 0xA146
PRODUCT_ID_UNKNOWN = 0xFFFF

# SunSpec inverter state -> /StatusCode, same mapping as Victron's dbus-fronius (sunspec_updater.cpp)
STATUS_CODES = {
	solaredge.STATUS_OFF: 0,
	solaredge.STATUS_SLEEPING: 8,
	solaredge.STATUS_STARTING: 3,
	solaredge.STATUS_MPPT: 11,
	solaredge.STATUS_THROTTLED: 12,
	solaredge.STATUS_SHUTTING_DOWN: 8,
	solaredge.STATUS_FAULT: 10,
	solaredge.STATUS_STANDBY: 8,
}

PHASES = ('L1', 'L2', 'L3')

PvInverterReading = collections.namedtuple('PvInverterReading',
	'inverter advanced_power_control active_power_limit')


def private_bus():
	"""A separate bus connection per service, object paths are registered per connection."""
	if 'DBUS_SESSION_BUS_ADDRESS' in os.environ:
		return dbus.bus.BusConnection(os.environ['DBUS_SESSION_BUS_ADDRESS'])
	return dbus.bus.BusConnection(dbus.bus.BusConnection.TYPE_SYSTEM)


def velib_supports(function, parameter):
	"""The velib_python version depends on the Venus OS version, check for optional features."""
	return parameter in inspect.signature(function).parameters


def make_ident(*parts):
	"""Identifier usable in D-Bus service names and settings paths."""
	return re.sub(r'[^A-Za-z0-9_]', '_', '_'.join(str(p) for p in parts if p))


def text(unit, digits=None):
	def fmt(path, value):
		if digits is not None:
			value = round(value, digits)
		return '%s%s' % (value, unit)
	return fmt


_kwh = text('kWh', 3)
_a = text('A', 2)
_w = text('W', 2)
_v = text('V', 2)
_c = text('C', 1)
_pct = text('%')


class RefreshableItem(VeDbusItemExport):
	"""Calls the onchange callback also when the same value is written again.

	Needed for /Ac/PowerLimit: the ESS rewrites the limit periodically to keep it alive.
	"""

	def is_equal(self, newvalue):
		return False


class DbusDevice(object):
	"""A device published as one service on the D-Bus.

	The device instance is assigned by localsettings (/Settings/Devices/<ident>/ClassAndVrmInstance),
	the service is registered on the D-Bus only after all paths are added and populated.
	"""

	service_type = None
	default_instance = 0
	product_id = PRODUCT_ID_UNKNOWN
	default_custom_name = ''

	def __init__(self, ident, info, connection, version, settings_bus, on_restart):
		self.ident = ident
		self.on_restart = on_restart
		self.settings_path = '/Settings/Devices/' + ident
		self.default_class_instance = '%s:%d' % (self.service_type, self.default_instance)
		self.setting_paths = {}  # setting name -> D-Bus path

		supported = {
			'instance': [self.settings_path + '/ClassAndVrmInstance', self.default_class_instance, 0, 0],
			'customname': [self.settings_path + '/CustomName', self.default_custom_name, 0, 0],
		}
		supported.update(self.settings_definition())
		self.settings_definitions = supported
		self.settings = SettingsDevice(settings_bus, supported, self._setting_changed, timeout=10)
		self.instance = self._read_instance()

		self.service_name = 'com.victronenergy.%s.%s' % (self.service_type, ident)
		self.service, self._register_later = self._create_service(self.service_name)
		self.service.add_mandatory_paths(
			processname=PROCESS_NAME,
			processversion=version,
			connection=connection,
			deviceinstance=self.instance,
			productid=self.product_id,
			productname=('%s %s' % (info.manufacturer, info.model)).strip(),
			firmwareversion=info.version,
			hardwareversion=None,
			connected=1)
		self.service.add_path('/Serial', info.serial)
		self.add_setting_path('/CustomName', 'customname')
		self.add_paths()

	@staticmethod
	def _create_service(name):
		if velib_supports(VeDbusService.__init__, 'register'):
			return VeDbusService(name, private_bus(), register=False), True
		return VeDbusService(name, private_bus()), False  # velib before 2024 registers immediately

	def settings_definition(self):
		"""Additional settings: name -> [path, default, min, max]."""
		return {}

	def add_paths(self):
		"""Add the device specific paths."""

	def add_setting_path(self, path, setting):
		"""Publish a setting as writeable path, changes are stored by localsettings."""
		self.setting_paths[setting] = path
		self.service.add_path(path, self.settings[setting], writeable=True,
			onchangecallback=lambda p, v: self._write_setting(setting, v))

	def _write_setting(self, setting, value):
		default, minimum, maximum = self.settings_definitions[setting][1:4]
		if isinstance(default, int):
			try:
				value = int(value)
			except (TypeError, ValueError):
				return False
			if not minimum <= value <= maximum:
				return False
		elif not isinstance(value, str):
			return False
		self.settings[setting] = value
		return True

	def _read_instance(self):
		try:
			service_type, instance = self.settings['instance'].split(':')
			return int(instance)
		except (AttributeError, ValueError):
			log.warning('%s: invalid ClassAndVrmInstance %r, resetting' % (self.ident, self.settings['instance']))
			self.instance = self.default_instance
			self.settings['instance'] = self.default_class_instance
			return self.default_instance

	def _setting_changed(self, setting, old, new):
		if setting == 'instance':
			try:
				instance = int(str(new).split(':')[1])
			except (IndexError, ValueError):
				instance = None
			if instance != self.instance:
				log.info('%s: device instance changed from %s to %s, restarting' % (self.ident, old, new))
				self.on_restart()
		elif setting in self.setting_paths:
			self.service[self.setting_paths[setting]] = new

	def register(self):
		if self._register_later:
			self.service.register()
		log.info('registered %s with device instance %d' % (self.service_name, self.instance))

	def update(self, reading):
		# collect all changes and send them as one ItemsChanged signal
		with self.service as s:
			self.publish(s, reading)

	def publish(self, s, reading):
		raise NotImplementedError


class GridMeter(DbusDevice):
	service_type = 'grid'
	default_instance = 0

	def add_paths(self):
		s = self.service
		s.add_path('/Ac/Power', None, gettextcallback=_w)
		s.add_path('/Ac/Energy/Forward', None, gettextcallback=_kwh)  # bought from the grid
		s.add_path('/Ac/Energy/Reverse', None, gettextcallback=_kwh)  # sold to the grid
		for phase in PHASES:
			s.add_path('/Ac/%s/Voltage' % phase, None, gettextcallback=_v)
			s.add_path('/Ac/%s/Current' % phase, None, gettextcallback=_a)
			s.add_path('/Ac/%s/Power' % phase, None, gettextcallback=_w)
			s.add_path('/Ac/%s/Energy/Forward' % phase, None, gettextcallback=_kwh)
			s.add_path('/Ac/%s/Energy/Reverse' % phase, None, gettextcallback=_kwh)
		s.add_path('/ErrorCode', 0)

	def publish(self, s, reading):
		s['/Ac/Power'] = reading.power
		s['/Ac/Energy/Forward'] = reading.energy_forward
		s['/Ac/Energy/Reverse'] = reading.energy_reverse
		for name, phase in zip(PHASES, reading.phases):
			s['/Ac/%s/Voltage' % name] = phase.voltage
			s['/Ac/%s/Current' % name] = phase.current
			s['/Ac/%s/Power' % name] = phase.power
			s['/Ac/%s/Energy/Forward' % name] = phase.energy_forward
			s['/Ac/%s/Energy/Reverse' % name] = phase.energy_reverse


class PvInverter(DbusDevice):
	"""com.victronenergy.pvinverter, including the zero feed-in power limit used by ESS."""

	service_type = 'pvinverter'
	default_instance = 20
	product_id = PRODUCT_ID_SOLAREDGE_PV_INVERTER

	def __init__(self, ident, info, connection, version, settings_bus, on_restart, inverter, max_power, limiter):
		self.inverter = inverter
		self.max_power = max_power
		self.limiter = limiter
		DbusDevice.__init__(self, ident, info, connection, version, settings_bus, on_restart)

	def settings_definition(self):
		return {'position': [self.settings_path + '/Position', 0, 0, 2]}

	def add_paths(self):
		s = self.service
		s.add_path('/Ac/Power', None, gettextcallback=_w)
		s.add_path('/Ac/Energy/Forward', None, gettextcallback=_kwh)
		for phase in PHASES:
			s.add_path('/Ac/%s/Voltage' % phase, None, gettextcallback=_v)
			s.add_path('/Ac/%s/Current' % phase, None, gettextcallback=_a)
			s.add_path('/Ac/%s/Power' % phase, None, gettextcallback=_w)
			s.add_path('/Ac/%s/Energy/Forward' % phase, None, gettextcallback=_kwh)
		s.add_path('/Ac/MaxPower', self.max_power or None, gettextcallback=_w)
		s.add_path('/StatusCode', None)
		s.add_path('/ErrorCode', None)
		self.add_setting_path('/Position', 'position')
		s.add_path('/PositionIsAdjustable', 1)

		# only published if the limiter is enabled, see dbus api: must not exist otherwise
		if self.limiter is not None:
			kwargs = dict(value=self.max_power, description='Zero feed-in power limit in W', writeable=True,
				onchangecallback=self._set_power_limit, gettextcallback=_w)
			if velib_supports(s.add_path, 'itemtype'):
				kwargs['itemtype'] = RefreshableItem
			else:  # velib before 2023: an unchanged limit is not seen as refresh
				log.warning('velib_python without itemtype, power limit refreshes are not detected')
			s.add_path('/Ac/PowerLimit', **kwargs)

		# SolarEdge specific extension paths
		s.add_path('/Ac/AdvancedPwrControlEn', None, description='SolarEdge advanced power control (0/1)',
			writeable=True, onchangecallback=self._set_advanced_power_control)
		s.add_path('/Ac/ActivePowerLimit', None, description='SolarEdge active power limit in %',
			writeable=True, onchangecallback=self._set_active_power_limit, gettextcallback=_pct)

	def publish(self, s, reading):
		s['/Ac/Power'] = reading.inverter.power
		s['/Ac/Energy/Forward'] = reading.inverter.energy
		for i, name in enumerate(PHASES):
			phase = reading.inverter.phases[i] if i < len(reading.inverter.phases) else None
			s['/Ac/%s/Voltage' % name] = phase and phase.voltage
			s['/Ac/%s/Current' % name] = phase and phase.current
			s['/Ac/%s/Power' % name] = phase and phase.power
			s['/Ac/%s/Energy/Forward' % name] = phase and phase.energy_forward
		s['/StatusCode'] = STATUS_CODES.get(reading.inverter.status)
		s['/ErrorCode'] = reading.inverter.error_code
		s['/Ac/AdvancedPwrControlEn'] = reading.advanced_power_control
		s['/Ac/ActivePowerLimit'] = reading.active_power_limit

	def check_power_limit(self):
		"""Remove an ESS power limit which was not refreshed in time."""
		if self.limiter is not None and self.limiter.expired():
			log.info('power limit expired, removing it')
			self.service['/Ac/PowerLimit'] = self.limiter.reset()

	def _set_power_limit(self, path, value):
		try:
			watts = float(value)
		except (TypeError, ValueError):
			return False
		if watts != watts:  # NaN
			return False
		try:
			self.limiter.set_limit(watts)
		except Exception:
			log.error('setting power limit failed', exc_info=True)
			return False
		return True

	def _set_advanced_power_control(self, path, value):
		if value not in (0, 1):
			return False
		return self._write(path, self.inverter.write_advanced_power_control, value == 1)

	def _set_active_power_limit(self, path, value):
		try:
			percent = int(round(float(value)))
		except (TypeError, ValueError):
			return False
		if not 0 <= percent <= 100:
			return False
		return self._write(path, self.inverter.write_active_power_limit, percent)

	def _write(self, path, function, value):
		log.info('%s set to %s' % (path, value))
		try:
			function(value)
		except Exception:
			log.error('writing %s failed' % path, exc_info=True)
			return False
		return True


class InverterTemperature(DbusDevice):
	service_type = 'temperature'
	default_instance = 26
	default_custom_name = 'PV inverter temperature'

	def settings_definition(self):
		return {'temperaturetype': [self.settings_path + '/TemperatureType', 2, 0, 6]}  # 2 = generic

	def add_paths(self):
		self.service.add_path('/Temperature', None, gettextcallback=_c)
		self.service.add_path('/Status', 0)
		self.add_setting_path('/TemperatureType', 'temperaturetype')

	def publish(self, s, reading):
		s['/Temperature'] = reading.temperature


class ThrottleInput(DbusDevice):
	"""Digital input which is active while the inverter is throttled (optional)."""

	service_type = 'digitalinput'
	default_instance = 10
	default_custom_name = 'PV inverter throttled'

	TYPE_GENERIC = 2
	STATE_OFF = 2
	STATE_ON = 3
	MIN_POWER = 100  # W, below this the inverter is not considered to be throttled

	def add_paths(self):
		self.service.add_path('/Type', self.TYPE_GENERIC)
		self.service.add_path('/State', None)
		self.service.add_path('/Status', 0)
		self.service.add_path('/Alarm', None)

	def publish(self, s, reading):
		throttled = reading.status == solaredge.STATUS_THROTTLED and (reading.power or 0) > self.MIN_POWER
		s['/State'] = self.STATE_ON if throttled else self.STATE_OFF
		s['/Alarm'] = 2 if throttled else 0
