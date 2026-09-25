import logging
import runpy
import sys
import types

import pytest

import solaredge
from conftest import SCRIPT, Clock, FakeModbusClient, Response, driver_module as dm


def write_config(tmp_path, text):
	path = tmp_path / 'config.ini'
	path.write_text(text)
	return str(path)


def make_config(**changes):
	config = dm.Config.load(None)
	for name, value in changes.items():
		setattr(config, name, value)
	return config


# ---- Config ----
def test_config_defaults(tmp_path):
	config = dm.Config.load(str(tmp_path / 'missing.ini'))
	assert (config.host, config.port, config.unit, config.modbus_timeout) == ('192.168.178.80', 502, 126, 2.0)
	assert (config.max_power, config.power_limit, config.power_limit_timeout) == (0, True, 120)
	assert config.throttle_input is False
	assert (config.update_interval, config.fail_timeout) == (1.0, 10)


def test_config_file_and_overrides(tmp_path):
	path = write_config(tmp_path, '[modbus]\nhost = 10.0.0.5\nunit = 1\n[inverter]\npower_limit = no\n'
		'max_power = 25000\nthrottle_input = yes\n')
	config = dm.Config.load(path, {('modbus', 'host'): '10.0.0.6', ('modbus', 'port'): None})
	assert config.host == '10.0.0.6'
	assert config.port == 502
	assert config.unit == 1
	assert config.power_limit is False
	assert config.max_power == 25000
	assert config.throttle_input is True


def test_config_sample_matches_defaults():
	config = dm.Config.load(str(SCRIPT.parent / 'config.sample.ini'))
	assert vars(config) == vars(dm.Config.load(None))


@pytest.mark.parametrize('timeout', [29, 601])
def test_config_invalid_power_limit_timeout(tmp_path, timeout):
	with pytest.raises(ValueError):
		dm.Config.load(write_config(tmp_path, '[inverter]\npower_limit_timeout = %d\n' % timeout))


def test_config_invalid_value(tmp_path):
	with pytest.raises(ValueError):
		dm.Config.load(write_config(tmp_path, '[modbus]\nport = abc\n'))


# ---- Watchdog ----
def test_watchdog(monkeypatch):
	clock = Clock()
	exits = []
	monkeypatch.setattr(dm.faulthandler, 'dump_traceback', lambda: None)
	watchdog = dm.Watchdog(60, clock, exits.append)
	clock.now += 60
	assert watchdog.check()
	watchdog.update()
	clock.now += 61
	assert not watchdog.check()
	assert exits == [1]


def test_watchdog_run(monkeypatch):
	clock = Clock()
	exits = []
	monkeypatch.setattr(dm.faulthandler, 'dump_traceback', lambda: None)
	watchdog = dm.Watchdog(60, clock, exits.append)
	sleeps = []

	def sleep(seconds):
		sleeps.append(seconds)
		clock.now += seconds

	watchdog.run(sleep)
	assert sleeps == [15.0] * 5
	assert exits == [1]


def test_watchdog_start(monkeypatch):
	started = []

	class Thread(object):
		def __init__(self, target, name):
			self.target = target
			self.daemon = False

		def start(self):
			started.append(self)

	monkeypatch.setattr(dm.threading, 'Thread', Thread)
	watchdog = dm.Watchdog(60)
	watchdog.start()
	assert started[0].daemon
	assert started[0].target == watchdog.run


# ---- Driver ----
@pytest.fixture
def client():
	return FakeModbusClient()


@pytest.fixture
def driver(client):
	driver = dm.Driver(make_config(), client, 'settings bus', Clock())
	driver.setup()
	return driver


def test_setup(driver):
	assert [d.service.name for d in driver.devices] == [
		'com.victronenergy.grid.solaredge_meter_M1234',
		'com.victronenergy.pvinverter.solaredge_7E123456',
		'com.victronenergy.temperature.solaredge_7E123456_temperature']
	assert all(d.service.registered for d in driver.devices)
	# values are published before the services are registered
	assert driver.grid.service['/Ac/Power'] == 3000
	assert driver.pv.service['/Ac/Power'] == 9000
	assert driver.temperature.service['/Temperature'] == 45.2
	assert driver.pv.service['/Mgmt/Connection'] == 'Modbus TCP 192.168.178.80:502 unit 126'
	assert driver.pv.service['/Mgmt/ProcessVersion'] == dm.VERSION
	assert driver.last_update == 1000.0


def test_setup_initializes_power_limiter(driver, client):
	assert driver.pv.limiter.max_power == 10000.0
	assert driver.pv.service['/Ac/PowerLimit'] == 10000.0
	assert client.value(solaredge.REG_ENABLE_DYNAMIC_POWER_CONTROL, '16bit_uint') == 1


