import pytest

import services
import solaredge
from conftest import INVERTER_INFO, METER_INFO, LegacyVeDbusService, SettingsDevice


class Restart(object):
	def __init__(self):
		self.count = 0

	def __call__(self):
		self.count += 1


@pytest.fixture
def restart():
	return Restart()


def common(restart):
	return dict(connection='Modbus TCP 1.2.3.4:502 unit 126', version='1.0.0', settings_bus='bus',
		on_restart=restart)


def inverter_reading(power=9000, status=solaredge.STATUS_MPPT, phases=3, temperature=45.2):
	phase = solaredge.PhaseReading(10.0, 230.0, power and power / phases, 100.0 / phases, None)
	return solaredge.InverterReading(power, 100.0, [phase] * phases, status, 0, temperature)


class FakeLimiter(object):
	def __init__(self):
		self.limits = []
		self.error = False
		self.is_expired = False
		self.resets = 0

	def set_limit(self, watts):
		if self.error:
			raise solaredge.ModbusError('write failed')
		self.limits.append(watts)
		return watts

	def expired(self):
		return self.is_expired

	def reset(self):
		self.resets += 1
		self.is_expired = False
		return 10000.0


class FakeInverter(object):
	def __init__(self):
		self.calls = []
		self.error = False

	def write_advanced_power_control(self, enabled):
		self._call('adv', enabled)

	def write_active_power_limit(self, percent):
		self._call('limit', percent)

	def _call(self, name, value):
		if self.error:
			raise solaredge.ModbusError('write failed')
		self.calls.append((name, value))


def make_pv(restart, limiter=None, max_power=10000.0, inverter=None):
	return services.PvInverter('solaredge_7E123456', INVERTER_INFO, inverter=inverter or FakeInverter(),
		max_power=max_power, limiter=limiter, **common(restart))


# ---- helpers ----
def test_make_ident():
	assert services.make_ident('solaredge', '7E-12.3 4') == 'solaredge_7E_12_3_4'
	assert services.make_ident('solaredge', 'meter', '') == 'solaredge_meter'


def test_private_bus(monkeypatch):
	monkeypatch.delenv('DBUS_SESSION_BUS_ADDRESS', raising=False)
	assert services.private_bus().address == 'system'
	monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/tmp/bus')
	assert services.private_bus().address == 'unix:path=/tmp/bus'


def test_text():
	assert services._w('/p', 12.345) == '12.35W'
	assert services._kwh('/p', 1.23456) == '1.235kWh'
	assert services._c('/p', -5.24) == '-5.2C'
	assert services._pct('/p', 80) == '80%'


def test_refreshable_item_calls_callback_for_same_value():
	calls = []
	item = services.RefreshableItem(5, True, lambda p, v: calls.append(v) or True, None)
	assert item.SetValue('/p', 5) == 0
	assert calls == [5]


@pytest.mark.parametrize('status, code', [
	(solaredge.STATUS_OFF, 0), (solaredge.STATUS_SLEEPING, 8), (solaredge.STATUS_STARTING, 3),
	(solaredge.STATUS_MPPT, 11), (solaredge.STATUS_THROTTLED, 12), (solaredge.STATUS_SHUTTING_DOWN, 8),
	(solaredge.STATUS_FAULT, 10), (solaredge.STATUS_STANDBY, 8), (99, None),
])
def test_status_code(restart, status, code):
	pv = make_pv(restart)
	pv.update(services.PvInverterReading(inverter_reading(status=status), 1, 100))
	assert pv.service['/StatusCode'] == code


# ---- DbusDevice ----
def test_mandatory_paths(restart):
	grid = services.GridMeter('solaredge_meter_M1234', METER_INFO, **common(restart))
	s = grid.service
	assert s.name == 'com.victronenergy.grid.solaredge_meter_M1234'
	assert s['/Mgmt/ProcessName'] == 'dbus-solaredge'
	assert s['/Mgmt/ProcessVersion'] == '1.0.0'
	assert s['/Mgmt/Connection'] == 'Modbus TCP 1.2.3.4:502 unit 126'
	assert s['/DeviceInstance'] == 0
	assert s['/ProductId'] == services.PRODUCT_ID_UNKNOWN
	assert s['/ProductName'] == 'SolarEdge SE-WND-3Y400-MB-K2'
	assert s['/FirmwareVersion'] == '2.3'
	assert s['/HardwareVersion'] is None
	assert s['/Serial'] == 'M1234'
	assert s['/Connected'] == 1
	assert s['/CustomName'] == ''


