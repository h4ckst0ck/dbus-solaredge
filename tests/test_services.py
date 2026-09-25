import pytest

from conftest import FakeModbusClient, string_regs


def test_service_names_and_instances(bridge):
    services = bridge.services
    assert sorted(services) == ['grid', 'pv', 'temp']
    assert services['grid'].servicename == 'com.victronenergy.grid.grid_id00'
    assert services['pv'].servicename == 'com.victronenergy.pvinverter.pv0.pvinverter_id00'
    assert services['temp'].servicename == 'com.victronenergy.temperature.temp_pvinverter_id00'
    assert [services[s]['/DeviceInstance'] for s in ('grid', 'pv', 'temp')] == [0, 20, 26]
    assert [services[s]['/ProductId'] for s in ('grid', 'pv', 'temp')] == [16, 41284, 0]


def test_common_paths(se, bridge):
    for service in bridge.services.values():
        assert service['/Connected'] == 1
        assert service['/Mgmt/Connection'] == 'ModbusTCP test:502, UNIT 126'
        assert service['/Mgmt/ProcessVersion'].startswith(se.VERSION + ' on Python ')
        assert service['/DataManagerVersion'] == se.VERSION


def test_device_strings(bridge):
    pv = bridge.services['pv']
    assert pv['/ProductName'] == 'SolarEdge SE10K'
    assert pv['/FirmwareVersion'] == '0004.0018.0032'
    assert pv['/Serial'] == '7E123456'

    grid = bridge.services['grid']
    assert grid['/ProductName'] == 'SolarEdge SE-WND-3Y400-MB-K2'
    assert grid['/FirmwareVersion'] == '2.3'
    assert grid['/Serial'] == 'M1234'
    assert grid['/CustomName'] == 'Grid meter Export+Import'



@pytest.mark.parametrize('address', [40004, 40123])
def test_device_strings_use_full_register_range(se, client, address):
    # every field completely filled, the last character of each field must not be cut off
    client.load(address, string_regs('M' * 32, 16) + string_regs('D' * 32, 16) + string_regs('O' * 16, 8)
                + string_regs('V' * 16, 8) + string_regs('S' * 32, 16))
    bridge = se.SolarEdge(client, 126, 'test')
    bridge.create_services()
    service = bridge.services['pv' if address == 40004 else 'grid']
    assert service['/ProductName'] == 'M' * 32 + ' ' + 'D' * 32
    assert service['/FirmwareVersion'] == 'V' * 16
    assert service['/Serial'] == 'S' * 32

def test_temperature_service(bridge):
    temp = bridge.services['temp']
    assert temp['/CustomName'] == 'PV Inverter Temperature'
    assert temp['/TemperatureType'] == 2
    assert temp.writeable['/TemperatureType']


def test_writeable_paths(bridge):
    pv = bridge.services['pv']
    writeable = sorted(path for path, flag in pv.writeable.items() if flag)
    assert writeable == ['/Ac/ActivePowerLimit', '/Ac/AdvancedPwrControlEn', '/Ac/PowerLimit']


@pytest.mark.parametrize('phases', [1, 2, 3])
def test_phase_count_from_sunspec_model(se, phases):
    bridge = se.SolarEdge(FakeModbusClient(phases), 126, 'test')
    bridge.create_services()
    assert bridge.phases == phases


def test_unknown_model_defaults_to_three_phases(se, client):
    client.load(40069, [0xFFFF])
    bridge = se.SolarEdge(client, 126, 'test')
    bridge.create_services()
    assert bridge.phases == 3


def test_limit_input_service_disabled_by_default(bridge):
    assert 'limit' not in bridge.services


def test_limit_input_service(se, client, monkeypatch):
    monkeypatch.setattr(se, 'ENABLE_LIMIT_INPUT', True)
    bridge = se.SolarEdge(client, 126, 'test')
    bridge.create_services()
    limit = bridge.services['limit']
    assert limit.servicename == 'com.victronenergy.digitalinput.limit_pvinverter_id00'
    assert limit['/DeviceInstance'] == 10
    assert limit['/Type'] == 2


def test_fixed_max_power_is_logged(se, client, caplog):
    caplog.set_level('INFO')
    bridge = se.SolarEdge(client, 126, 'test', max_power=25000)
    bridge.create_services()
    assert 'maxPower manually set to 25000 W' in caplog.text


def test_create_services_fails_on_read_error(se, client):
    client.read_error = True
    bridge = se.SolarEdge(client, 126, 'test')
    with pytest.raises(se.ModbusError):
        bridge.create_services()
