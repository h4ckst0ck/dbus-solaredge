"""Register map of a SolarEdge SE10K with a SolarEdge energy meter, shared by unit and integration tests."""

import solaredge

UNIT = 126


def string_regs(text, count):
	data = text.encode().ljust(2 * count, b'\0')
	return [data[i] << 8 | data[i + 1] for i in range(0, 2 * count, 2)]


def s16(value):
	return value & 0xFFFF


def register_map(phases=3):
	registers = {}

	def load(address, values):
		for offset, value in enumerate(values):
			registers[address + offset] = value

	load(solaredge.INVERTER_COMMON, string_regs('SolarEdge ', 16) + string_regs('SE10K', 16)
		+ string_regs('', 8) + string_regs('0004.0018.0032', 8) + string_regs('7E123456', 16))
	load(solaredge.INVERTER_MODEL_ID, [100 + phases])

	inverter = [0] * solaredge.INVERTER_DATA_LENGTH
	inverter[1:5] = [100, 101, 102, s16(-1)]          # current 10.0/10.1/10.2 A
	inverter[8:12] = [2301, 2302, 2303, s16(-1)]      # voltage 230.1/230.2/230.3 V
	inverter[12:14] = [9000, 0]                       # power 9000 W
	inverter[22:25] = [0x0001, 0x86A0, 0]             # energy 100000 Wh
	inverter[32] = 452                                # heat sink temperature
	inverter[35] = s16(-1)                            # 45.2 C
	inverter[36] = solaredge.STATUS_MPPT
	if phases < 3:
		inverter[3] = inverter[10] = 0xFFFF
	if phases < 2:
		inverter[2] = inverter[9] = 0xFFFF
	load(solaredge.INVERTER_DATA, inverter)

	load(solaredge.METER_COMMON, string_regs('SolarEdge ', 16) + string_regs('SE-WND-3Y400-MB-K2', 16)
		+ string_regs('Export+Import', 8) + string_regs('2.3', 8) + string_regs('M1234', 16))
	meter = [0] * solaredge.METER_DATA_LENGTH
	meter[1:5] = [s16(-50), 20, 30, s16(-1)]                            # -5.0/2.0/3.0 A
	meter[6:9] = [2300, 2310, 2320]
	meter[13] = s16(-1)
	meter[16:21] = [s16(-3000), s16(-1500), s16(-1000), s16(-500), 0]   # exporting 3000 W
	meter[36:44] = [0, 6000, 0, 1000, 0, 2000, 0, 3000]                  # exported Wh total/L1/L2/L3
	meter[44:52] = [1, 0, 0, 4000, 0, 5000, 0, 7000]                     # imported Wh total/L1/L2/L3
	load(solaredge.METER_DATA, meter)

	load(solaredge.REG_ADV_PWR_CONTROL_EN, [1, 0])
	load(solaredge.REG_ACTIVE_POWER_LIMIT, [100])
	load(solaredge.REG_COMMIT_POWER_CONTROL, [0])
	# enhanced dynamic power control block 0xF300 - 0xF327
	load(solaredge.REG_ENABLE_DYNAMIC_POWER_CONTROL, [0] * 0x28)
	load(solaredge.REG_MAX_ACTIVE_POWER, solaredge.ModbusDevice.encode('32bit_float', 10000.0))
	return registers
