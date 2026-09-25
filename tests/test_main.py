import logging
import runpy
import sys
import types

import pytest

import conftest
from conftest import FakeModbusClient


class FakeMainLoop(object):
    """Runs the registered timer until it returns False or quit() is called."""

    def __init__(self, glib):
        self.glib = glib
        self.running = False

    def run(self):
        self.running = True
        interval, callback = self.glib.timers[0]
        while self.running and callback():
            pass

    def quit(self):
        self.running = False


class FakeGLib(object):
    def __init__(self):
        self.timers = []
        self.loops = []

    def MainLoop(self):
        loop = FakeMainLoop(self)
        self.loops.append(loop)
        return loop

    def timeout_add(self, interval, callback):
        self.timers.append((interval, callback))


@pytest.fixture
def glib(se, monkeypatch):
    glib = FakeGLib()
    monkeypatch.setattr(se, 'GLib', glib)
    return glib


@pytest.fixture
def modbus(se, monkeypatch):
    created = []

    def factory(host, port, retry_on_empty):
        client = FakeModbusClient()
        client.args = (host, port, retry_on_empty)
        created.append(client)
        return client

    monkeypatch.setattr(se, 'ModbusClient', factory)
    return created


def test_main_runs_until_fatal_error(se, glib, modbus, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['dbus-solaredge.py'])
    monkeypatch.setattr(se, 'MAX_CONSECUTIVE_ERRORS', 3)
    calls = []
    original_update = se.SolarEdge.update

    def update(self):
        calls.append(True)
        if len(calls) > 2:
            self.client.read_error = True
        return original_update(self)

    monkeypatch.setattr(se.SolarEdge, 'update', update)

    with pytest.raises(SystemExit) as exit_info:
        se.main()

    assert exit_info.value.code == 1
    client = modbus[0]
    assert client.args == (se.SERVER_HOST, se.SERVER_PORT, True)
    assert glib.timers[0][0] == se.UPDATE_INTERVAL_MS
    assert len(calls) == 2 + 3  # two good cycles, then three errors
    assert client.closed
    assert logging.getLogger().level == logging.INFO


def test_main_command_line_arguments(se, glib, modbus, monkeypatch, caplog):
    caplog.set_level('INFO')
    monkeypatch.setattr(sys, 'argv', ['dbus-solaredge.py', '--host', '10.0.0.5', '--port', '1502',
                                      '--unit', '126', '--max-power', '25000'])
    monkeypatch.setattr(se, 'MAX_CONSECUTIVE_ERRORS', 1)
    monkeypatch.setattr(FakeModbusClient, 'read_holding_registers', _fail_after_setup())

    with pytest.raises(SystemExit):
        se.main()

    assert modbus[0].args == ('10.0.0.5', 1502, True)
    assert 'ModbusTCP 10.0.0.5:1502, UNIT 126' in caplog.text
    assert 'maxPower manually set to 25000.0 W' in caplog.text


def _fail_after_setup():
    original = FakeModbusClient.read_holding_registers

    def read(self, address, count, unit):
        if address == 40190:  # first register block read by update()
            return conftest.Response(error=True)
        return original(self, address, count, unit)
    return read


def test_main_connect_failure(se, glib, monkeypatch, caplog):
    client = FakeModbusClient()
    client.connect_result = False
    monkeypatch.setattr(se, 'ModbusClient', lambda *args, **kwargs: client)
    monkeypatch.setattr(sys, 'argv', ['dbus-solaredge.py'])

    with pytest.raises(SystemExit) as exit_info:
        se.main()

    assert exit_info.value.code == 1
    assert 'unable to connect to 192.168.178.80:502' in caplog.text
    assert glib.timers == []


def test_script_entry_point(monkeypatch):
    """Running the file as script calls main()."""
    client = FakeModbusClient()
    client.connect_result = False
    sync = types.ModuleType('pymodbus.client.sync')
    sync.ModbusTcpClient = lambda *args, **kwargs: client
    monkeypatch.setitem(sys.modules, 'pymodbus.client.sync', sync)
    monkeypatch.setattr(sys, 'argv', ['dbus-solaredge.py'])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(conftest.SCRIPT), run_name='__main__')
    assert exit_info.value.code == 1
