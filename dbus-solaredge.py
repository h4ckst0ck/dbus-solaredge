#!/usr/bin/env python3
"""Publish SolarEdge inverter and meter data (SunSpec Modbus TCP) on the Victron Venus OS dbus."""

import argparse
import ctypes
import logging
import math
import os
import platform
import sys

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
from pymodbus.client.sync import ModbusTcpClient as ModbusClient
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadBuilder, BinaryPayloadDecoder

# velib_python location differs between Venus OS versions
sys.path.insert(1, '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python')
sys.path.insert(2, '/opt/victronenergy/dbus-modem')
from vedbus import VeDbusService

log = logging.getLogger("DbusSolarEdge")

# ----------------------------------------------------------------
# Configuration (can also be overridden via command line, see --help)
VERSION     = "0.3"
SERVER_HOST = "192.168.178.80"
SERVER_PORT = 502
# setup the SolarEdge inverter based on this guide: https://www.victronenergy.com/live/venus-os:gx_solaredge
UNIT = 126 # From SolarEdge Setapp in Communication -> RS481 -> Protocol -> SunSpec (Non-SE Logger) -> Device ID

# max power (W) of the SolarEdge PV inverter. 0 = read it from the inverter (register 0xF304).
# Set a fixed value (e.g. 25000 for a SE25K) in case the value can not be read via modbus.
MAX_POWER = 0

# publish com.victronenergy.digitalinput service which signals when the inverter is throttled
ENABLE_LIMIT_INPUT = False

UPDATE_INTERVAL_MS = 1000
# stop (and let the supervisor restart us) after this many failed update cycles in a row
MAX_CONSECUTIVE_ERRORS = 30
# ----------------------------------------------------------------

# SolarEdge power control registers
REG_ACTIVE_POWER_LIMIT   = 0xF001 # uint16, %
REG_COMMIT_POWER_CONTROL = 0xF100 # int16, write 1 to commit
REG_ADV_PWR_CONTROL_EN   = 0xF142 # int32
REG_MAX_ACTIVE_POWER     = 0xF304 # float32, W

# SunSpec "not implemented" markers
NOT_IMPLEMENTED_UINT16 = 0xFFFF
NOT_IMPLEMENTED_INT16  = 0x8000

# SunSpec inverter model id -> number of phases
PHASES_BY_MODEL = {101: 1, 102: 2, 103: 3}

# SolarEdge I_Status -> Victron pvinverter /StatusCode (everything else -> 8 = Standby)
VICTRON_PV_STATE = {
    3: 1,   # Grid Monitoring/wake-up -> Startup 1
    4: 11,  # Producing power -> Running (MPPT)
    5: 12,  # Production (curtailed) -> Running (Throttled)
    7: 10,  # Fault -> Error
}


class ModbusError(Exception):
    pass


def _get_string(regs):
    numbers = []
    for x in regs:
        if (((x >> 8) & 0xFF) != 0):
            numbers.append((x >> 8) & 0xFF)
        if (((x >> 0) & 0xFF) != 0):
            numbers.append((x >> 0) & 0xFF)
    return "".join(map(chr, numbers)).strip()

def _get_signed_short(reg):
    return ctypes.c_short(reg).value

def _get_scale_factor(reg):
    return 10**_get_signed_short(reg)

def _uint16(reg, sf):
    return None if reg == NOT_IMPLEMENTED_UINT16 else round(reg * sf, 2)

def _int16(reg, sf):
    return None if reg == NOT_IMPLEMENTED_INT16 else round(_get_signed_short(reg) * sf, 2)

def _negate(value):
    return None if value is None else -value

def _energy_kwh(high, low, sf):
    return float((high << 16) + low) * sf / 1000

def _get_victron_pv_state(state):
    return VICTRON_PV_STATE.get(state, 8)

def _encode(kind, value):
    builder = BinaryPayloadBuilder(byteorder=Endian.Big, wordorder=Endian.Little)
    getattr(builder, 'add_' + kind)(value)
    return builder.to_registers()

def _decode(kind, registers):
    decoder = BinaryPayloadDecoder.fromRegisters(registers, byteorder=Endian.Big, wordorder=Endian.Little)
    return getattr(decoder, 'decode_' + kind)()

def _to_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number

def _text(unit, digits=None):
    def fmt(path, value):
        if value is None:
            return ''
        if digits is not None:
            value = round(value, digits)
        return '%s%s' % (value, unit)
    return fmt

_kwh = _text('kWh', 3)
_a = _text('A', 2)
_w = _text('W', 2)
_v = _text('V', 2)
_c = _text('C')
_pct = _text('%')