def test_setup_power_limit_disabled(client, caplog):
	caplog.set_level(logging.INFO)
	driver = dm.Driver(make_config(power_limit=False), client, 'bus', Clock())
	driver.setup()
	assert '/Ac/PowerLimit' not in driver.pv.service
	assert client.writes == []
	assert 'power limiting disabled in the configuration' in caplog.text


def test_setup_max_power_unknown(client, caplog):
	client.load(solaredge.REG_MAX_ACTIVE_POWER, [0, 0])
	driver = dm.Driver(make_config(), client, 'bus', Clock())
	driver.setup()
	assert '/Ac/PowerLimit' not in driver.pv.service
	assert driver.pv.service['/Ac/MaxPower'] is None
	assert 'power limiting disabled' in caplog.text


def test_setup_fixed_max_power(client):
	client.load(solaredge.REG_MAX_ACTIVE_POWER, [0xFFFF, 0xFFFF])  # garbage, must not be used
	driver = dm.Driver(make_config(max_power=25000.0), client, 'bus', Clock())
	driver.setup()
	assert driver.pv.service['/Ac/MaxPower'] == 25000.0


def test_setup_throttle_input(client):
	driver = dm.Driver(make_config(throttle_input=True), client, 'bus', Clock())
	driver.setup()
	assert driver.devices[-1] is driver.throttle
	assert driver.throttle.service['/State'] == 2
	assert driver.throttle.service.registered


def test_setup_without_serial(client):
	client.load(solaredge.INVERTER_COMMON + 48, [0] * 16)
	client.load(solaredge.METER_COMMON + 48, [0] * 16)
	driver = dm.Driver(make_config(), client, 'bus', Clock())
	driver.setup()
	assert driver.pv.service.name == 'com.victronenergy.pvinverter.solaredge_unit126'
	assert driver.grid.service.name == 'com.victronenergy.grid.solaredge_meter'


def test_setup_fails_on_modbus_error(client):
	client.read_error = True
	driver = dm.Driver(make_config(), client, 'bus', Clock())
	with pytest.raises(solaredge.ModbusError):
		driver.setup()


def test_update(driver, client):
	client.load(40083, [5000])
	driver.clock.now += 1
	assert driver.update() is True
	assert driver.pv.service['/Ac/Power'] == 5000
	assert driver.last_update == 1001.0


def test_update_retries_until_fail_timeout(driver, client, caplog):
	exits = []
	driver.on_exit = lambda: exits.append(driver.exit_code)
	client.read_error = True
	for _ in range(9):
		driver.clock.now += 1
		assert driver.update() is True
	assert caplog.text.count('update failed, retrying') == 1
	driver.clock.now += 1
	assert driver.update() is False
	assert exits == [1]
	assert 'no data from the inverter for 10 s' in caplog.text


def test_update_recovers(driver, client, caplog):
	caplog.set_level(logging.INFO)
	client.read_error = True
	driver.clock.now += 5
	driver.update()
	client.read_error = False
	driver.clock.now += 1
	assert driver.update() is True
	assert not driver.failing
	assert 'update succeeded again' in caplog.text
	# the timeout starts again after a successful update
	client.read_error = True
	driver.clock.now += 9
	assert driver.update() is True


def test_update_expires_power_limit(driver, client):
	driver.pv.service.set_value('/Ac/PowerLimit', 2000)
	assert client.value(solaredge.REG_DYNAMIC_ACTIVE_POWER_LIMIT, '32bit_float') == 20.0
	driver.clock.now += 59
	driver.update()
	assert driver.pv.service['/Ac/PowerLimit'] == 2000
	driver.clock.now += 1
	driver.update()
	assert driver.pv.service['/Ac/PowerLimit'] == 10000.0
	assert client.value(solaredge.REG_DYNAMIC_ACTIVE_POWER_LIMIT, '32bit_float') == 100.0


def test_instance_change_restarts_driver(driver):
	exits = []
	driver.on_exit = lambda: exits.append(driver.exit_code)
	driver.pv.settings.change('instance', 'pvinverter:21')
	assert exits == [0]


def test_exit_without_callback(driver):
	driver.exit(1)
	assert driver.exit_code == 1


# ---- main ----
def test_parse_args():
	args = dm.parse_args(['--host', '10.0.0.5', '--port', '1502', '--unit', '1', '-d', '-c', 'x.ini'])
	assert (args.host, args.port, args.unit, args.debug, args.config) == ('10.0.0.5', 1502, 1, True, 'x.ini')
	assert dm.parse_args([]).config == dm.CONFIG_FILE


def test_version(capsys):
	with pytest.raises(SystemExit):
		dm.parse_args(['--version'])
	assert capsys.readouterr().out.strip() == dm.VERSION


def test_settings_bus(monkeypatch):
	monkeypatch.delenv('DBUS_SESSION_BUS_ADDRESS', raising=False)
	assert dm.settings_bus() == 'system bus'
	monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/tmp/bus')
	assert dm.settings_bus() == 'session bus'