def test_registered_only_after_register(restart):
	grid = services.GridMeter('meter', METER_INFO, **common(restart))
	assert not grid.service.registered
	grid.register()
	assert grid.service.registered


def test_legacy_velib_registers_immediately(restart, legacy_velib):
	grid = services.GridMeter('meter', METER_INFO, **common(restart))
	assert isinstance(grid.service, LegacyVeDbusService)
	assert grid.service.registered
	grid.register()  # nothing left to do


def test_private_bus_per_service(restart):
	grid = services.GridMeter('meter', METER_INFO, **common(restart))
	pv = make_pv(restart)
	assert grid.service.bus is not pv.service.bus


def test_device_instance_from_localsettings(restart):
	SettingsDevice.stored['/Settings/Devices/meter/ClassAndVrmInstance'] = 'grid:31'
	grid = services.GridMeter('meter', METER_INFO, **common(restart))
	assert grid.instance == 31
	assert grid.service['/DeviceInstance'] == 31


def test_default_device_instances(restart):
	grid = services.GridMeter('meter', METER_INFO, **common(restart))
	pv = make_pv(restart)
	temp = services.InverterTemperature('temp', INVERTER_INFO, **common(restart))
	throttle = services.ThrottleInput('throttle', INVERTER_INFO, **common(restart))
	stored = SettingsDevice.stored
	assert stored['/Settings/Devices/meter/ClassAndVrmInstance'] == 'grid:0'
	assert stored['/Settings/Devices/solaredge_7E123456/ClassAndVrmInstance'] == 'pvinverter:20'
	assert stored['/Settings/Devices/temp/ClassAndVrmInstance'] == 'temperature:26'
	assert stored['/Settings/Devices/throttle/ClassAndVrmInstance'] == 'digitalinput:10'
	assert [d.instance for d in (grid, pv, temp, throttle)] == [0, 20, 26, 10]


@pytest.mark.parametrize('value', ['garbage', 'grid:x', None])
def test_invalid_device_instance_is_reset(restart, value):
	SettingsDevice.stored['/Settings/Devices/meter/ClassAndVrmInstance'] = value
	grid = services.GridMeter('meter', METER_INFO, **common(restart))
	assert grid.instance == 0
	assert SettingsDevice.stored['/Settings/Devices/meter/ClassAndVrmInstance'] == 'grid:0'
	assert restart.count == 0


def test_instance_change_restarts(restart):
	grid = services.GridMeter('meter', METER_INFO, **common(restart))
	grid.settings.change('instance', 'grid:0')
	assert restart.count == 0
	grid.settings.change('instance', 'grid:5')
	assert restart.count == 1
	grid.settings.change('instance', 'invalid')
	assert restart.count == 2


def test_custom_name_is_stored(restart):
	grid = services.GridMeter('meter', METER_INFO, **common(restart))
	assert grid.service.set_value('/CustomName', 'Hausanschluss') == 0
	assert SettingsDevice.stored['/Settings/Devices/meter/CustomName'] == 'Hausanschluss'
	assert grid.service.set_value('/CustomName', 5) == 2


def test_setting_changed_by_gui_updates_path(restart):
	grid = services.GridMeter('meter', METER_INFO, **common(restart))
	grid.settings.change('customname', 'from gui')
	assert grid.service['/CustomName'] == 'from gui'


def test_unknown_setting_change_is_ignored(restart):
	grid = services.GridMeter('meter', METER_INFO, **common(restart))
	grid._setting_changed('other', 1, 2)


def test_publish_is_abstract(restart):
	device = services.DbusDevice.__new__(services.DbusDevice)
	with pytest.raises(NotImplementedError):
		device.publish(None, None)


def test_update_sends_one_items_changed(restart):
	grid = services.GridMeter('meter', METER_INFO, **common(restart))
	grid.update(_meter_reading())
	assert grid.service.items_changed == 1


def _meter_reading():
	phase = solaredge.PhaseReading(-5.0, 230.0, 1500, 4.0, 1.0)
	return solaredge.MeterReading(3000, 65.536, 6.0, [phase, phase, phase])


