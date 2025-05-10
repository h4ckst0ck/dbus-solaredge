#!/usr/bin/env python
 
# probably not all these required some are legacy and no longer used.
from dbus.mainloop.glib import DBusGMainLoop

try:
  import gobject  # Python 2.x
except:
  from gi.repository import GLib as gobject # Python 3.x

# from gobject import idle_add

import dbus
import dbus.service
import inspect
import platform
from threading import Timer
import argparse
import logging
import sys
import os
import math
import requests # for http GET
# from pyModbusTCP.client import ModbusClient
from pymodbus.client.sync import ModbusTcpClient as ModbusClient
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadDecoder
from pymodbus.payload import BinaryPayloadBuilder

import time
import ctypes

log = logging.getLogger("DbusSolarEdge")

sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-modem'))
from vedbus import VeDbusService

# ----------------------------------------------------------------
VERSION     = "0.2"
SERVER_PORT = 502
# setup the SolarEdge inverter based on this guide: https://www.victronenergy.com/live/venus-os:gx_solaredge
UNIT = 126 # From SolarEdge Setapp in Communication -> RS481 -> Protocol -> SunSpec (Non-SE Logger) -> Device ID
# ----------------------------------------------------------------
CONNECTION  = "ModbusTCP " + SERVER_HOST + ":" + str(SERVER_PORT) + ", UNIT " + str(UNIT)

# max power (W) of the SolarEdge PV inverter (will be initialized later and read out from SE)
# can be set to a fixed value (e.g. 25000 for a SE25K) in case the value can not get via modbus
maxPower = 0 

# Have a mainloop, so we can send/receive asynchronous calls to and from dbus
DBusGMainLoop(set_as_default=True)

root = logging.getLogger()
root.setLevel(logging.INFO)

handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
root.addHandler(handler)

log.info('Startup, trying connection to Modbus-Server: '+ CONNECTION)

modbusClient = ModbusClient(SERVER_HOST, port=SERVER_PORT, retry_on_empty=True ) 

modbusClient.auto_open=True

if not modbusClient.is_socket_open():
    if not modbusClient.connect():
        log.error("unable to connect to "+SERVER_HOST+":"+str(SERVER_PORT))
        sys.exit()

log.info('Connected to Modbus Server.')

if (maxPower != 0):
    log.info('Inverter maxPower manually set to %s W' % maxPower)

def _get_string(regs):
    numbers = []
    for x in regs:
        if (((x >> 8) & 0xFF) != 0):
            numbers.append((x >> 8) & 0xFF)
        if (((x >> 0) & 0xFF) != 0):
            numbers.append((x >> 0) & 0xFF)
    return ("".join(map(chr, numbers)))

def _get_signed_short(regs):
    return ctypes.c_short(regs).value

def _get_scale_factor(regs):
    return 10**_get_signed_short(regs)

def _get_victron_pv_state(state):
    if (state == 1):
        return 0 
    elif (state == 3):
        return 1
    elif (state == 4):
        return 11
    elif (state == 5):
        return 12
    elif (state == 7):
        return 10
    else:
        return 8

# Again not all of these needed this is just duplicating the Victron code.
class SystemBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SYSTEM)
 
class SessionBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SESSION)
 
def dbusconnection():
    return SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else SystemBus()
 