class FakeGLib(object):
	"""Runs the timer until it returns False or the main loop is quit."""

	def __init__(self, max_ticks=100):
		self.timers = []
		self.max_ticks = max_ticks
		self.ticks = 0

	def MainLoop(self):
		glib = self

		class Loop(object):
			running = False

			def run(self):
				self.running = True
				interval, callback = glib.timers[0]
				while self.running and glib.ticks < glib.max_ticks and callback():
					glib.ticks += 1

			def quit(self):
				self.running = False
		return Loop()

	def timeout_add(self, interval, callback):
		self.timers.append((interval, callback))


@pytest.fixture
def run_main(monkeypatch, tmp_path):
	"""Run main() with fake GLib, watchdog and Modbus client."""
	clients = []
	glib = FakeGLib()
	watchdogs = []

	def client_factory(host, port, timeout):
		client = FakeModbusClient()
		client.args = (host, port, timeout)
		clients.append(client)
		return client

	class Watchdog(object):
		def __init__(self, timeout):
			self.updates = 0
			watchdogs.append(self)

		def start(self):
			pass

		def update(self):
			self.updates += 1

	monkeypatch.setattr(dm, 'ModbusTcpClient', client_factory)
	monkeypatch.setattr(dm, 'GLib', glib)
	monkeypatch.setattr(dm, 'Watchdog', Watchdog)

	def run(*argv):
		code = dm.main(['-c', str(tmp_path / 'config.ini')] + list(argv))
		return code, clients, glib, watchdogs
	return run


def test_main_runs_until_inverter_fails(run_main, monkeypatch):
	ticks = []
	original = dm.Driver.update

	def update(self):
		ticks.append(1)
		if len(ticks) == 3:
			self.modbus.client.read_error = True
			self.clock = lambda: 1e9  # fail timeout reached
		return original(self)

	monkeypatch.setattr(dm.Driver, 'update', update)
	code, clients, glib, watchdogs = run_main('--host', '10.0.0.5', '--unit', '126')
	assert code == 1
	assert clients[0].args == ('10.0.0.5', 502, 2.0)
	assert clients[0].closed
	assert glib.timers[0][0] == 1000
	assert len(ticks) == 3
	assert watchdogs[0].updates == 3


def test_main_exits_on_instance_change(run_main, monkeypatch):
	def update(self):
		self.pv.settings.change('instance', 'pvinverter:30')
		return True

	monkeypatch.setattr(dm.Driver, 'update', update)
	code, clients, glib, watchdogs = run_main()
	assert code == 0


def test_main_connect_failure(run_main, monkeypatch, caplog):
	monkeypatch.setattr(FakeModbusClient, 'connect', lambda self: False)
	code, clients, glib, watchdogs = run_main()
	assert code == 1
	assert 'unable to connect to 192.168.178.80:502' in caplog.text
	assert glib.timers == []


def test_main_setup_failure(run_main, monkeypatch, caplog):
	monkeypatch.setattr(FakeModbusClient, 'read_holding_registers',
		lambda self, address, count, unit: Response(error=True))
	code, clients, glib, watchdogs = run_main()
	assert code == 1
	assert 'setup failed' in caplog.text
	assert clients[0].closed


def test_main_debug_logging(run_main, monkeypatch):
	monkeypatch.setattr(dm.Driver, 'update', lambda self: False)
	run_main('-d')
	assert logging.getLogger().level == logging.DEBUG
	assert logging.getLogger('pymodbus').level == logging.CRITICAL


def test_script_entry_point(monkeypatch, tmp_path):
	client = FakeModbusClient()
	client.connect_result = False
	sync = types.ModuleType('pymodbus.client.sync')
	sync.ModbusTcpClient = lambda *args, **kwargs: client
	monkeypatch.setitem(sys.modules, 'pymodbus.client.sync', sync)
	monkeypatch.setattr(sys, 'argv', ['dbus-solaredge.py', '-c', str(tmp_path / 'none.ini')])
	with pytest.raises(SystemExit) as exit_info:
		runpy.run_path(str(SCRIPT), run_name='__main__')
	assert exit_info.value.code == 1


# ---- --check ----
def test_check(run_main, capsys):
	code, clients, glib, watchdogs = run_main('--check')
	assert code == 0
	out = capsys.readouterr().out
	assert ('inverter: SolarEdge SE10K, serial 7E123456, firmware 0004.0018.0032, 3 phase(s), '
		'max power 10000.0 W') in out
	assert 'meter:    SolarEdge SE-WND-3Y400-MB-K2, serial M1234' in out
	assert clients[0].closed
	assert glib.timers == []


def test_check_error(run_main, monkeypatch, capsys):
	monkeypatch.setattr(FakeModbusClient, 'read_holding_registers',
		lambda self, address, count, unit: Response(error=True))
	code, clients, glib, watchdogs = run_main('--check')
	assert code == 1
	assert 'error: reading 64 registers at 0x9C44 failed' in capsys.readouterr().out
