"""Decoding of SunSpec Modbus register values.

See https://sunspec.org and the SolarEdge "SunSpec Implementation" technical note.
"""

import collections
import ctypes

# Values reported by a device for registers it does not implement
NOT_IMPLEMENTED_UINT16 = 0xFFFF
NOT_IMPLEMENTED_INT16 = 0x8000

# Common model (model 1): Manufacturer(16) Model(16) Options(8) Version(8) SerialNumber(16)
COMMON_BLOCK_LENGTH = 64

CommonBlock = collections.namedtuple('CommonBlock', 'manufacturer model options version serial')


def int16(register):
	"""Interpret a register as signed 16 bit integer."""
	return ctypes.c_short(register).value


def scale_factor(register):
	"""Convert a SunSpec scale factor register (sunssf) into a multiplier."""
	return 10 ** int16(register)


def uint16_value(register, factor):
	"""Scaled uint16 value, None if not implemented."""
	if register == NOT_IMPLEMENTED_UINT16:
		return None
	return round(register * factor, 2)


def int16_value(register, factor):
	"""Scaled int16 value, None if not implemented."""
	if register == NOT_IMPLEMENTED_INT16:
		return None
	return round(int16(register) * factor, 2)


def acc32_kwh(high, low, factor):
	"""Energy counter (acc32, Wh) in kWh."""
	return ((high << 16) + low) * factor / 1000.0


def string(registers):
	"""Decode a SunSpec string (two characters per register, NUL padded)."""
	characters = []
	for register in registers:
		for byte in (register >> 8, register & 0xFF):
			if byte:
				characters.append(chr(byte))
	return ''.join(characters).strip()


def parse_common_block(registers):
	"""Parse the registers of a common model block, starting at the manufacturer."""
	if len(registers) < COMMON_BLOCK_LENGTH:
		raise ValueError('common block needs %d registers, got %d' % (COMMON_BLOCK_LENGTH, len(registers)))
	return CommonBlock(
		manufacturer=string(registers[0:16]),
		model=string(registers[16:32]),
		options=string(registers[32:40]),
		version=string(registers[40:48]),
		serial=string(registers[48:64]))