def _update():
    global maxPower
    try:
        regs = modbusClient.read_holding_registers(40190, 70, unit=UNIT)

        if regs.isError():
            log.error('regs.isError: '+str(regs))
            sys.exit()                                                                             
        else:
           sf = _get_scale_factor(regs.registers[4])
           dbusservice['grid']['/Ac/L1/Current'] = round(_get_signed_short(regs.registers[1]) * sf, 2)
           dbusservice['grid']['/Ac/L2/Current'] = round(_get_signed_short(regs.registers[2]) * sf, 2)
           dbusservice['grid']['/Ac/L3/Current'] = round(_get_signed_short(regs.registers[3]) * sf, 2)
           sf = _get_scale_factor(regs.registers[13])
           dbusservice['grid']['/Ac/L1/Voltage'] = round(_get_signed_short(regs.registers[6]) * sf, 2)
           dbusservice['grid']['/Ac/L2/Voltage'] = round(_get_signed_short(regs.registers[7]) * sf, 2)
           dbusservice['grid']['/Ac/L3/Voltage'] = round(_get_signed_short(regs.registers[8]) * sf, 2)
           sf = _get_scale_factor(regs.registers[20])
           dbusservice['grid']['/Ac/Power'] = round(_get_signed_short(regs.registers[16]) * sf * -1, 2)
           dbusservice['grid']['/Ac/L1/Power'] = round(_get_signed_short(regs.registers[17]) * sf * -1, 2)
           dbusservice['grid']['/Ac/L2/Power'] = round(_get_signed_short(regs.registers[18]) * sf * -1, 2)
           dbusservice['grid']['/Ac/L3/Power'] = round(_get_signed_short(regs.registers[19]) * sf * -1, 2)
           sf = _get_scale_factor(regs.registers[52])
           dbusservice['grid']['/Ac/Energy/Reverse'] = float((regs.registers[36] << 16) + regs.registers[37]) * sf / 1000
           dbusservice['grid']['/Ac/L1/Energy/Reverse'] = float((regs.registers[38] << 16) + regs.registers[39]) * sf / 1000
           dbusservice['grid']['/Ac/L2/Energy/Reverse'] = float((regs.registers[40] << 16) + regs.registers[41]) * sf / 1000
           dbusservice['grid']['/Ac/L3/Energy/Reverse'] = float((regs.registers[42] << 16) + regs.registers[43]) * sf / 1000
           dbusservice['grid']['/Ac/Energy/Forward'] = float((regs.registers[44] << 16) + regs.registers[45]) * sf / 1000
           dbusservice['grid']['/Ac/L1/Energy/Forward'] = float((regs.registers[46] << 16) + regs.registers[47]) * sf / 1000
           dbusservice['grid']['/Ac/L2/Energy/Forward'] = float((regs.registers[48] << 16) + regs.registers[49]) * sf / 1000
           dbusservice['grid']['/Ac/L3/Energy/Forward'] = float((regs.registers[50] << 16) + regs.registers[51]) * sf / 1000

        # read registers, store result in regs list
        regs = modbusClient.read_holding_registers(40071, 38, unit=UNIT)

        if regs.isError():
            log.error('regs.isError: '+str(regs))
            sys.exit()                                                                             
        else:
           sf = _get_scale_factor(regs.registers[4])
           dbusservice['pvinverter.pv0']['/Ac/L1/Current'] = round(regs.registers[1] * sf, 2)
           dbusservice['pvinverter.pv0']['/Ac/L2/Current'] = round(regs.registers[2] * sf, 2)
           dbusservice['pvinverter.pv0']['/Ac/L3/Current'] = round(regs.registers[3] * sf, 2)
           sf = _get_scale_factor(regs.registers[11])
           dbusservice['pvinverter.pv0']['/Ac/L1/Voltage'] = round(regs.registers[8] * sf, 2)
           dbusservice['pvinverter.pv0']['/Ac/L2/Voltage'] = round(regs.registers[9] * sf, 2)
           dbusservice['pvinverter.pv0']['/Ac/L3/Voltage'] = round(regs.registers[10] * sf, 2)
           sf = _get_scale_factor(regs.registers[13])
           acpower = _get_signed_short(regs.registers[12]) * sf
           dbusservice['pvinverter.pv0']['/Ac/Power'] = acpower
           dbusservice['pvinverter.pv0']['/Ac/L1/Power'] = round(_get_signed_short(regs.registers[12]) * sf / 3, 2)
           dbusservice['pvinverter.pv0']['/Ac/L2/Power'] = round(_get_signed_short(regs.registers[12]) * sf / 3, 2)
           dbusservice['pvinverter.pv0']['/Ac/L3/Power'] = round(_get_signed_short(regs.registers[12]) * sf / 3, 2)
           sf = _get_scale_factor(regs.registers[24])
           dbusservice['pvinverter.pv0']['/Ac/Energy/Forward'] = float((regs.registers[22] << 16) + regs.registers[23]) * sf / 1000
           dbusservice['pvinverter.pv0']['/Ac/L1/Energy/Forward'] = float((regs.registers[22] << 16) + regs.registers[23]) * sf / 3 / 1000
           dbusservice['pvinverter.pv0']['/Ac/L2/Energy/Forward'] = float((regs.registers[22] << 16) + regs.registers[23]) * sf / 3 / 1000
           dbusservice['pvinverter.pv0']['/Ac/L3/Energy/Forward'] = float((regs.registers[22] << 16) + regs.registers[23]) * sf / 3 / 1000
           
           dbusservice['pvinverter.pv0']['/StatusCode'] = _get_victron_pv_state(regs.registers[36])
           dbusservice['pvinverter.pv0']['/ErrorCode'] = regs.registers[37]

           sf = _get_scale_factor(regs.registers[35])
           dbusservice['adc-temp0']['/Temperature'] = round(regs.registers[32] * sf, 2)

           if ((regs.registers[36] == 5) & (acpower > 100)):
               dbusservice['digitalinput0']['/State'] = 3
               dbusservice['digitalinput0']['/Alarm'] = 2
           else:
               dbusservice['digitalinput0']['/State'] = 2
               dbusservice['digitalinput0']['/Alarm'] = 0

           # Read AdvancedPwrControlEn
           regs = modbusClient.read_holding_registers( address=0xF142, count=2, unit=UNIT )

           if regs.isError():
               log.error('regs.isError: '+str(regs))
               sys.exit()
           else:
               # log.info("_update(): enable registers %s" % regs.registers )
               decoder = BinaryPayloadDecoder.fromRegisters( regs.registers, byteorder=Endian.Big, wordorder=Endian.Little )
               dbusservice['pvinverter.pv0']['/Ac/AdvancedPwrControlEn'] = decoder.decode_32bit_int()

           # Read MaxActivePower
           regs = modbusClient.read_holding_registers( address=0xF304, count=2, unit=UNIT )

           if regs.isError():
               log.error('regs.isError: '+str(regs))
               sys.exit()
           else:
               decoder = BinaryPayloadDecoder.fromRegisters( regs.registers, byteorder=Endian.Big, wordorder=Endian.Little )
               decodedMaxPower = decoder.decode_32bit_float()

               if maxPower != decodedMaxPower:
                  log.info('Inverter maxPower received: %s W' % decodedMaxPower)

               maxPower = decodedMaxPower
               dbusservice['pvinverter.pv0']['/Ac/MaxPower'] = decodedMaxPower

           # Read power limiter register
           regs = modbusClient.read_holding_registers( address=0xF001, count=1, unit=UNIT )

           if regs.isError():
               log.error('regs.isError: '+str(regs))
               sys.exit()
           else:
               decoder = BinaryPayloadDecoder.fromRegisters( regs.registers, byteorder=Endian.Big, wordorder=Endian.Little )
               powerlimitRel= decoder.decode_16bit_uint() # SolarEdge relative power limit in %
               dbusservice['pvinverter.pv0']['/Ac/ActivePowerLimit'] = powerlimitRel
               powerlimit= int(maxPower * powerlimitRel / 100) # calculate absolute limit in W
               dbusservice['pvinverter.pv0']['/Ac/PowerLimit'] = powerlimit # ESS dynamic zero feed-in power limit

    except:
        log.error('exception in _update.',exc_info=1)
        sys.exit()

    return True

