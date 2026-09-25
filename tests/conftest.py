"""Test setup: fakes for the Venus OS only modules (dbus, GLib, velib_python) and the Modbus client.

The fakes implement the behaviour of velib_python which the driver relies on, so the unit
tests run on any machine. tests/integration runs the driver against a real D-Bus.
"""

import importlib.util
import logging
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'dbus-solaredge.py'
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))


# ---- dbus / GLib ----
class BusConnection(object):
	TYPE_SYSTEM = 'system'
	TYPE_SESSION = 'session'

	def __init__(self, address):
		self.address = address


def _install_dbus_stubs():
	dbus = types.ModuleType('dbus')
	dbus.bus = types.SimpleNamespace(BusConnection=BusConnection)
	dbus.SystemBus = lambda: 'system bus'
	dbus.SessionBus = lambda: 'session bus'
	mainloop = types.ModuleType('dbus.mainloop')
	glib = types.ModuleType('dbus.mainloop.glib')
	glib.DBusGMainLoop = lambda set_as_default=False: None
	glib.threads_init = lambda: None
	dbus.mainloop = mainloop
	mainloop.glib = glib

	gi = types.ModuleType('gi')
	repository = types.ModuleType('gi.repository')
	repository.GLib = types.SimpleNamespace(MainLoop=lambda: None, timeout_add=lambda interval, callback: None)
	gi.repository = repository

	sys.modules.update({'dbus': dbus, 'dbus.mainloop': mainloop, 'dbus.mainloop.glib': glib,
		'gi': gi, 'gi.repository': repository})


# ---- velib_python ----
class VeDbusItemExport(object):
	"""Same SetValue semantics as velib_python: unchanged values do not call the callback."""

	def __init__(self, value, writeable, onchangecallback, gettextcallback):
		self.value = value
		self.writeable = writeable
		self.onchangecallback = onchangecallback
		self.gettextcallback = gettextcallback

	def is_equal(self, newvalue):
		return newvalue == self.value

	def SetValue(self, path, newvalue):
		if not self.writeable:
			return 1
		if self.is_equal(newvalue):
			return 0
		if self.onchangecallback is None or self.onchangecallback(path, newvalue):
			self.value = newvalue
			return 0
		return 2

	def GetText(self, path):
		if self.value is None:
			return '---'
		if self.gettextcallback is None:
			return str(self.value)
		return self.gettextcallback(path, self.value)


class VeDbusService(object):
	"""velib_python VeDbusService with deferred registration (register=False)."""

	def __init__(self, servicename, bus=None, register=None):
		self.name = servicename
		self.bus = bus
		self.items = {}
		self.registered = register is None or register
		self.items_changed = 0

	def register(self):
		assert not self.registered, 'registered twice'
		self.registered = True

	def add_path(self, path, value, description='', writeable=False, onchangecallback=None,
			gettextcallback=None, valuetype=None, itemtype=None):
		assert path not in self.items, 'duplicate path %s' % path
		itemtype = itemtype or VeDbusItemExport
		self.items[path] = itemtype(value, writeable, onchangecallback, gettextcallback)

	def add_mandatory_paths(self, processname, processversion, connection, deviceinstance, productid,
			productname, firmwareversion, hardwareversion, connected):
		for path, value in (('/Mgmt/ProcessName', processname), ('/Mgmt/ProcessVersion', processversion),
				('/Mgmt/Connection', connection), ('/DeviceInstance', deviceinstance), ('/ProductId', productid),
				('/ProductName', productname), ('/FirmwareVersion', firmwareversion),
				('/HardwareVersion', hardwareversion), ('/Connected', connected)):
			self.add_path(path, value)

	def __getitem__(self, path):
		return self.items[path].value

	def __setitem__(self, path, value):
		self.items[path].value = value

	def __contains__(self, path):
		return path in self.items

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		self.items_changed += 1

	# helpers for the tests, emulate calls from other processes
	def set_value(self, path, value):
		return self.items[path].SetValue(path, value)

	def text(self, path):
		return self.items[path].GetText(path)


class LegacyVeDbusService(VeDbusService):
	"""velib_python of older Venus OS versions: no deferred registration, no itemtype."""

	def __init__(self, servicename, bus=None):
		VeDbusService.__init__(self, servicename, bus)

	def add_path(self, path, value, description='', writeable=False, onchangecallback=None,
			gettextcallback=None):
		VeDbusService.add_path(self, path, value, description, writeable, onchangecallback, gettextcallback)


