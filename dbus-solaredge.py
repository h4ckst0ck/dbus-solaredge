#!/usr/bin/env python3
"""Publish a SolarEdge inverter and its energy meter (SunSpec Modbus TCP) on the Venus OS D-Bus."""

import argparse
import configparser
import faulthandler
import logging
import os
import sys
import threading
import time

import dbus
from dbus.mainloop.glib import DBusGMainLoop, threads_init
from gi.repository import GLib
from pymodbus.client.sync import ModbusTcpClient

# velib_python is part of Venus OS, its location differs between versions
sys.path.insert(1, '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python')
sys.path.insert(2, '/opt/victronenergy/dbus-modem')

import services  # noqa: E402 (needs velib_python on the path)
import solaredge  # noqa: E402

VERSION = '1.0.0'
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini')
WATCHDOG_TIMEOUT = 60  # s, exit when the main loop hangs (e.g. blocked in a Modbus call)

log = logging.getLogger('dbus-solaredge')


class Config(object):
	"""Configuration from config.ini, see config.sample.ini for a description of all options."""

	DEFAULTS = {
		'modbus': {'host': '192.168.178.80', 'port': '502', 'unit': '126', 'timeout': '2.0'},
		'inverter': {'max_power': '0', 'power_limit': 'yes', 'power_limit_timeout': '120', 'throttle_input': 'no'},
		'driver': {'update_interval': '1.0', 'fail_timeout': '10'},
	}

	def __init__(self, parser):
		self.host = parser.get('modbus', 'host')
		self.port = parser.getint('modbus', 'port')
		self.unit = parser.getint('modbus', 'unit')
		self.modbus_timeout = parser.getfloat('modbus', 'timeout')
		self.max_power = parser.getfloat('inverter', 'max_power')
		self.power_limit = parser.getboolean('inverter', 'power_limit')
		self.power_limit_timeout = parser.getint('inverter', 'power_limit_timeout')
		self.throttle_input = parser.getboolean('inverter', 'throttle_input')
		self.update_interval = parser.getfloat('driver', 'update_interval')
		self.fail_timeout = parser.getfloat('driver', 'fail_timeout')
		if not 30 <= self.power_limit_timeout <= 600:
			raise ValueError('power_limit_timeout must be between 30 and 600 s')

	@classmethod
	def load(cls, path=CONFIG_FILE, overrides=None):
		parser = configparser.ConfigParser()
		parser.read_dict(cls.DEFAULTS)
		if path and os.path.exists(path):
			parser.read(path)
		for (section, option), value in (overrides or {}).items():
			if value is not None:
				parser.set(section, option, str(value))
		return cls(parser)


class Watchdog(object):
	"""Exit the process when the main loop stops calling update(), like Victron's dbus-modbus-client."""

	def __init__(self, timeout, clock=time.monotonic, exit=os._exit):
		self.timeout = timeout
		self.clock = clock
		self.exit = exit
		self.last = clock()

	def update(self):
		self.last = self.clock()

	def check(self):
		if self.clock() - self.last > self.timeout:
			log.error('watchdog timeout, main loop hangs')
			faulthandler.dump_traceback()
			self.exit(1)
			return False
		return True

	def run(self, sleep=time.sleep):
		while self.check():
			sleep(self.timeout / 4.0)

	def start(self):
		thread = threading.Thread(target=self.run, name='watchdog')
		thread.daemon = True
		thread.start()


