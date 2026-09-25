"""Test setup: stubs for the Venus OS only modules (dbus, GLib, vedbus) and a fake Modbus client."""

import importlib.util
import logging
import pathlib
import sys
import types

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'dbus-solaredge.py'


# ---- stubs for modules which are only available on Venus OS ----
class BusConnection(object):
    TYPE_SYSTEM = 'system'
    TYPE_SESSION = 'session'

    def __new__(cls, bus_type):
        bus = object.__new__(cls)
        bus.bus_type = bus_type
        return bus


class VeDbusService(dict):
    """Minimal stand-in for velib's VeDbusService, paths are stored as dict items."""

    def __init__(self, servicename, bus):
        dict.__init__(self)
        self.servicename = servicename
        self.bus = bus
        self.onchange = {}
        self.gettext = {}
        self.writeable = {}

    def add_path(self, path, value=None, description='', writeable=False, onchangecallback=None, gettextcallback=None):
        assert path not in self, 'duplicate path %s' % path
        self[path] = value
        self.writeable[path] = writeable
        self.onchange[path] = onchangecallback
        self.gettext[path] = gettextcallback

    def set_value(self, path, value):
        """Emulate a SetValue call from another process on the dbus."""
        assert self.writeable[path]
        if self.onchange[path] is None or self.onchange[path](path, value):
            self[path] = value
            return True
        return False

    def text(self, path):
        return self.gettext[path](path, self[path])


def _install_stubs():
    dbus = types.ModuleType('dbus')
    dbus.bus = types.SimpleNamespace(BusConnection=BusConnection)
    mainloop = types.ModuleType('dbus.mainloop')
    glib = types.ModuleType('dbus.mainloop.glib')
    glib.DBusGMainLoop = lambda set_as_default=False: None
    dbus.mainloop = mainloop
    mainloop.glib = glib

    gi = types.ModuleType('gi')
    repository = types.ModuleType('gi.repository')
    repository.GLib = types.SimpleNamespace(MainLoop=None, timeout_add=None)
    gi.repository = repository

    vedbus = types.ModuleType('vedbus')
    vedbus.VeDbusService = VeDbusService

    sys.modules.update({
        'dbus': dbus,
        'dbus.mainloop': mainloop,
        'dbus.mainloop.glib': glib,
        'gi': gi,
        'gi.repository': repository,
        'vedbus': vedbus,
    })


_install_stubs()


def _load_script():
    spec = importlib.util.spec_from_file_location('dbus_solaredge', str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


se_module = _load_script()


# ---- fake modbus ----
def string_regs(text, count):
    data = text.encode().ljust(2 * count, b'\0')
    return [data[i] << 8 | data[i + 1] for i in range(0, 2 * count, 2)]


def s16(value):
    return value & 0xFFFF


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

    def __init__(self, phases=3):
        self.registers = {}
        self.writes = []
        self.read_error = False
        self.write_error = False
        self.connect_result = True
        self.closed = False

        # SunSpec common block inverter: Manufacturer, Model, Options, Version, Serial
        self.load(40004, string_regs('SolarEdge ', 16) + string_regs('SE10K', 16) + string_regs('', 8)
                  + string_regs('0004.0018.0032', 8) + string_regs('7E123456', 16))
        self.load(40069, [100 + phases])

        inverter = [0] * 38
        inverter[1:5] = [100, 101, 102, s16(-1)]          # current 10.0/10.1/10.2 A
        inverter[8:12] = [2301, 2302, 2303, s16(-1)]      # voltage 230.1/230.2/230.3 V
        inverter[12:14] = [9000, 0]                       # power 9000 W
        inverter[22:25] = [0x0001, 0x86A0, 0]             # energy 100000 Wh
        inverter[32] = 452                                # heat sink temperature
        inverter[35] = s16(-1)                            # 45.2 C
        inverter[36] = 4                                  # MPPT
        inverter[37] = 0
        if phases < 3:
            inverter[3] = inverter[10] = 0xFFFF
        if phases < 2:
            inverter[2] = inverter[9] = 0xFFFF
        self.load(40071, inverter)

        # SunSpec common block meter
        self.load(40123, string_regs('SolarEdge ', 16) + string_regs('SE-WND-3Y400-MB-K2', 16)
                  + string_regs('Export+Import', 8) + string_regs('2.3', 8) + string_regs('M1234', 16))

        meter = [0] * 70
        meter[1:5] = [s16(-50), 20, 30, s16(-1)]                   # -5.0/2.0/3.0 A
        meter[6:9] = [2300, 2310, 2320]                            # voltage
        meter[13] = s16(-1)
        meter[16:21] = [s16(-3000), s16(-1500), s16(-1000), s16(-500), 0]  # exporting 3000 W
        meter[36:44] = [0, 6000, 0, 1000, 0, 2000, 0, 3000]         # exported Wh total/L1/L2/L3
        meter[44:52] = [1, 0, 0, 4000, 0, 5000, 0, 7000]            # imported Wh total/L1/L2/L3
        meter[52] = 0
        self.load(40190, meter)

        self.set_max_power(10000.0)
        self.load(0xF142, [0, 0])
        self.load(0xF001, [100])

    def load(self, address, registers):
        for offset, value in enumerate(registers):
            self.registers[address + offset] = value

    def set_max_power(self, watts):
        self.load(0xF304, se_module._encode('32bit_float', watts))

    def connect(self):
        return self.connect_result

    def close(self):
        self.closed = True

    def read_holding_registers(self, address, count, unit):
        assert unit == 126
        if self.read_error:
            return Response(error=True)
        return Response([self.registers.get(address + i, 0) for i in range(count)])

    def write_registers(self, address, values, unit):
        assert unit == 126
        if self.write_error:
            return Response(error=True)
        self.writes.append((address, list(values)))
        self.load(address, values)
        return Response()


@pytest.fixture
def se():
    return se_module


@pytest.fixture
def client():
    return FakeModbusClient()


@pytest.fixture
def bridge(se, client):
    bridge = se.SolarEdge(client, 126, 'ModbusTCP test:502, UNIT 126')
    bridge.create_services()
    return bridge


@pytest.fixture(autouse=True)
def restore_root_logger():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