class SettingsDevice(object):
	"""localsettings: values survive in `stored` like in /data/conf/settings.xml."""

	stored = {}

	def __init__(self, bus, supportedSettings, eventCallback, name='com.victronenergy.settings', timeout=0):
		self.bus = bus
		self.definitions = supportedSettings
		self.callback = eventCallback
		for setting, options in supportedSettings.items():
			self.stored.setdefault(options[0], options[1])

	def __getitem__(self, setting):
		return self.stored[self.definitions[setting][0]]

	def __setitem__(self, setting, value):
		self.change(setting, value)

	def change(self, setting, value):
		"""A setting changed, by us or e.g. the GUI."""
		path = self.definitions[setting][0]
		old = self.stored[path]
		self.stored[path] = value
		if old != value:
			self.callback(setting, old, value)


def _install_velib_stubs():
	vedbus = types.ModuleType('vedbus')
	vedbus.VeDbusService = VeDbusService
	vedbus.VeDbusItemExport = VeDbusItemExport
	settingsdevice = types.ModuleType('settingsdevice')
	settingsdevice.SettingsDevice = SettingsDevice
	sys.modules.update({'vedbus': vedbus, 'settingsdevice': settingsdevice})


_install_dbus_stubs()
_install_velib_stubs()


def load_script():
	spec = importlib.util.spec_from_file_location('dbus_solaredge', str(SCRIPT))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


import services  # noqa: E402
import solaredge  # noqa: E402
import sunspec  # noqa: E402

driver_module = load_script()


# ---- fake Modbus ----
from fake_inverter import UNIT, register_map  # noqa: E402


class Response(object):
	def __init__(self, registers=None, error=False):
		self.registers = registers
		self.error = error

	def isError(self):
		return self.error

	def __str__(self):
		return 'Response(error=%s)' % self.error


class FakeModbusClient(object):
	"""Register map of a SolarEdge SE10K (three phase) with a SolarEdge meter."""

	UNIT = UNIT

	def __init__(self, phases=3):
		self.registers = register_map(phases)
		self.writes = []
		self.read_error = False
		self.write_error = False
		self.connect_result = True
		self.closed = False

	def load(self, address, registers):
		for offset, value in enumerate(registers):
			self.registers[address + offset] = value

	def value(self, address, kind):
		count = solaredge.REGISTER_COUNT[kind]
		return solaredge.ModbusDevice.decode(kind, [self.registers[address + i] for i in range(count)])

	def written(self, address, kind):
		"""Values written to one register, decoded."""
		return [solaredge.ModbusDevice.decode(kind, values) for a, values in self.writes if a == address]

	def connect(self):
		return self.connect_result

	def close(self):
		self.closed = True

	def read_holding_registers(self, address, count, unit):
		assert unit == self.UNIT
		if self.read_error:
			return Response(error=True)
		return Response([self.registers.get(address + i, 0) for i in range(count)])

	def write_registers(self, address, values, unit):
		assert unit == self.UNIT
		if self.write_error:
			return Response(error=True)
		self.writes.append((address, list(values)))
		self.load(address, values)
		return Response()


class Clock(object):
	def __init__(self, now=1000.0):
		self.now = now

	def __call__(self):
		return self.now


INVERTER_INFO = sunspec.CommonBlock('SolarEdge', 'SE10K', '', '0004.0018.0032', '7E123456')
METER_INFO = sunspec.CommonBlock('SolarEdge', 'SE-WND-3Y400-MB-K2', 'Export+Import', '2.3', 'M1234')


@pytest.fixture(autouse=True)
def clean_settings():
	SettingsDevice.stored = {}
	yield


@pytest.fixture(autouse=True)
def restore_root_logger():
	root = logging.getLogger()
	handlers, level = list(root.handlers), root.level
	yield
	root.handlers[:] = handlers
	root.setLevel(level)


@pytest.fixture
def legacy_velib(monkeypatch):
	monkeypatch.setattr(services, 'VeDbusService', LegacyVeDbusService)


@pytest.fixture
def client():
	return FakeModbusClient()


@pytest.fixture
def modbus(client):
	return solaredge.ModbusDevice(client, FakeModbusClient.UNIT)


@pytest.fixture
def clock():
	return Clock()
