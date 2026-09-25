import pytest

import sunspec
from fake_inverter import s16, string_regs


def test_int16():
	assert sunspec.int16(0x0001) == 1
	assert sunspec.int16(0xFFFF) == -1
	assert sunspec.int16(0x8000) == -32768


@pytest.mark.parametrize('register, factor', [(0, 1), (2, 100), (s16(-2), 0.01)])
def test_scale_factor(register, factor):
	assert sunspec.scale_factor(register) == pytest.approx(factor)


def test_uint16_value():
	assert sunspec.uint16_value(2301, 0.1) == 230.1
	assert sunspec.uint16_value(0xFFFF, 1) is None


def test_int16_value():
	assert sunspec.int16_value(s16(-52), 0.1) == -5.2
	assert sunspec.int16_value(0x8000, 1) is None


def test_acc32_kwh():
	assert sunspec.acc32_kwh(0x0001, 0x86A0, 1) == 100.0
	assert sunspec.acc32_kwh(0, 5, 1000) == 5.0


def test_string():
	assert sunspec.string(string_regs(' SolarEdge ', 8)) == 'SolarEdge'
	assert sunspec.string([0x0041, 0x4200]) == 'AB'  # NUL bytes are skipped
	assert sunspec.string([]) == ''


def test_parse_common_block_uses_full_fields():
	registers = (string_regs('M' * 32, 16) + string_regs('D' * 32, 16) + string_regs('O' * 16, 8)
		+ string_regs('V' * 16, 8) + string_regs('S' * 32, 16))
	expected = sunspec.CommonBlock('M' * 32, 'D' * 32, 'O' * 16, 'V' * 16, 'S' * 32)
	assert sunspec.parse_common_block(registers) == expected


def test_parse_common_block_too_short():
	with pytest.raises(ValueError):
		sunspec.parse_common_block([0] * 63)
