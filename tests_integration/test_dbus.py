"""End-to-end test: the driver on a real D-Bus with Victron's velib_python, a Modbus TCP server
simulating the inverter and a minimal localsettings service.

Requires dbus-python, PyGObject, dbus-daemon and a checkout of velib_python in $VELIB_PYTHON:
	git clone https://github.com/victronenergy/velib_python /tmp/velib_python
	VELIB_PYTHON=/tmp/velib_python pytest tests_integration
"""

import os
import pathlib
import subprocess
import sys
import threading
import time

import pytest

dbus = pytest.importorskip('dbus')
pytest.importorskip('gi')

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import fake_inverter  # noqa: E402
import solaredge  # noqa: E402
from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext, ModbusSparseDataBlock  # noqa: E402
from pymodbus.server.sync import ModbusTcpServer  # noqa: E402

VELIB = os.environ.get('VELIB_PYTHON')
if not VELIB or not os.path.isfile(os.path.join(VELIB, 'vedbus.py')):
	pytest.skip('VELIB_PYTHON does not point to a velib_python checkout', allow_module_level=True)

BUSITEM = 'com.victronenergy.BusItem'
PV = 'com.victronenergy.pvinverter.solaredge_7E123456'
GRID = 'com.victronenergy.grid.solaredge_meter_M1234'
TEMPERATURE = 'com.victronenergy.temperature.solaredge_7E123456_temperature'


def wait_for(condition, timeout=15.0, interval=0.1):
	end = time.time() + timeout
	while time.time() < end:
		result = condition()
		if result:
			return result
		time.sleep(interval)
	raise AssertionError('timeout waiting for condition')


@pytest.fixture
def bus_address():
	daemon = subprocess.Popen(['dbus-daemon', '--session', '--nofork', '--print-address'],
		stdout=subprocess.PIPE, text=True)
	address = daemon.stdout.readline().strip()
	yield address
	daemon.terminate()
	daemon.wait()


@pytest.fixture
def bus(bus_address, monkeypatch):
	monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', bus_address)
	connection = dbus.bus.BusConnection(bus_address)
	yield connection
	connection.close()


@pytest.fixture
def localsettings(bus):
	process = subprocess.Popen([sys.executable, str(ROOT / 'tests_integration' / 'fake_localsettings.py')])
	wait_for(lambda: 'com.victronenergy.settings' in bus.list_names())
	yield
	process.terminate()
	process.wait()


class Inverter(object):
	"""Modbus TCP server with the register map of tests/fake_inverter.py."""

	def __init__(self):
		self.block = ModbusSparseDataBlock(fake_inverter.register_map())
		store = ModbusSlaveContext(hr=self.block, zero_mode=True)
		context = ModbusServerContext(slaves={fake_inverter.UNIT: store}, single=False)
		self.server = ModbusTcpServer(context, address=('127.0.0.1', 0))
		self.port = self.server.socket.getsockname()[1]
		self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
		self.thread.start()

	def value(self, address, kind):
		count = solaredge.REGISTER_COUNT[kind]
		return solaredge.ModbusDevice.decode(kind, self.block.getValues(address, count))

	def stop(self):
		self.server.shutdown()
		self.server.server_close()


@pytest.fixture
def inverter():
	inverter = Inverter()
	yield inverter
	inverter.stop()


@pytest.fixture
def driver(bus, localsettings, inverter, tmp_path):
	config = tmp_path / 'config.ini'
	config.write_text('[modbus]\nhost = 127.0.0.1\nport = %d\ntimeout = 0.5\n'
		'[inverter]\npower_limit_timeout = 30\n'
		'[driver]\nupdate_interval = 0.2\nfail_timeout = 2\n' % inverter.port)
	env = dict(os.environ, PYTHONPATH=VELIB)
	process = subprocess.Popen([sys.executable, str(ROOT / 'dbus-solaredge.py'), '-c', str(config)], env=env)
	wait_for(lambda: {PV, GRID, TEMPERATURE} <= set(bus.list_names()) or process.poll() is not None)
	assert process.poll() is None, 'driver exited with %s' % process.returncode
	yield process
	if process.poll() is None:
		process.terminate()
		process.wait()


def get(bus, service, path):
	return bus.call_blocking(service, path, BUSITEM, 'GetValue', '', [])


def set_value(bus, service, path, value):
	return bus.call_blocking(service, path, BUSITEM, 'SetValue', 'v', [value])


