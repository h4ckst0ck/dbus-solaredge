"""Access to a SolarEdge inverter and its energy meter via SunSpec Modbus TCP.

This module has no dependency on the D-Bus, it only reads and writes Modbus registers.
Register addresses: see sunspec.txt and the SolarEdge technical notes
"SunSpec Logging in SolarEdge Inverters" and "Power Control Open Protocol for SolarEdge Inverters".
"""

import collections
import logging
import time

from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadBuilder, BinaryPayloadDecoder

import sunspec

log = logging.getLogger(__name__)

# SunSpec register map (holding registers, 0-based addresses as used by SolarEdge)
INVERTER_COMMON = 40004
INVERTER_MODEL_ID = 40069
INVERTER_DATA = 40071
INVERTER_DATA_LENGTH = 38
METER_COMMON = 40123
METER_DATA = 40190
METER_DATA_LENGTH = 70

# SunSpec inverter model id -> number of phases
PHASES_BY_MODEL = {101: 1, 102: 2, 103: 3}

# SolarEdge I_Status values
STATUS_OFF = 1
STATUS_SLEEPING = 2
STATUS_STARTING = 3
STATUS_MPPT = 4
STATUS_THROTTLED = 5
STATUS_SHUTTING_DOWN = 6
STATUS_FAULT = 7
STATUS_STANDBY = 8

# SolarEdge power control registers
REG_ACTIVE_POWER_LIMIT = 0xF001         # uint16 %, dynamic (not stored, no commit needed)
REG_COMMIT_POWER_CONTROL = 0xF100       # int16, 1 = commit settings 0xF102 and following
REG_ADV_PWR_CONTROL_EN = 0xF142         # int32, 0/1
REG_ENABLE_DYNAMIC_POWER_CONTROL = 0xF300  # uint16, 0/1
REG_MAX_ACTIVE_POWER = 0xF304           # float32 W, read only
REG_COMMAND_TIMEOUT = 0xF310            # uint32 s
REG_FALLBACK_ACTIVE_POWER_LIMIT = 0xF312  # float32 %
REG_ACTIVE_POWER_RAMP_UP = 0xF318       # float32 %/min, -1 = disabled
REG_ACTIVE_POWER_RAMP_DOWN = 0xF31A     # float32 %/min, -1 = disabled
REG_DYNAMIC_ACTIVE_POWER_LIMIT = 0xF322  # float32 %, dynamic (not stored)

REGISTER_COUNT = {'16bit_uint': 1, '16bit_int': 1, '32bit_uint': 2, '32bit_int': 2, '32bit_float': 2}

PhaseReading = collections.namedtuple('PhaseReading', 'current voltage power energy_forward energy_reverse')
InverterReading = collections.namedtuple('InverterReading',
	'power energy phases status error_code temperature')
MeterReading = collections.namedtuple('MeterReading', 'power energy_forward energy_reverse phases')


class ModbusError(Exception):
	pass


class ModbusDevice(object):
	"""Register access to one Modbus unit. SolarEdge uses big endian registers, low word first."""

	def __init__(self, client, unit):
		self.client = client
		self.unit = unit

	def read(self, address, count):
		response = self.client.read_holding_registers(address, count, unit=self.unit)
		if response.isError():
			raise ModbusError('reading %d registers at 0x%04X failed: %s' % (count, address, response))
		return response.registers

	def write(self, address, registers):
		response = self.client.write_registers(address, registers, unit=self.unit)
		if response.isError():
			raise ModbusError('writing registers at 0x%04X failed: %s' % (address, response))

	@staticmethod
	def encode(kind, value):
		builder = BinaryPayloadBuilder(byteorder=Endian.Big, wordorder=Endian.Little)
		getattr(builder, 'add_' + kind)(value)
		return builder.to_registers()

	@staticmethod
	def decode(kind, registers):
		decoder = BinaryPayloadDecoder.fromRegisters(registers, byteorder=Endian.Big, wordorder=Endian.Little)
		return getattr(decoder, 'decode_' + kind)()

	def read_value(self, address, kind):
		return self.decode(kind, self.read(address, REGISTER_COUNT[kind]))

	def write_value(self, address, kind, value):
		self.write(address, self.encode(kind, value))


class Inverter(object):
	"""SolarEdge PV inverter (SunSpec model 101/102/103)."""

	def __init__(self, modbus):
		self.modbus = modbus
		self.info = None
		self.phases = 3

	def read_info(self):
		self.info = sunspec.parse_common_block(self.modbus.read(INVERTER_COMMON, sunspec.COMMON_BLOCK_LENGTH))
		self.phases = PHASES_BY_MODEL.get(self.modbus.read(INVERTER_MODEL_ID, 1)[0], 3)
		return self.info

	def read(self):
		r = self.modbus.read(INVERTER_DATA, INVERTER_DATA_LENGTH)
		sf_current = sunspec.scale_factor(r[4])
		sf_voltage = sunspec.scale_factor(r[11])
		power = sunspec.int16_value(r[12], sunspec.scale_factor(r[13]))
		energy = sunspec.acc32_kwh(r[22], r[23], sunspec.scale_factor(r[24]))

		phases = []
		for i in range(self.phases):
			# the inverter does not report power and energy per phase, the total is split evenly
			phases.append(PhaseReading(
				current=sunspec.uint16_value(r[1 + i], sf_current),
				voltage=sunspec.uint16_value(r[8 + i], sf_voltage),
				power=None if power is None else round(power / self.phases, 2),
				energy_forward=energy / self.phases,
				energy_reverse=None))

		return InverterReading(
			power=power,
			energy=energy,
			phases=phases,
			status=r[36],
			error_code=r[37],
			temperature=sunspec.int16_value(r[32], sunspec.scale_factor(r[35])))

	def read_max_power(self):
		return self.modbus.read_value(REG_MAX_ACTIVE_POWER, '32bit_float')

	# SolarEdge specific power control, published as extension paths
	def read_advanced_power_control(self):
		return self.modbus.read_value(REG_ADV_PWR_CONTROL_EN, '32bit_int')

	def write_advanced_power_control(self, enabled):
		self.modbus.write_value(REG_ADV_PWR_CONTROL_EN, '32bit_int', 1 if enabled else 0)
		self.modbus.write_value(REG_COMMIT_POWER_CONTROL, '16bit_int', 1)

	def read_active_power_limit(self):
		return self.modbus.read_value(REG_ACTIVE_POWER_LIMIT, '16bit_uint')

	def write_active_power_limit(self, percent):
		self.modbus.write_value(REG_ACTIVE_POWER_LIMIT, '16bit_uint', percent)