def _handleAdvancedPwrControlEn(path, value):
    log.info("_handleAdvancedPwrControlEn(): someone else updated %s to %s" % (path, value))

    try:
        builder = BinaryPayloadBuilder( byteorder=Endian.Big, wordorder=Endian.Little )
        builder.add_32bit_int( value )
        # log.info("_handleAdvancedPwrControlEn(): registers %s" % builder.to_registers() )
        result = modbusClient.write_registers( address=0xF142, values=builder.to_registers(), unit=UNIT )

        if result.isError():
            log.error('result.isError(): '+str(result))

    except Exception as e:
        log.error('Exception in _handleAdvancedPwrControlEn(): %s' % e )
        sys.exit()

    return True # accept the change

def _handlePowerLimit(path, value):
    if (maxPower==0):
        log.warning('_handlePowerLimit(): maxPower is unknown, unable to set power limit')
        return false;

    # calculate relative value for SolarEdge
    relVal=min(100,max(0,int(math.ceil(100 * value / maxPower))));
    #log.info("_handlePowerLimit(): someone else updated %s to %s (%s)" % (path, value, relVal))

    if ((relVal < 0) or (relVal > 100)):
        log.warning('_handlePowerLimit(): value %s (%s) out of scope (0 <= value <= 100' % ( relVal, value) )
        return False

    try:
        # Enable AdvancedPwrControl
        builder = BinaryPayloadBuilder( byteorder=Endian.Big, wordorder=Endian.Little )
        builder.add_32bit_int( 1 )
        # log.info("_handleAdvancedPwrControlEn(): registers %s" % builder.to_registers() )
        result = modbusClient.write_registers( address=0xF142, values=builder.to_registers(), unit=UNIT )

        if result.isError():
            log.error('result.isError(): '+str(result))
            
        # Write new Power Limit
        builder = BinaryPayloadBuilder( byteorder=Endian.Big, wordorder=Endian.Little )
        builder.add_16bit_uint( relVal )
        # log.info("_handlePowerLimit(): write registers %s" % builder.to_registers() )
        result = modbusClient.write_registers( address=0xF001, values=builder.to_registers(), unit=UNIT )

        if result.isError():
            log.error('result.isError(): '+str(result))

        # Commit new Power Limit
        builder = BinaryPayloadBuilder( byteorder=Endian.Big, wordorder=Endian.Little )
        builder.add_16bit_int( 1 )
        # log.info("_handlePowerLimit(): commit registers %s" % builder.to_registers() )
        result = modbusClient.write_registers( address=0xF100, values=builder.to_registers(), unit=UNIT )

        if result.isError():
            log.error('result.isError(): '+str(result))


    except Exception as e:
        log.error('Exception in _handlePowerLimit(): %s' % e )
        sys.exit()

    return True # accept the change

