import pytest

ADV = 0xF142
LIMIT = 0xF001
COMMIT = 0xF100


@pytest.fixture
def pv(bridge, client):
    bridge.update()
    client.writes.clear()
    return bridge.services['pv']


# ---- /Ac/ActivePowerLimit (%) ----
@pytest.mark.parametrize('value, percent', [(0, 0), (50, 50), (100, 100), (50.4, 50), ('75', 75)])
def test_active_power_limit(pv, client, value, percent):
    assert pv.set_value('/Ac/ActivePowerLimit', value) is True
    assert client.writes == [(LIMIT, [percent]), (COMMIT, [1])]


@pytest.mark.parametrize('value', [-1, 101, None, 'abc', float('nan')])
def test_active_power_limit_invalid(pv, client, value):
    assert pv.set_value('/Ac/ActivePowerLimit', value) is False
    assert client.writes == []


def test_active_power_limit_write_error(pv, client, caplog):
    client.write_error = True
    assert pv.set_value('/Ac/ActivePowerLimit', 50) is False
    assert 'writing /Ac/ActivePowerLimit failed' in caplog.text


# ---- /Ac/AdvancedPwrControlEn ----
@pytest.mark.parametrize('value, registers', [(0, [0, 0]), (1, [1, 0]), (1.0, [1, 0])])
def test_adv_pwr_control_en(pv, client, value, registers):
    assert pv.set_value('/Ac/AdvancedPwrControlEn', value) is True
    assert client.writes == [(ADV, registers)]


@pytest.mark.parametrize('value', [2, -1, 0.5, None, 'on'])
def test_adv_pwr_control_en_invalid(pv, client, value):
    assert pv.set_value('/Ac/AdvancedPwrControlEn', value) is False
    assert client.writes == []


def test_adv_pwr_control_en_write_error(pv, client, caplog):
    client.write_error = True
    assert pv.set_value('/Ac/AdvancedPwrControlEn', 1) is False
    assert 'writing /Ac/AdvancedPwrControlEn failed' in caplog.text


# ---- /Ac/PowerLimit (W, written by ESS) ----
def test_power_limit_enables_adv_pwr_control(pv, client):
    assert pv['/Ac/AdvancedPwrControlEn'] == 0
    assert pv.set_value('/Ac/PowerLimit', 2345) is True
    # 2345 W of 10000 W is rounded up to 24 %
    assert client.writes == [(ADV, [1, 0]), (LIMIT, [24]), (COMMIT, [1])]


def test_power_limit_skips_enable_when_already_enabled(bridge, pv, client):
    client.load(ADV, [1, 0])
    bridge.update()
    assert pv.set_value('/Ac/PowerLimit', 5000) is True
    assert client.writes == [(LIMIT, [50]), (COMMIT, [1])]


@pytest.mark.parametrize('watts, percent', [(0, 0), (-500, 0), (1, 1), (10000, 100), (20000, 100)])
def test_power_limit_is_clamped(bridge, pv, client, watts, percent):
    client.load(ADV, [1, 0])
    bridge.update()
    assert pv.set_value('/Ac/PowerLimit', watts) is True
    assert client.writes == [(LIMIT, [percent]), (COMMIT, [1])]


@pytest.mark.parametrize('value', [None, 'abc'])
def test_power_limit_invalid(pv, client, value):
    assert pv.set_value('/Ac/PowerLimit', value) is False
    assert client.writes == []


def test_power_limit_without_max_power(se, client):
    bridge = se.SolarEdge(client, 126, 'test')
    bridge.create_services()
    pv = bridge.services['pv']
    assert pv.set_value('/Ac/PowerLimit', 1000) is False
    assert client.writes == []


def test_power_limit_write_error(pv, client, caplog):
    client.write_error = True
    assert pv.set_value('/Ac/PowerLimit', 1000) is False
    assert 'writing /Ac/PowerLimit failed' in caplog.text


def test_power_limit_text(pv):
    assert pv.text('/Ac/PowerLimit') == '10000W'
