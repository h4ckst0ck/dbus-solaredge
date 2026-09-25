import pytest

from conftest import FakeModbusClient, s16


def test_update_grid(bridge):
    assert bridge.update() is True
    grid = bridge.services['grid']
    # the meter reports export as negative power, Victron expects it the other way round
    assert grid['/Ac/Power'] == 3000
    assert [grid['/Ac/L%d/Power' % i] for i in (1, 2, 3)] == [1500, 1000, 500]
    assert [grid['/Ac/L%d/Current' % i] for i in (1, 2, 3)] == [-5.0, 2.0, 3.0]
    assert [grid['/Ac/L%d/Voltage' % i] for i in (1, 2, 3)] == [230.0, 231.0, 232.0]
    assert grid['/Ac/Energy/Reverse'] == 6.0
    assert [grid['/Ac/L%d/Energy/Reverse' % i] for i in (1, 2, 3)] == [1.0, 2.0, 3.0]
    assert grid['/Ac/Energy/Forward'] == 65.536
    assert [grid['/Ac/L%d/Energy/Forward' % i] for i in (1, 2, 3)] == [4.0, 5.0, 7.0]


def test_update_grid_not_implemented_phase(bridge, client):
    client.load(40193, [0x8000])  # current L3
    client.load(40209, [0x8000])  # power L3
    bridge.update()
    grid = bridge.services['grid']
    assert grid['/Ac/L3/Current'] is None
    assert grid['/Ac/L3/Power'] is None


def test_update_inverter_three_phase(bridge):
    bridge.update()
    pv = bridge.services['pv']
    assert pv['/Ac/Power'] == 9000
    assert [pv['/Ac/L%d/Power' % i] for i in (1, 2, 3)] == [3000, 3000, 3000]
    assert [pv['/Ac/L%d/Current' % i] for i in (1, 2, 3)] == [10.0, 10.1, 10.2]
    assert [pv['/Ac/L%d/Voltage' % i] for i in (1, 2, 3)] == [230.1, 230.2, 230.3]
    assert pv['/Ac/Energy/Forward'] == 100.0
    assert [pv['/Ac/L%d/Energy/Forward' % i] for i in (1, 2, 3)] == pytest.approx([100 / 3.0] * 3)
    assert pv['/StatusCode'] == 11
    assert pv['/ErrorCode'] == 0
    assert bridge.services['temp']['/Temperature'] == 45.2


def test_update_inverter_single_phase(se):
    bridge = se.SolarEdge(FakeModbusClient(phases=1), 126, 'test')
    bridge.create_services()
    bridge.update()
    pv = bridge.services['pv']
    assert [pv['/Ac/L%d/Current' % i] for i in (1, 2, 3)] == [10.0, None, None]
    assert [pv['/Ac/L%d/Voltage' % i] for i in (1, 2, 3)] == [230.1, None, None]
    assert [pv['/Ac/L%d/Power' % i] for i in (1, 2, 3)] == [9000, None, None]
    assert [pv['/Ac/L%d/Energy/Forward' % i] for i in (1, 2, 3)] == [100.0, None, None]


def test_update_inverter_split_phase(se):
    bridge = se.SolarEdge(FakeModbusClient(phases=2), 126, 'test')
    bridge.create_services()
    bridge.update()
    pv = bridge.services['pv']
    assert [pv['/Ac/L%d/Power' % i] for i in (1, 2, 3)] == [4500, 4500, None]


def test_update_inverter_power_not_implemented(bridge, client):
    client.load(40083, [0x8000])
    bridge.update()
    pv = bridge.services['pv']
    assert pv['/Ac/Power'] is None
    assert [pv['/Ac/L%d/Power' % i] for i in (1, 2, 3)] == [None, None, None]


def test_negative_temperature(bridge, client):
    client.load(40103, [s16(-52)])
    bridge.update()
    assert bridge.services['temp']['/Temperature'] == -5.2


def test_negative_inverter_power(bridge, client):
    client.load(40083, [s16(-12)])  # night consumption
    bridge.update()
    assert bridge.services['pv']['/Ac/Power'] == -12