class Meter(object):
	"""SolarEdge energy meter connected to the inverter (SunSpec model 201-204)."""

	def __init__(self, modbus):
		self.modbus = modbus
		self.info = None

	def read_info(self):
		self.info = sunspec.parse_common_block(self.modbus.read(METER_COMMON, sunspec.COMMON_BLOCK_LENGTH))
		return self.info

	def read(self):
		r = self.modbus.read(METER_DATA, METER_DATA_LENGTH)
		sf_current = sunspec.scale_factor(r[4])
		sf_voltage = sunspec.scale_factor(r[13])
		sf_power = sunspec.scale_factor(r[20])
		sf_energy = sunspec.scale_factor(r[52])

		def power(register):
			# the meter counts export as negative, Victron as positive feed-in to the grid
			value = sunspec.int16_value(register, sf_power)
			return None if value is None else -value

		phases = []
		for i in range(3):
			phases.append(PhaseReading(
				current=sunspec.int16_value(r[1 + i], sf_current),
				voltage=sunspec.int16_value(r[6 + i], sf_voltage),
				power=power(r[17 + i]),
				energy_forward=sunspec.acc32_kwh(r[46 + 2 * i], r[47 + 2 * i], sf_energy),
				energy_reverse=sunspec.acc32_kwh(r[38 + 2 * i], r[39 + 2 * i], sf_energy)))

		return MeterReading(
			power=power(r[16]),
			energy_forward=sunspec.acc32_kwh(r[44], r[45], sf_energy),   # imported, bought from the grid
			energy_reverse=sunspec.acc32_kwh(r[36], r[37], sf_energy),   # exported, sold to the grid
			phases=phases)


class PowerLimiter(object):
	"""Zero feed-in power limiting via the SolarEdge Enhanced Dynamic Power Control (EDPC).

	Same approach as the SolarEdge limiter of Victron's dbus-fronius: the limit is written to the
	dynamic (not stored) register 0xF322. If no new limit is written within `timeout` seconds the
	inverter falls back to 100 %, so a crashed driver or GX device never leaves the inverter throttled.
	The limit requested via the D-Bus expires after timeout / 2 without a refresh.
	"""

	FALLBACK_PERCENT = 100.0

	def __init__(self, modbus, max_power, timeout, clock=time.monotonic):
		self.modbus = modbus
		self.max_power = max_power
		self.timeout = timeout
		self.clock = clock
		self.expires = None

	def initialize(self):
		if self.modbus.read_value(REG_ADV_PWR_CONTROL_EN, '32bit_int') != 1:
			log.info('enabling advanced power control')
			self.modbus.write_value(REG_ADV_PWR_CONTROL_EN, '32bit_int', 1)
			self.modbus.write_value(REG_COMMIT_POWER_CONTROL, '16bit_int', 1)
		# enable/disable the ramps once, SolarEdge otherwise keeps a previous ramp setting
		self.modbus.write_value(REG_ACTIVE_POWER_RAMP_UP, '32bit_float', 100.0)
		self.modbus.write_value(REG_ACTIVE_POWER_RAMP_DOWN, '32bit_float', 100.0)
		self.modbus.write_value(REG_ACTIVE_POWER_RAMP_UP, '32bit_float', -1.0)
		self.modbus.write_value(REG_ACTIVE_POWER_RAMP_DOWN, '32bit_float', -1.0)
		self.modbus.write_value(REG_FALLBACK_ACTIVE_POWER_LIMIT, '32bit_float', self.FALLBACK_PERCENT)
		self.modbus.write_value(REG_COMMAND_TIMEOUT, '32bit_uint', int(self.timeout))
		self.modbus.write_value(REG_ENABLE_DYNAMIC_POWER_CONTROL, '16bit_uint', 1)
		log.info('power limiter initialized (max power %s W, timeout %d s)' % (self.max_power, self.timeout))

	def set_limit(self, watts):
		"""Limit the inverter output, returns the limit in W which is effective."""
		fraction = min(1.0, max(0.0, float(watts) / self.max_power))
		self.modbus.write_value(REG_DYNAMIC_ACTIVE_POWER_LIMIT, '32bit_float', fraction * 100)
		self.expires = self.clock() + self.timeout / 2.0
		return fraction * self.max_power

	def expired(self):
		return self.expires is not None and self.clock() >= self.expires

	def reset(self):
		"""Remove the limit, returns the limit in W which is effective."""
		self.modbus.write_value(REG_DYNAMIC_ACTIVE_POWER_LIMIT, '32bit_float', self.FALLBACK_PERCENT)
		self.expires = None
		return self.max_power