# Again not all of these needed this is just duplicating the Victron code.
class SystemBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SYSTEM)

class SessionBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SESSION)

def dbusconnection():
    return SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else SystemBus()


class SolarEdge(object):
    def __init__(self, client, unit, connection, max_power=0):
        self.client = client
        self.unit = unit
        self.connection = connection
        self.fixed_max_power = max_power
        self.max_power = max_power
        self.phases = 3
        self.errors = 0
        self.services = {}
        self.on_fatal = None

    # ---- modbus helpers ----
    def read(self, address, count):
        regs = self.client.read_holding_registers(address, count, unit=self.unit)
        if regs.isError():
            raise ModbusError('read of %d registers at %d failed: %s' % (count, address, regs))
        return regs.registers

    def write(self, address, registers):
        result = self.client.write_registers(address, registers, unit=self.unit)
        if result.isError():
            raise ModbusError('write to %d failed: %s' % (address, result))

    # ---- dbus services ----
    def _new_service(self, type, physical, instance, common, product_id):
        # common = SunSpec common block registers (Manufacturer, Model, Options, Version, Serial)
        service = VeDbusService("com.victronenergy.{}.{}_id00".format(type, physical), dbusconnection())

        # Create the management objects, as specified in the ccgx dbus-api document
        service.add_path('/Mgmt/ProcessName', __file__)
        service.add_path('/Mgmt/ProcessVersion', VERSION + ' on Python ' + platform.python_version())
        service.add_path('/Mgmt/Connection', self.connection)
        service.add_path('/Connected', 1)
        service.add_path('/HardwareVersion', 0)
        service.add_path('/DeviceInstance', instance)
        service.add_path('/ProductId', product_id)
        service.add_path('/ProductName', _get_string(common[0:16]) + " " + _get_string(common[16:32]))
        service.add_path('/FirmwareVersion', _get_string(common[40:48]))
        service.add_path('/DataManagerVersion', VERSION)
        service.add_path('/Serial', _get_string(common[48:64]))
        return service

    def create_services(self):
        meter = self.read(40123, 64)
        inverter = self.read(40004, 64)
        self.phases = PHASES_BY_MODEL.get(self.read(40069, 1)[0], 3)
        log.info('Inverter has %d phase(s)' % self.phases)

        grid = self._new_service('grid', 'grid', 0, meter, 16) # value used in ac_sensor_bridge.cpp of dbus-cgwacs
        grid.add_path('/CustomName', "Grid meter " + _get_string(meter[32:40]))
        grid.add_path('/Ac/Power', None, gettextcallback=_w)
        for phase in ('L1', 'L2', 'L3'):
            grid.add_path('/Ac/%s/Voltage' % phase, None, gettextcallback=_v)
            grid.add_path('/Ac/%s/Current' % phase, None, gettextcallback=_a)
            grid.add_path('/Ac/%s/Power' % phase, None, gettextcallback=_w)
            grid.add_path('/Ac/%s/Energy/Forward' % phase, None, gettextcallback=_kwh)
            grid.add_path('/Ac/%s/Energy/Reverse' % phase, None, gettextcallback=_kwh)
        grid.add_path('/Ac/Energy/Forward', None, gettextcallback=_kwh) # energy bought from the grid
        grid.add_path('/Ac/Energy/Reverse', None, gettextcallback=_kwh) # energy sold to the grid
        self.services['grid'] = grid

        pv = self._new_service('pvinverter.pv0', 'pvinverter', 20, inverter, 41284)
        pv.add_path('/Ac/Energy/Forward', None, gettextcallback=_kwh)
        pv.add_path('/Ac/Power', None, gettextcallback=_w)
        for phase in ('L1', 'L2', 'L3'):
            pv.add_path('/Ac/%s/Current' % phase, None, gettextcallback=_a)
            pv.add_path('/Ac/%s/Energy/Forward' % phase, None, gettextcallback=_kwh)
            pv.add_path('/Ac/%s/Power' % phase, None, gettextcallback=_w)
            pv.add_path('/Ac/%s/Voltage' % phase, None, gettextcallback=_v)
        pv.add_path('/Ac/MaxPower', None, gettextcallback=_w)
        pv.add_path('/ErrorCode', None)
        pv.add_path('/Position', 0)
        pv.add_path('/StatusCode', None)
        pv.add_path('/Ac/PowerLimit', value=None, description='ESS zero feed-in power limit in W', writeable=True, onchangecallback=self._handle_power_limit, gettextcallback=_w)
        pv.add_path('/Ac/AdvancedPwrControlEn', value=None, description='Enable SolarEdge power limitation', writeable=True, onchangecallback=self._handle_adv_pwr_control_en)
        pv.add_path('/Ac/ActivePowerLimit', value=None, description='SolarEdge active power limit in %', writeable=True, onchangecallback=self._handle_active_power_limit, gettextcallback=_pct)
        self.services['pv'] = pv

        temp = self._new_service('temperature', 'temp_pvinverter', 26, inverter, 0)
        temp.add_path('/CustomName', 'PV Inverter Temperature')
        temp.add_path('/Temperature', None, gettextcallback=_c)
        temp.add_path('/Status', 0)
        temp.add_path('/TemperatureType', 2, writeable=True) # 2 = generic
        self.services['temp'] = temp

        if ENABLE_LIMIT_INPUT:
            limit = self._new_service('digitalinput', 'limit_pvinverter', 10, inverter, 0)
            limit.add_path('/CustomName', 'PV Inverter Limiter active')
            limit.add_path('/State', None)
            limit.add_path('/Status', 0)
            limit.add_path('/Type', 2, writeable=True)
            limit.add_path('/Alarm', None, writeable=True)
            self.services['limit'] = limit

        if self.fixed_max_power:
            log.info('Inverter maxPower manually set to %s W' % self.fixed_max_power)

    # ---- cyclic update ----
    def update(self):
        try:
            self._update_grid()
            self._update_inverter()
            self._update_power_control()
        except Exception:
            self.errors += 1
            if self.errors == 1:
                log.error('update failed, will retry', exc_info=True)
                self._set_connected(0)
            if self.errors >= MAX_CONSECUTIVE_ERRORS:
                log.error('%d consecutive update errors, giving up' % self.errors)
                if self.on_fatal:
                    self.on_fatal()
                return False
            return True

        if self.errors:
            log.info('update succeeded again after %d error(s)' % self.errors)
            self.errors = 0
            self._set_connected(1)
        return True

    def _set_connected(self, value):
        for service in self.services.values():
            service['/Connected'] = value

    def _update_grid(self):
        grid = self.services['grid']
        r = self.read(40190, 70)
        sf_i = _get_scale_factor(r[4])
        sf_u = _get_scale_factor(r[13])
        sf_p = _get_scale_factor(r[20])
        sf_e = _get_scale_factor(r[52])
        grid['/Ac/Power'] = _negate(_int16(r[16], sf_p))
        grid['/Ac/Energy/Reverse'] = _energy_kwh(r[36], r[37], sf_e)
        grid['/Ac/Energy/Forward'] = _energy_kwh(r[44], r[45], sf_e)
        for i, phase in enumerate(('L1', 'L2', 'L3')):
            grid['/Ac/%s/Current' % phase] = _int16(r[1 + i], sf_i)
            grid['/Ac/%s/Voltage' % phase] = _int16(r[6 + i], sf_u)
            grid['/Ac/%s/Power' % phase] = _negate(_int16(r[17 + i], sf_p))
            grid['/Ac/%s/Energy/Reverse' % phase] = _energy_kwh(r[38 + 2 * i], r[39 + 2 * i], sf_e)
            grid['/Ac/%s/Energy/Forward' % phase] = _energy_kwh(r[46 + 2 * i], r[47 + 2 * i], sf_e)

    def _update_inverter(self):
        pv = self.services['pv']
        r = self.read(40071, 38)
        sf_i = _get_scale_factor(r[4])
        sf_u = _get_scale_factor(r[11])
        power = _int16(r[12], _get_scale_factor(r[13]))
        energy = _energy_kwh(r[22], r[23], _get_scale_factor(r[24]))
        pv['/Ac/Power'] = power
        pv['/Ac/Energy/Forward'] = energy
        for i, phase in enumerate(('L1', 'L2', 'L3')):
            active = i < self.phases
            # per phase power/energy is not available, so the total is split evenly
            pv['/Ac/%s/Current' % phase] = _uint16(r[1 + i], sf_i) if active else None
            pv['/Ac/%s/Voltage' % phase] = _uint16(r[8 + i], sf_u) if active else None
            pv['/Ac/%s/Power' % phase] = round(power / self.phases, 2) if active and power is not None else None
            pv['/Ac/%s/Energy/Forward' % phase] = energy / self.phases if active else None

        pv['/StatusCode'] = _get_victron_pv_state(r[36])
        pv['/ErrorCode'] = r[37]

        self.services['temp']['/Temperature'] = _int16(r[32], _get_scale_factor(r[35]))

        if 'limit' in self.services:
            limit = self.services['limit']
            throttled = r[36] == 5 and (power or 0) > 100
            limit['/State'] = 3 if throttled else 2
            limit['/Alarm'] = 2 if throttled else 0

    def _update_power_control(self):
        pv = self.services['pv']
        pv['/Ac/AdvancedPwrControlEn'] = _decode('32bit_int', self.read(REG_ADV_PWR_CONTROL_EN, 2))

        if not self.fixed_max_power:
            max_power = _decode('32bit_float', self.read(REG_MAX_ACTIVE_POWER, 2))
            if max_power != self.max_power:
                log.info('Inverter maxPower received: %s W' % max_power)
            self.max_power = max_power
        pv['/Ac/MaxPower'] = self.max_power

        limit_rel = _decode('16bit_uint', self.read(REG_ACTIVE_POWER_LIMIT, 1)) # SolarEdge relative power limit in %
        pv['/Ac/ActivePowerLimit'] = limit_rel
        pv['/Ac/PowerLimit'] = int(self.max_power * limit_rel / 100) # ESS dynamic zero feed-in power limit in W

    # ---- dbus write handlers ----
    def _write_active_power_limit(self, percent):
        self.write(REG_ACTIVE_POWER_LIMIT, _encode('16bit_uint', percent))
        self.write(REG_COMMIT_POWER_CONTROL, _encode('16bit_int', 1))

    def _handle_adv_pwr_control_en(self, path, value):
        log.info("someone else updated %s to %s" % (path, value))
        number = _to_number(value)
        if number not in (0, 1):
            log.warning('%s: invalid value %s (allowed: 0, 1)' % (path, value))
            return False
        try:
            self.write(REG_ADV_PWR_CONTROL_EN, _encode('32bit_int', int(number)))
        except Exception:
            log.error('writing %s failed' % path, exc_info=True)
            return False
        return True # accept the change

    def _handle_power_limit(self, path, value):
        if self.max_power <= 0:
            log.warning('%s: maxPower is unknown, unable to set power limit' % path)
            return False
        watts = _to_number(value)
        if watts is None:
            log.warning('%s: invalid value %s' % (path, value))
            return False

        # calculate relative value for SolarEdge
        percent = min(100, max(0, int(math.ceil(100 * watts / self.max_power))))
        try:
            if self.services['pv']['/Ac/AdvancedPwrControlEn'] != 1:
                log.info('enabling AdvancedPwrControl')
                self.write(REG_ADV_PWR_CONTROL_EN, _encode('32bit_int', 1))
            self._write_active_power_limit(percent)
        except Exception:
            log.error('writing %s failed' % path, exc_info=True)
            return False
        return True # accept the change

    def _handle_active_power_limit(self, path, value):
        log.info("someone else updated %s to %s" % (path, value))
        number = _to_number(value)
        if number is None or number < 0 or number > 100:
            log.warning('%s: value %s out of range (0 <= value <= 100)' % (path, value))
            return False
        try:
            self._write_active_power_limit(int(round(number)))
        except Exception:
            log.error('writing %s failed' % path, exc_info=True)
            return False
        return True # accept the change


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=SERVER_HOST, help='IP address of the SolarEdge inverter')
    parser.add_argument('--port', type=int, default=SERVER_PORT, help='Modbus TCP port')
    parser.add_argument('--unit', type=int, default=UNIT, help='Modbus device id')
    parser.add_argument('--max-power', type=float, default=MAX_POWER, help='fixed inverter max power in W (0 = read from inverter)')
    args = parser.parse_args()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
    DBusGMainLoop(set_as_default=True)

    connection = "ModbusTCP %s:%d, UNIT %d" % (args.host, args.port, args.unit)
    log.info('Startup, trying connection to Modbus-Server: ' + connection)

    client = ModbusClient(args.host, port=args.port, retry_on_empty=True)
    if not client.connect():
        log.error("unable to connect to %s:%d" % (args.host, args.port))
        sys.exit(1)
    log.info('Connected to Modbus Server.')

    solaredge = SolarEdge(client, args.unit, connection, args.max_power)
    solaredge.create_services()

    mainloop = GLib.MainLoop()
    solaredge.on_fatal = mainloop.quit

    # Everything done so just set a time to run an update function to update the data values every second.
    GLib.timeout_add(UPDATE_INTERVAL_MS, solaredge.update)

    log.info('Connected to dbus, and switching over to GLib.MainLoop() (= event based)')
    mainloop.run()

    # only reached after too many errors, the supervisor will restart us
    client.close()
    sys.exit(1)


if __name__ == '__main__':
    main()