def _handleActivePowerLimit(path, value):
    log.info("_handleActivePowerLimit(): someone else updated %s to %s" % (path, value))
    if ((value < 0) or (value > 100)):
        log.warning("_handleActivePowerLimit(): value out of scope (0 <= value <= 100" )
        return False

    try:
        # Write new Active Power Limit
        builder = BinaryPayloadBuilder( byteorder=Endian.Big, wordorder=Endian.Little )
        builder.add_16bit_uint( value )
        # log.info("_handleActivePowerLimit(): write registers %s" % builder.to_registers() )
        result = modbusClient.write_registers( address=0xF001, values=builder.to_registers(), unit=UNIT )

        if result.isError():
            log.error('result.isError(): '+str(result))

        # Commit new Active Power Limit
        builder = BinaryPayloadBuilder( byteorder=Endian.Big, wordorder=Endian.Little )
        builder.add_16bit_int( 1 )
        # log.info("_handleActivePowerLimit(): commit registers %s" % builder.to_registers() )
        result = modbusClient.write_registers( address=0xF100, values=builder.to_registers(), unit=UNIT )

        if result.isError():
            log.error('result.isError(): '+str(result))


    except Exception as e:
        log.error('Exception in _handleActivePowerLimit(): %s' % e )
        sys.exit()

    return True # accept the change


