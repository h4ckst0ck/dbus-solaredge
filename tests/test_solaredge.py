import pytest

import solaredge
from conftest import FakeModbusClient
from fake_inverter import s16


# ---- ModbusDevice ----
def test_read_and_write(modbus, client):
	assert modbus.read(solaredge.REG_ACTIVE_POWER_LIMIT, 1) == [100]
	modbus.write(0xF001, [50])
	assert client.writes == [(0xF001, [50])]


def test_read_error(modbus, client):
	client.read_error = True
	with pytest.raises(solaredge.ModbusError, match='reading 1 registers at 0xF001 failed'):
		modbus.read(0xF001, 1)


def test_write_error(modbus, client):
	client.write_error = True
	with pytest.raises(solaredge.ModbusError, match='writing registers at 0xF001 failed'):
		modbus.write(0xF001, [50])


@pytest.mark.parametrize('kind, value, registers', [
	('16bit_uint', 50, [50]),
	('16bit_int', -1, [0xFFFF]),
	('32bit_int', 1, [1, 0]),            # low word first
	('32bit_uint', 0x12345678, [0x5678, 0x1234]),
	('32bit_float', 10000.0, [0x4000, 0x461C]),
])
def test_encode_decode(kind, value, registers):
	assert solaredge.ModbusDevice.encode(kind, value) == registers
	assert solaredge.ModbusDevice.decode(kind, registers) == value


def test_read_write_value(modbus, client):
	assert modbus.read_value(solaredge.REG_MAX_ACTIVE_POWER, '32bit_float') == 10000.0
	modbus.write_value(solaredge.REG_DYNAMIC_ACTIVE_POWER_LIMIT, '32bit_float', 42.5)
	assert client.value(solaredge.REG_DYNAMIC_ACTIVE_POWER_LIMIT, '32bit_float') == 42.5


# ---- Inverter ----
@pytest.mark.parametrize('phases', [1, 2, 3])
def test_inverter_info(phases):
	inverter = solaredge.Inverter(solaredge.ModbusDevice(FakeModbusClient(phases), 126))
	info = inverter.read_info()
	assert info.manufacturer == 'SolarEdge'
	assert info.model == 'SE10K'
	assert info.version == '0004.0018.0032'
	assert info.serial == '7E123456'
	assert inverter.phases == phases
	assert inverter.info is info


def test_inverter_unknown_model_is_three_phase(modbus, client):
	client.load(solaredge.INVERTER_MODEL_ID, [0xFFFF])
	inverter = solaredge.Inverter(modbus)
	inverter.read_info()
	assert inverter.phases == 3


def test_inverter_read_three_phase(modbus):
	inverter = solaredge.Inverter(modbus)
	inverter.read_info()
	r = inverter.read()
	assert r.power == 9000
	assert r.energy == 100.0
	assert [p.current for p in r.phases] == [10.0, 10.1, 10.2]
	assert [p.voltage for p in r.phases] == [230.1, 230.2, 230.3]
	assert [p.power for p in r.phases] == [3000, 3000, 3000]
	assert [p.energy_forward for p in r.phases] == pytest.approx([100 / 3.0] * 3)
	assert [p.energy_reverse for p in r.phases] == [None] * 3
	assert r.status == solaredge.STATUS_MPPT
	assert r.error_code == 0
	assert r.temperature == 45.2


def test_inverter_read_single_phase():
	inverter = solaredge.Inverter(solaredge.ModbusDevice(FakeModbusClient(1), 126))
	inverter.read_info()
	r = inverter.read()
	assert len(r.phases) == 1
	assert r.phases[0].power == 9000
	assert r.phases[0].energy_forward == 100.0


def test_inverter_read_negative_values(modbus, client):
	client.load(40083, [s16(-12)])   # night consumption
	client.load(40103, [s16(-52)])   # frost
	r = solaredge.Inverter(modbus).read()
	assert r.power == -12
	assert r.temperature == -5.2


def test_inverter_power_not_implemented(modbus, client):
	client.load(40083, [0x8000])
	r = solaredge.Inverter(modbus).read()
	assert r.power is None
	assert [p.power for p in r.phases] == [None] * 3


def test_inverter_power_control_registers(modbus, client):
	inverter = solaredge.Inverter(modbus)
	assert inverter.read_max_power() == 10000.0
	assert inverter.read_advanced_power_control() == 1
	assert inverter.read_active_power_limit() == 100

	inverter.write_active_power_limit(40)
	assert client.writes == [(solaredge.REG_ACTIVE_POWER_LIMIT, [40])]  # dynamic register, no commit

	client.writes.clear()
	inverter.write_advanced_power_control(False)
	assert client.writes == [
		(solaredge.REG_ADV_PWR_CONTROL_EN, [0, 0]), (solaredge.REG_COMMIT_POWER_CONTROL, [1])]
	client.writes.clear()
	inverter.write_advanced_power_control(True)
	assert client.writes[0] == (solaredge.REG_ADV_PWR_CONTROL_EN, [1, 0])