def test_services_and_values(bus, driver):
	assert get(bus, GRID, '/Ac/Power') == 3000
	assert get(bus, GRID, '/Ac/L1/Current') == -5.0
	assert get(bus, GRID, '/DeviceInstance') == 0
	assert get(bus, PV, '/Ac/Power') == 9000
	assert get(bus, PV, '/DeviceInstance') == 20
	assert get(bus, PV, '/ProductId') == 0xA146
	assert get(bus, PV, '/ProductName') == 'SolarEdge SE10K'
	assert get(bus, PV, '/Serial') == '7E123456'
	assert get(bus, PV, '/StatusCode') == 11
	assert get(bus, PV, '/Ac/MaxPower') == 10000.0
	assert get(bus, TEMPERATURE, '/Temperature') == 45.2
	assert get(bus, TEMPERATURE, '/TemperatureType') == 2
	# the whole tree via the root object, as used by the GUI and dbus-flashmq
	items = bus.call_blocking(PV, '/', BUSITEM, 'GetItems', '', [])
	assert items['/Ac/Power']['Value'] == 9000
	assert items['/Ac/Power']['Text'] == '9000W'


def test_values_are_updated(bus, driver, inverter):
	inverter.block.setValues(40083, [4200])
	wait_for(lambda: get(bus, PV, '/Ac/Power') == 4200)


def test_device_instances_from_localsettings(bus, driver):
	value = get(bus, 'com.victronenergy.settings', '/Settings/Devices/solaredge_7E123456/ClassAndVrmInstance')
	assert value == 'pvinverter:20'


def test_ess_power_limit(bus, driver, inverter):
	assert inverter.value(solaredge.REG_ENABLE_DYNAMIC_POWER_CONTROL, '16bit_uint') == 1
	assert inverter.value(solaredge.REG_COMMAND_TIMEOUT, '32bit_uint') == 30
	assert set_value(bus, PV, '/Ac/PowerLimit', 2500) == 0
	assert inverter.value(solaredge.REG_DYNAMIC_ACTIVE_POWER_LIMIT, '32bit_float') == 25.0
	assert get(bus, PV, '/Ac/PowerLimit') == 2500


def test_power_limit_expires(bus, driver, inverter):
	set_value(bus, PV, '/Ac/PowerLimit', 2500)
	# timeout 30 s: the limit is removed after 15 s without refresh
	wait_for(lambda: get(bus, PV, '/Ac/PowerLimit') == 10000.0, timeout=20)
	assert inverter.value(solaredge.REG_DYNAMIC_ACTIVE_POWER_LIMIT, '32bit_float') == 100.0


def test_custom_name_is_stored(bus, driver):
	assert set_value(bus, GRID, '/CustomName', 'Hausanschluss') == 0
	settings_path = '/Settings/Devices/solaredge_meter_M1234/CustomName'
	wait_for(lambda: get(bus, 'com.victronenergy.settings', settings_path) == 'Hausanschluss')


def test_exits_when_inverter_is_gone(driver, inverter):
	inverter.stop()
	assert driver.wait(timeout=15) == 1


def test_exits_on_device_instance_change(bus, driver):
	set_value(bus, 'com.victronenergy.settings', '/Settings/Devices/solaredge_7E123456/ClassAndVrmInstance',
		'pvinverter:21')
	assert driver.wait(timeout=10) == 0


def test_check(inverter, tmp_path):
	"""The connection test used by setup.sh."""
	config = tmp_path / 'config.ini'
	config.write_text('[modbus]\nhost = 127.0.0.1\nport = %d\n' % inverter.port)
	result = subprocess.run([sys.executable, str(ROOT / 'dbus-solaredge.py'), '-c', str(config), '--check'],
		env=dict(os.environ, PYTHONPATH=VELIB), capture_output=True, text=True, timeout=30)
	assert result.returncode == 0, result.stdout + result.stderr
	assert 'inverter: SolarEdge SE10K, serial 7E123456' in result.stdout
	assert 'meter:    SolarEdge SE-WND-3Y400-MB-K2, serial M1234' in result.stdout


def test_check_unreachable(tmp_path):
	config = tmp_path / 'config.ini'
	config.write_text('[modbus]\nhost = 127.0.0.1\nport = 1\ntimeout = 0.5\n')
	result = subprocess.run([sys.executable, str(ROOT / 'dbus-solaredge.py'), '-c', str(config), '--check'],
		env=dict(os.environ, PYTHONPATH=VELIB), capture_output=True, text=True, timeout=30)
	assert result.returncode == 1
	assert 'unable to connect to 127.0.0.1:1' in result.stderr