# Here is the bit you need to create multiple new services - try as much as possible timplement the Victron Dbus API requirements.
def new_service(base, type, physical, id, instance):
    self =  VeDbusService("{}.{}.{}_id{:02d}".format(base, type, physical,  id), dbusconnection())

    # Create the management objects, as specified in the ccgx dbus-api document
    self.add_path('/Mgmt/ProcessName', __file__)
    self.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' + platform.python_version())
    self.add_path('/Connected', 1)  
    self.add_path('/HardwareVersion', 0)

    _kwh = lambda p, v: (str(round(v,3)) + 'kWh')
    _a = lambda p, v: (str(round(v,2)) + 'A')
    _w = lambda p, v: (str(round(v,2)) + 'W')
    _v = lambda p, v: (str(round(v,2)) + 'V')
    _c = lambda p, v: (str(v) + 'C')
    _p = lambda p, v: (str(min(100,max(0,int(math.ceil(100 * v / maxPower))))) + '%') if maxPower>0 else 'null'

    # Create device type specific objects
    if physical == 'grid':
        # if open() is ok, read register (modbus function 0x03)
        if modbusClient.is_socket_open():
            # read registers, store result in regs list
            regs = modbusClient.read_holding_registers(40123, 64, unit=UNIT)
            if regs.isError():
                log.error('regs.isError: '+str(regs))
                sys.exit()                                                                             
            else:
                self.add_path('/DeviceInstance', instance)
                self.add_path('/FirmwareVersion', _get_string(regs.registers[40:47]))
                self.add_path('/DataManagerVersion', VERSION)
                self.add_path('/Serial', _get_string(regs.registers[48:63]))
                self.add_path('/Mgmt/Connection', CONNECTION)
                self.add_path('/ProductId', 16) # value used in ac_sensor_bridge.cpp of dbus-cgwacs
                self.add_path('/ProductName',  _get_string(regs.registers[0:15])+" "+_get_string(regs.registers[16:31]))
                self.add_path('/CustomName', "Grid meter " +_get_string(regs.registers[32:39]))
                self.add_path('/Ac/Power', None, gettextcallback=_w)
                self.add_path('/Ac/L1/Voltage', None, gettextcallback=_v)
                self.add_path('/Ac/L2/Voltage', None, gettextcallback=_v)
                self.add_path('/Ac/L3/Voltage', None, gettextcallback=_v)
                self.add_path('/Ac/L1/Current', None, gettextcallback=_a)
                self.add_path('/Ac/L2/Current', None, gettextcallback=_a)
                self.add_path('/Ac/L3/Current', None, gettextcallback=_a)
                self.add_path('/Ac/L1/Power', None, gettextcallback=_w)
                self.add_path('/Ac/L2/Power', None, gettextcallback=_w) 
                self.add_path('/Ac/L3/Power', None, gettextcallback=_w) 
                self.add_path('/Ac/L1/Energy/Forward', None, gettextcallback=_kwh)
                self.add_path('/Ac/L2/Energy/Forward', None, gettextcallback=_kwh)
                self.add_path('/Ac/L3/Energy/Forward', None, gettextcallback=_kwh)
                self.add_path('/Ac/L1/Energy/Reverse', None, gettextcallback=_kwh)
                self.add_path('/Ac/L2/Energy/Reverse', None, gettextcallback=_kwh)
                self.add_path('/Ac/L3/Energy/Reverse', None, gettextcallback=_kwh)
                self.add_path('/Ac/Energy/Forward', None, gettextcallback=_kwh) # energy bought from the grid
                self.add_path('/Ac/Energy/Reverse', None, gettextcallback=_kwh) # energy sold to the grid

    if physical == 'pvinverter':
        # if open() is ok, read register (modbus function 0x03)
        if modbusClient.is_socket_open():
            # read registers, store result in regs list
            regs = modbusClient.read_holding_registers(40004, 56, unit=UNIT)
            if regs.isError():
                log.error('regs.isError: '+str(regs))
                sys.exit()                                                                             
            else:   
                self.add_path('/DeviceInstance', instance)
                self.add_path('/FirmwareVersion', _get_string(regs.registers[32:47]))
                self.add_path('/DataManagerVersion', VERSION)
                self.add_path('/Serial', _get_string(regs.registers[48:55]))
                self.add_path('/Mgmt/Connection', CONNECTION)
                self.add_path('/ProductId', 41284) # value used in ac_sensor_bridge.cpp of dbus-cgwacs
                self.add_path('/ProductName', _get_string(regs.registers[0:15])+" "+_get_string(regs.registers[16:31]))
                self.add_path('/Ac/Energy/Forward', None, gettextcallback=_kwh)
                self.add_path('/Ac/Power', None, gettextcallback=_w)
                self.add_path('/Ac/L1/Current', None, gettextcallback=_a)
                self.add_path('/Ac/L2/Current', None, gettextcallback=_a)
                self.add_path('/Ac/L3/Current', None, gettextcallback=_a)
                self.add_path('/Ac/L1/Energy/Forward', None, gettextcallback=_kwh)
                self.add_path('/Ac/L2/Energy/Forward', None, gettextcallback=_kwh)
                self.add_path('/Ac/L3/Energy/Forward', None, gettextcallback=_kwh)
                self.add_path('/Ac/L1/Power', None, gettextcallback=_w)
                self.add_path('/Ac/L2/Power', None, gettextcallback=_w)
                self.add_path('/Ac/L3/Power', None, gettextcallback=_w)
                self.add_path('/Ac/L1/Voltage', None, gettextcallback=_v)
                self.add_path('/Ac/L2/Voltage', None, gettextcallback=_v)
                self.add_path('/Ac/L3/Voltage', None, gettextcallback=_v)
                self.add_path('/Ac/MaxPower', None, gettextcallback=_w)
                self.add_path('/ErrorCode', None)
                self.add_path('/Position', 0)
                self.add_path('/StatusCode', None)
                self.add_path('/Ac/PowerLimit', value=None, description='ESS zero feed-in power limit in W', writeable=True, onchangecallback=_handlePowerLimit, gettextcallback=_w )
                self.add_path('/Ac/AdvancedPwrControlEn', value=None, description='Enable SolarEdge power limitation', writeable=True, onchangecallback=_handleAdvancedPwrControlEn )
                self.add_path('/Ac/ActivePowerLimit', value=None, description='SolarEdge active power limit in %', writeable=True, onchangecallback=_handleActivePowerLimit, gettextcallback=_p )

    if physical == 'temp_pvinverter':
        # if open() is ok, read register (modbus function 0x03)
        if modbusClient.is_socket_open():
            # read registers, store result in regs list
            regs = modbusClient.read_holding_registers(40004, 56, unit=UNIT)
            if regs.isError():
                log.error('regs.isError: '+str(regs))
                sys.exit()                                                                             
            else:   
                self.add_path('/DeviceInstance', instance)
                self.add_path('/FirmwareVersion', _get_string(regs.registers[32:47]))
                self.add_path('/DataManagerVersion', VERSION)
                self.add_path('/Serial', _get_string(regs.registers[48:55]))
                self.add_path('/Mgmt/Connection', CONNECTION)
                self.add_path('/ProductName', _get_string(regs.registers[0:15])+" "+_get_string(regs.registers[16:31]))
                self.add_path('/ProductId', 0) 
                self.add_path('/CustomName', 'PV Inverter Temperature')
                self.add_path('/Temperature', None, gettextcallback=_c)
                self.add_path('/Status', 0)
                self.add_path('/TemperatureType', 0, writeable=True)

    if physical == 'limit_pvinverter':
        # if open() is ok, read register (modbus function 0x03)
        if modbusClient.is_socket_open():
            # read registers, store result in regs list
            regs = modbusClient.read_holding_registers(40004, 56, unit=UNIT)
            if regs.isError():
                log.error('regs.isError: '+str(regs))
                sys.exit()                                                                             
            else:   
                self.add_path('/DeviceInstance', instance)
                self.add_path('/FirmwareVersion', _get_string(regs.registers[32:47]))
                self.add_path('/DataManagerVersion', VERSION)
                self.add_path('/Serial', _get_string(regs.registers[48:55]))
                self.add_path('/Mgmt/Connection', CONNECTION)
                self.add_path('/ProductName', _get_string(regs.registers[0:15])+" "+_get_string(regs.registers[16:31]))
                self.add_path('/ProductId', 0) 
                self.add_path('/CustomName', 'PV Inverter Limiter active')
                self.add_path('/State', None)
                self.add_path('/Status', 0)
                self.add_path('/Type', 2, writeable=True)
                self.add_path('/Alarm', None, writeable=True)

    return self

dbusservice = {} # Dictonary to hold the multiple services
 
base = 'com.victronenergy'

# service defined by (base*, type*, id*, instance):
# * items are include in service name
# Create all the dbus-services we want
dbusservice['grid']           = new_service(base, 'grid',           'grid',              0, 0)
dbusservice['pvinverter.pv0'] = new_service(base, 'pvinverter.pv0', 'pvinverter',        0, 20)
dbusservice['adc-temp0']      = new_service(base, 'temperature',    'temp_pvinverter',   0, 26)
dbusservice['digitalinput0']  = new_service(base, 'digitalinput',   'limit_pvinverter',  0, 10)

# Everything done so just set a time to run an update function to update the data values every second.
gobject.timeout_add(1000, _update)

log.info('Connected to dbus, and switching over to GLib.MainLoop() (= event based)')

mainloop = gobject.MainLoop()
mainloop.run()
