import pytest

from conftest import BusConnection, s16, string_regs


def test_get_string_skips_null_bytes_and_strips(se):
    assert se._get_string(string_regs(' SolarEdge ', 8)) == 'SolarEdge'
    # a null byte in the high byte of a register is skipped as well
    assert se._get_string([0x0041, 0x4200]) == 'AB'


def test_get_signed_short(se):
    assert se._get_signed_short(0x0001) == 1
    assert se._get_signed_short(0xFFFF) == -1
    assert se._get_signed_short(0x8000) == -32768


@pytest.mark.parametrize('reg, factor', [(0, 1), (2, 100), (s16(-2), 0.01)])
def test_get_scale_factor(se, reg, factor):
    assert se._get_scale_factor(reg) == pytest.approx(factor)


def test_uint16(se):
    assert se._uint16(2301, 0.1) == 230.1
    assert se._uint16(0xFFFF, 1) is None


def test_int16(se):
    assert se._int16(s16(-52), 0.1) == -5.2
    assert se._int16(0x8000, 1) is None


def test_negate(se):
    assert se._negate(5) == -5
    assert se._negate(None) is None


def test_energy_kwh(se):
    assert se._energy_kwh(0x0001, 0x86A0, 1) == 100.0
    assert se._energy_kwh(0, 5, 1000) == 5.0


@pytest.mark.parametrize('state, victron', [
    (1, 8),   # off -> standby
    (2, 8),   # sleeping -> standby
    (3, 1),   # wake-up -> startup
    (4, 11),  # MPPT -> running
    (5, 12),  # throttled -> running (throttled)
    (6, 8),   # shutting down -> standby
    (7, 10),  # fault -> error
    (8, 8),   # maintenance -> standby
])
def test_victron_pv_state(se, state, victron):
    assert se._get_victron_pv_state(state) == victron


@pytest.mark.parametrize('kind, value, registers', [
    ('16bit_uint', 50, [50]),
    ('16bit_int', 1, [1]),
    ('32bit_int', 1, [1, 0]),  # word order little endian
])
def test_encode(se, kind, value, registers):
    assert se._encode(kind, value) == registers


def test_encode_decode_roundtrip(se):
    assert se._decode('32bit_float', se._encode('32bit_float', 10000.0)) == 10000.0
    assert se._decode('32bit_int', [1, 0]) == 1
    assert se._decode('16bit_uint', [75]) == 75


@pytest.mark.parametrize('value, number', [
    (5, 5.0),
    ('7.5', 7.5),
    (None, None),
    ('abc', None),
    (float('nan'), None),
])
def test_to_number(se, value, number):
    assert se._to_number(value) == number


def test_text(se):
    assert se._w('/p', 12.345) == '12.35W'
    assert se._kwh('/p', 1.23456) == '1.235kWh'
    assert se._c('/p', -5.2) == '-5.2C'
    assert se._pct('/p', 80) == '80%'
    assert se._w('/p', None) == ''


def test_dbusconnection_system(se, monkeypatch):
    monkeypatch.delenv('DBUS_SESSION_BUS_ADDRESS', raising=False)
    bus = se.dbusconnection()
    assert isinstance(bus, se.SystemBus)
    assert bus.bus_type == BusConnection.TYPE_SYSTEM


def test_dbusconnection_session(se, monkeypatch):
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/tmp/bus')
    bus = se.dbusconnection()
    assert isinstance(bus, se.SessionBus)
    assert bus.bus_type == BusConnection.TYPE_SESSION