# ---- GridMeter ----
def test_grid_meter(restart):
	grid = services.GridMeter('meter', METER_INFO, **common(restart))
	grid.update(_meter_reading())
	s = grid.service
	assert s['/Ac/Power'] == 3000
	assert s['/Ac/Energy/Forward'] == 65.536
	assert s['/Ac/Energy/Reverse'] == 6.0
	for phase in ('L1', 'L2', 'L3'):
		assert s['/Ac/%s/Current' % phase] == -5.0
		assert s['/Ac/%s/Voltage' % phase] == 230.0
		assert s['/Ac/%s/Power' % phase] == 1500
		assert s['/Ac/%s/Energy/Forward' % phase] == 4.0
		assert s['/Ac/%s/Energy/Reverse' % phase] == 1.0
	assert s['/ErrorCode'] == 0
	assert s.text('/Ac/Power') == '3000W'


# ---- PvInverter ----
def test_pv_inverter_paths(restart):
	pv = make_pv(restart, limiter=FakeLimiter())
	pv.update(services.PvInverterReading(inverter_reading(), 1, 80))
	s = pv.service
	assert s.name == 'com.victronenergy.pvinverter.solaredge_7E123456'
	assert s['/ProductId'] == 0xA146
	assert s['/Ac/Power'] == 9000
	assert s['/Ac/Energy/Forward'] == 100.0
	assert [s['/Ac/L%d/Power' % i] for i in (1, 2, 3)] == [3000] * 3
	assert [s['/Ac/L%d/Current' % i] for i in (1, 2, 3)] == [10.0] * 3
	assert [s['/Ac/L%d/Voltage' % i] for i in (1, 2, 3)] == [230.0] * 3
	assert s['/Ac/L1/Energy/Forward'] == pytest.approx(100 / 3.0)
	assert s['/Ac/MaxPower'] == 10000.0
	assert s['/Ac/PowerLimit'] == 10000.0
	assert s['/ErrorCode'] == 0
	assert s['/Position'] == 0
	assert s['/PositionIsAdjustable'] == 1
	assert s['/Ac/AdvancedPwrControlEn'] == 1
	assert s['/Ac/ActivePowerLimit'] == 80
	assert s.text('/Ac/ActivePowerLimit') == '80%'


def test_pv_inverter_single_phase(restart):
	pv = make_pv(restart)
	pv.update(services.PvInverterReading(inverter_reading(phases=1), 1, 100))
	s = pv.service
	assert s['/Ac/L1/Power'] == 9000
	unused = ['/Ac/L2/Power', '/Ac/L3/Current', '/Ac/L3/Voltage', '/Ac/L2/Energy/Forward']
	assert [s[path] for path in unused] == [None] * 4


def test_pv_inverter_unknown_max_power(restart):
	pv = make_pv(restart, max_power=0)
	assert pv.service['/Ac/MaxPower'] is None


def test_position_setting(restart):
	pv = make_pv(restart)
	assert pv.service.set_value('/Position', 1) == 0
	assert SettingsDevice.stored['/Settings/Devices/solaredge_7E123456/Position'] == 1
	assert pv.service.set_value('/Position', 3) == 2
	assert pv.service.set_value('/Position', 'x') == 2
	assert pv.service['/Position'] == 1


# power limit (ESS zero feed-in)
def test_no_power_limit_path_without_limiter(restart):
	pv = make_pv(restart)
	assert '/Ac/PowerLimit' not in pv.service
	pv.check_power_limit()


def test_set_power_limit(restart):
	limiter = FakeLimiter()
	pv = make_pv(restart, limiter)
	assert pv.service.set_value('/Ac/PowerLimit', 2345) == 0
	assert pv.service['/Ac/PowerLimit'] == 2345
	assert limiter.limits == [2345.0]


def test_power_limit_refresh_with_same_value(restart):
	limiter = FakeLimiter()
	pv = make_pv(restart, limiter)
	pv.service.set_value('/Ac/PowerLimit', 2345)
	pv.service.set_value('/Ac/PowerLimit', 2345)
	assert limiter.limits == [2345.0, 2345.0]


def test_power_limit_without_itemtype_support(restart, legacy_velib):
	limiter = FakeLimiter()
	pv = make_pv(restart, limiter)
	pv.service.set_value('/Ac/PowerLimit', 2345)
	pv.service.set_value('/Ac/PowerLimit', 2345)
	assert limiter.limits == [2345.0]