# ---- Meter ----
def test_meter_info(modbus):
	meter = solaredge.Meter(modbus)
	info = meter.read_info()
	assert info.model == 'SE-WND-3Y400-MB-K2'
	assert info.serial == 'M1234'
	assert meter.info is info


def test_meter_read(modbus):
	r = solaredge.Meter(modbus).read()
	assert r.power == 3000  # exporting is positive for Victron
	assert r.energy_forward == 65.536
	assert r.energy_reverse == 6.0
	assert [p.power for p in r.phases] == [1500, 1000, 500]
	assert [p.current for p in r.phases] == [-5.0, 2.0, 3.0]
	assert [p.voltage for p in r.phases] == [230.0, 231.0, 232.0]
	assert [p.energy_forward for p in r.phases] == [4.0, 5.0, 7.0]
	assert [p.energy_reverse for p in r.phases] == [1.0, 2.0, 3.0]


def test_meter_not_implemented_phase(modbus, client):
	client.load(40193, [0x8000])
	client.load(40209, [0x8000])
	r = solaredge.Meter(modbus).read()
	assert r.phases[2].current is None
	assert r.phases[2].power is None


# ---- PowerLimiter ----
@pytest.fixture
def limiter(modbus, clock):
	return solaredge.PowerLimiter(modbus, 10000.0, 120, clock)


def test_limiter_initialize(limiter, client):
	limiter.initialize()
	# same sequence as the SolarEdge limiter of Victron's dbus-fronius
	assert [a for a, v in client.writes] == [
		solaredge.REG_ACTIVE_POWER_RAMP_UP, solaredge.REG_ACTIVE_POWER_RAMP_DOWN,
		solaredge.REG_ACTIVE_POWER_RAMP_UP, solaredge.REG_ACTIVE_POWER_RAMP_DOWN,
		solaredge.REG_FALLBACK_ACTIVE_POWER_LIMIT, solaredge.REG_COMMAND_TIMEOUT,
		solaredge.REG_ENABLE_DYNAMIC_POWER_CONTROL]
	assert client.written(solaredge.REG_ACTIVE_POWER_RAMP_UP, '32bit_float') == [100.0, -1.0]
	assert client.value(solaredge.REG_FALLBACK_ACTIVE_POWER_LIMIT, '32bit_float') == 100.0
	assert client.value(solaredge.REG_COMMAND_TIMEOUT, '32bit_uint') == 120
	assert client.value(solaredge.REG_ENABLE_DYNAMIC_POWER_CONTROL, '16bit_uint') == 1


def test_limiter_initialize_enables_advanced_power_control(limiter, client):
	client.load(solaredge.REG_ADV_PWR_CONTROL_EN, [0, 0])
	limiter.initialize()
	assert client.writes[:2] == [
		(solaredge.REG_ADV_PWR_CONTROL_EN, [1, 0]), (solaredge.REG_COMMIT_POWER_CONTROL, [1])]


@pytest.mark.parametrize('watts, percent, effective', [
	(2345, 23.45, 2345),
	(0, 0, 0),
	(-500, 0, 0),
	(10000, 100, 10000),
	(20000, 100, 10000),
])
def test_limiter_set_limit(limiter, client, watts, percent, effective):
	assert limiter.set_limit(watts) == pytest.approx(effective)
	assert client.value(solaredge.REG_DYNAMIC_ACTIVE_POWER_LIMIT, '32bit_float') == pytest.approx(percent)


def test_limiter_expires_after_half_the_timeout(limiter, clock):
	assert not limiter.expired()
	limiter.set_limit(5000)
	clock.now += 59
	assert not limiter.expired()
	clock.now += 1
	assert limiter.expired()
	limiter.set_limit(5000)  # a refresh restarts the timer
	assert not limiter.expired()


def test_limiter_reset(limiter, client, clock):
	limiter.set_limit(5000)
	assert limiter.reset() == 10000.0
	assert client.value(solaredge.REG_DYNAMIC_ACTIVE_POWER_LIMIT, '32bit_float') == 100.0
	clock.now += 1000
	assert not limiter.expired()


def test_limiter_write_error(limiter, client):
	client.write_error = True
	with pytest.raises(solaredge.ModbusError):
		limiter.set_limit(5000)
	assert not limiter.expired()