def test_update_power_control(bridge, client, caplog):
    caplog.set_level('INFO')
    client.load(0xF142, [1, 0])
    client.load(0xF001, [40])
    bridge.update()
    pv = bridge.services['pv']
    assert pv['/Ac/AdvancedPwrControlEn'] == 1
    assert pv['/Ac/MaxPower'] == 10000.0
    assert pv['/Ac/ActivePowerLimit'] == 40
    assert pv.text('/Ac/ActivePowerLimit') == '40%'
    assert pv['/Ac/PowerLimit'] == 4000
    assert 'maxPower received: 10000.0 W' in caplog.text


def test_max_power_change_is_logged_once(bridge, client, caplog):
    caplog.set_level('INFO')
    bridge.update()
    bridge.update()
    assert caplog.text.count('maxPower received') == 1
    client.set_max_power(8000.0)
    bridge.update()
    assert 'maxPower received: 8000.0 W' in caplog.text
    assert bridge.max_power == 8000.0


def test_fixed_max_power_is_not_overwritten(se, client):
    client.load(0xF304, [0xFFFF, 0xFFFF])  # garbage, must not be read
    bridge = se.SolarEdge(client, 126, 'test', max_power=25000)
    bridge.create_services()
    client.load(0xF001, [50])
    bridge.update()
    pv = bridge.services['pv']
    assert pv['/Ac/MaxPower'] == 25000
    assert pv['/Ac/PowerLimit'] == 12500


@pytest.mark.parametrize('status, power, state, alarm', [
    (5, 9000, 3, 2),  # throttled and producing
    (5, 50, 2, 0),    # throttled but (almost) no power
    (4, 9000, 2, 0),  # producing, not throttled
])
def test_limit_input(se, client, monkeypatch, status, power, state, alarm):
    monkeypatch.setattr(se, 'ENABLE_LIMIT_INPUT', True)
    bridge = se.SolarEdge(client, 126, 'test')
    bridge.create_services()
    client.load(40107, [status])
    client.load(40083, [power])
    bridge.update()
    assert bridge.services['limit']['/State'] == state
    assert bridge.services['limit']['/Alarm'] == alarm


def test_limit_input_power_not_implemented(se, client, monkeypatch):
    monkeypatch.setattr(se, 'ENABLE_LIMIT_INPUT', True)
    bridge = se.SolarEdge(client, 126, 'test')
    bridge.create_services()
    client.load(40107, [5])
    client.load(40083, [0x8000])
    bridge.update()
    assert bridge.services['limit']['/State'] == 2


def test_read_error_sets_disconnected_and_retries(bridge, client, caplog):
    client.read_error = True
    assert bridge.update() is True
    assert bridge.update() is True
    assert bridge.errors == 2
    assert all(s['/Connected'] == 0 for s in bridge.services.values())
    # the traceback is only logged for the first error
    assert caplog.text.count('update failed, will retry') == 1


def test_recovery_after_errors(bridge, client, caplog):
    caplog.set_level('INFO')
    client.read_error = True
    bridge.update()
    client.read_error = False
    assert bridge.update() is True
    assert bridge.errors == 0
    assert all(s['/Connected'] == 1 for s in bridge.services.values())
    assert 'update succeeded again after 1 error(s)' in caplog.text


def test_exception_from_client_is_handled(bridge, client, monkeypatch):
    def broken(*args, **kwargs):
        raise ConnectionError('connection reset')
    monkeypatch.setattr(client, 'read_holding_registers', broken)
    assert bridge.update() is True
    assert bridge.errors == 1


def test_gives_up_after_max_errors(se, bridge, client):
    fatal = []
    bridge.on_fatal = lambda: fatal.append(True)
    client.read_error = True
    results = [bridge.update() for _ in range(se.MAX_CONSECUTIVE_ERRORS)]
    assert results[:-1] == [True] * (se.MAX_CONSECUTIVE_ERRORS - 1)
    assert results[-1] is False  # stops the GLib timer
    assert fatal == [True]


def test_gives_up_without_fatal_callback(se, bridge, client):
    client.read_error = True
    bridge.errors = se.MAX_CONSECUTIVE_ERRORS - 1
    assert bridge.update() is False