@pytest.mark.parametrize('value', [None, 'abc', float('nan')])
def test_invalid_power_limit(restart, value):
	limiter = FakeLimiter()
	pv = make_pv(restart, limiter)
	assert pv.service.set_value('/Ac/PowerLimit', value) == 2
	assert limiter.limits == []


def test_power_limit_write_error(restart, caplog):
	limiter = FakeLimiter()
	limiter.error = True
	pv = make_pv(restart, limiter)
	assert pv.service.set_value('/Ac/PowerLimit', 1000) == 2
	assert pv.service['/Ac/PowerLimit'] == 10000.0
	assert 'setting power limit failed' in caplog.text


def test_power_limit_expiry(restart):
	limiter = FakeLimiter()
	pv = make_pv(restart, limiter)
	pv.service.set_value('/Ac/PowerLimit', 1000)
	pv.check_power_limit()
	assert limiter.resets == 0
	limiter.is_expired = True
	pv.check_power_limit()
	assert limiter.resets == 1
	assert pv.service['/Ac/PowerLimit'] == 10000.0


# SolarEdge extension paths
@pytest.mark.parametrize('value, expected', [(0, False), (1, True), (1.0, True)])
def test_advanced_power_control(restart, value, expected):
	inverter = FakeInverter()
	pv = make_pv(restart, inverter=inverter)
	assert pv.service.set_value('/Ac/AdvancedPwrControlEn', value) == 0
	assert inverter.calls == [('adv', expected)]


@pytest.mark.parametrize('value', [2, -1, 0.5, 'on'])
def test_advanced_power_control_invalid(restart, value):
	inverter = FakeInverter()
	pv = make_pv(restart, inverter=inverter)
	assert pv.service.set_value('/Ac/AdvancedPwrControlEn', value) == 2
	assert inverter.calls == []


@pytest.mark.parametrize('value, percent', [(0, 0), (50, 50), (100, 100), (50.4, 50), ('75', 75)])
def test_active_power_limit(restart, value, percent):
	inverter = FakeInverter()
	pv = make_pv(restart, inverter=inverter)
	assert pv.service.set_value('/Ac/ActivePowerLimit', value) == 0
	assert inverter.calls == [('limit', percent)]


@pytest.mark.parametrize('value', [-1, 101, 'abc', [1]])
def test_active_power_limit_invalid(restart, value):
	inverter = FakeInverter()
	pv = make_pv(restart, inverter=inverter)
	assert pv.service.set_value('/Ac/ActivePowerLimit', value) == 2
	assert inverter.calls == []


def test_extension_path_write_error(restart, caplog):
	inverter = FakeInverter()
	inverter.error = True
	pv = make_pv(restart, inverter=inverter)
	assert pv.service.set_value('/Ac/ActivePowerLimit', 50) == 2
	assert 'writing /Ac/ActivePowerLimit failed' in caplog.text


# ---- InverterTemperature ----
def test_temperature(restart):
	temp = services.InverterTemperature('solaredge_7E123456_temperature', INVERTER_INFO, **common(restart))
	temp.update(inverter_reading(temperature=-5.2))
	s = temp.service
	assert s.name == 'com.victronenergy.temperature.solaredge_7E123456_temperature'
	assert s['/Temperature'] == -5.2
	assert s['/TemperatureType'] == 2  # generic, not battery
	assert s['/CustomName'] == 'PV inverter temperature'
	assert s['/Status'] == 0
	assert s.set_value('/TemperatureType', 4) == 0
	assert s.set_value('/TemperatureType', 7) == 2


# ---- ThrottleInput ----
@pytest.mark.parametrize('status, power, state, alarm', [
	(solaredge.STATUS_THROTTLED, 9000, 3, 2),
	(solaredge.STATUS_THROTTLED, 50, 2, 0),
	(solaredge.STATUS_THROTTLED, None, 2, 0),
	(solaredge.STATUS_MPPT, 9000, 2, 0),
])
def test_throttle_input(restart, status, power, state, alarm):
	throttle = services.ThrottleInput('throttle', INVERTER_INFO, **common(restart))
	throttle.update(inverter_reading(power=power, status=status))
	assert throttle.service['/State'] == state
	assert throttle.service['/Alarm'] == alarm
	assert throttle.service['/Type'] == 2