class Driver(object):
	"""Reads the inverter and meter cyclically and publishes the values on the D-Bus.

	Follows the Venus OS driver guidelines: when the inverter cannot be reached for `fail_timeout`
	seconds the driver exits and daemontools restarts it with a clean state.
	"""

	def __init__(self, config, client, settings_bus, clock=time.monotonic):
		self.config = config
		self.settings_bus = settings_bus
		self.clock = clock
		self.modbus = solaredge.ModbusDevice(client, config.unit)
		self.inverter = solaredge.Inverter(self.modbus)
		self.meter = solaredge.Meter(self.modbus)
		self.devices = []
		self.throttle = None
		self.last_update = None
		self.failing = False
		self.exit_code = None
		self.on_exit = None

	def setup(self):
		inverter_info = self.inverter.read_info()
		meter_info = self.meter.read_info()
		log.info('inverter: %s %s, serial %s, firmware %s, %d phase(s)' % (inverter_info.manufacturer,
			inverter_info.model, inverter_info.serial, inverter_info.version, self.inverter.phases))
		log.info('meter: %s %s, serial %s' % (meter_info.manufacturer, meter_info.model, meter_info.serial))

		max_power = self.config.max_power or self.inverter.read_max_power()
		log.info('inverter max power: %s W' % max_power)
		limiter = None
		if not self.config.power_limit:
			log.info('power limiting disabled in the configuration')
		elif max_power > 0:
			limiter = solaredge.PowerLimiter(self.modbus, max_power, self.config.power_limit_timeout, self.clock)
			limiter.initialize()
		else:
			log.warning('max power of the inverter is unknown, power limiting disabled')

		connection = 'Modbus TCP %s:%d unit %d' % (self.config.host, self.config.port, self.config.unit)
		common = dict(connection=connection, version=VERSION, settings_bus=self.settings_bus,
			on_restart=self.restart)
		inverter_ident = services.make_ident('solaredge', inverter_info.serial or 'unit%d' % self.config.unit)
		meter_ident = services.make_ident('solaredge', 'meter', meter_info.serial)

		self.grid = services.GridMeter(meter_ident, meter_info, **common)
		self.pv = services.PvInverter(inverter_ident, inverter_info, inverter=self.inverter, max_power=max_power,
			limiter=limiter, **common)
		self.temperature = services.InverterTemperature(inverter_ident + '_temperature', inverter_info, **common)
		self.devices = [self.grid, self.pv, self.temperature]
		if self.config.throttle_input:
			self.throttle = services.ThrottleInput(inverter_ident + '_throttle', inverter_info, **common)
			self.devices.append(self.throttle)

		# publish the first values before the services go online
		self.poll()
		for device in self.devices:
			device.register()

	def poll(self):
		inverter = self.inverter.read()
		meter = self.meter.read()
		pv = services.PvInverterReading(inverter, self.inverter.read_advanced_power_control(),
			self.inverter.read_active_power_limit())

		self.grid.update(meter)
		self.pv.update(pv)
		self.temperature.update(inverter)
		if self.throttle is not None:
			self.throttle.update(inverter)
		self.pv.check_power_limit()
		self.last_update = self.clock()

	def update(self):
		"""GLib timer callback, returns False to stop the timer."""
		try:
			self.poll()
		except Exception as e:
			if self.clock() - self.last_update >= self.config.fail_timeout:
				log.error('no data from the inverter for %d s (%s), exiting' % (self.config.fail_timeout, e))
				self.exit(1)
				return False
			if not self.failing:
				log.warning('update failed, retrying: %s' % e)
			self.failing = True
			return True

		if self.failing:
			log.info('update succeeded again')
			self.failing = False
		return True

	def restart(self):
		self.exit(0)

	def exit(self, code):
		self.exit_code = code
		if self.on_exit is not None:
			self.on_exit()


def parse_args(argv=None):
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument('-c', '--config', default=CONFIG_FILE, help='configuration file (default: %(default)s)')
	parser.add_argument('--host', help='IP address of the SolarEdge inverter')
	parser.add_argument('--port', type=int, help='Modbus TCP port')
	parser.add_argument('--unit', type=int, help='Modbus device id of the inverter')
	parser.add_argument('-d', '--debug', action='store_true', help='enable debug logging')
	parser.add_argument('-V', '--version', action='version', version=VERSION)
	return parser.parse_args(argv)


def settings_bus():
	return dbus.SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else dbus.SystemBus()


def main(argv=None):
	args = parse_args(argv)
	logging.basicConfig(format='%(levelname)-8s %(name)s: %(message)s')
	logging.getLogger().setLevel(logging.DEBUG if args.debug else logging.INFO)
	logging.getLogger('pymodbus').setLevel(logging.CRITICAL)  # errors are logged by the driver

	config = Config.load(args.config, {('modbus', 'host'): args.host, ('modbus', 'port'): args.port,
		('modbus', 'unit'): args.unit})
	log.info('%s v%s, connecting to %s:%d unit %d' % (services.PROCESS_NAME, VERSION, config.host, config.port,
		config.unit))

	threads_init()
	DBusGMainLoop(set_as_default=True)
	mainloop = GLib.MainLoop()

	client = ModbusTcpClient(config.host, port=config.port, timeout=config.modbus_timeout)
	if not client.connect():
		log.error('unable to connect to %s:%d' % (config.host, config.port))
		return 1

	driver = Driver(config, client, settings_bus())
	driver.on_exit = mainloop.quit
	try:
		driver.setup()
	except Exception:
		log.error('setup failed', exc_info=True)
		client.close()
		return 1

	watchdog = Watchdog(WATCHDOG_TIMEOUT)
	watchdog.start()

	def tick():
		watchdog.update()
		return driver.update()

	GLib.timeout_add(int(config.update_interval * 1000), tick)
	mainloop.run()
	client.close()
	return driver.exit_code


if __name__ == '__main__':
	sys.exit(main())
