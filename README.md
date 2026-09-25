# dbus-solaredge

Victron Venus OS driver for SolarEdge inverters and the SolarEdge energy meter.

The driver reads the inverter and the meter connected to it via SunSpec Modbus TCP and publishes them on the
Venus OS D-Bus, following the [Venus OS D-Bus API](https://github.com/victronenergy/venus/wiki/dbus-api):

| Service | Content |
| --- | --- |
| `com.victronenergy.grid` | SolarEdge energy meter as grid meter (power, voltage, current, energy per phase) |
| `com.victronenergy.pvinverter` | PV inverter including the ESS zero feed-in power limit |
| `com.victronenergy.temperature` | Heat sink temperature of the inverter |
| `com.victronenergy.digitalinput` | Optional: active while the inverter is throttled |

This makes the GX device work as if a Victron grid meter was installed, which is not possible with the
built-in SolarEdge support of Venus OS.

![Dashboard shows Energy flow](images/dashboard.png?raw=true "Dashboard")
![Menu shows Entries of the Inverter](images/menu.png?raw=true "Menu")

## ESS zero feed-in

The inverter output can be limited by ESS (Settings -> ESS -> Grid feed-in), the same way as for Fronius
inverters. If the limit is set to zero (or a very low value), the PV power only charges the battery and
covers the consumption, nothing is fed into the grid.

![ESS Grid feed-in configuartion](images/ESS-limit-system-feed-in.png?raw=true "ESS configuration")
![Current Limit for SolarEdge inverter](images/zero-feed-in-menu.png?raw=true "Current limit for SolarEdge inverter")
![Zero feed-in](images/zero-feed-in-curve.png?raw=true "Dashboard")

The limit uses the SolarEdge *Enhanced Dynamic Power Control*, like the SolarEdge limiter of Victron's own
[dbus-fronius](https://github.com/victronenergy/dbus-fronius/blob/master/software/src/solaredge_limiter.cpp):

- the limit is written to the dynamic register `0xF322`, which is not stored in the flash of the inverter
- the limit is a percentage with decimals, not 1 % steps
- when the inverter receives no new limit within `power_limit_timeout` (120 s), it falls back to 100 %,
  so a crashed driver or GX device never leaves the inverter throttled
- when ESS does not refresh the limit within half of that time, the driver removes it

## Requirements

- Venus OS on a GX device (Cerbo GX, Raspberry Pi, ...), root access enabled
  ([instructions](https://www.victronenergy.com/live/ccgx:root_access))
- SolarEdge inverter with Modbus TCP enabled in SetApp (Communication -> LAN / Modbus TCP),
  see the [Victron guide](https://www.victronenergy.com/live/venus-os:gx_solaredge)
- optional: SolarEdge energy meter connected to the inverter

The inverter accepts only **one** Modbus TCP connection. Disable the SolarEdge / Modbus TCP support of
Venus OS for this inverter (Settings -> PV inverters) and any other Modbus client, or use a Modbus proxy like
[modbus-proxy](https://pypi.org/project/modbus-proxy/).

## Installation

```sh
wget -O /tmp/dbus-solaredge.zip https://github.com/h4ckst0ck/dbus-solaredge/archive/refs/heads/master.zip
unzip /tmp/dbus-solaredge.zip -d /data
mv /data/dbus-solaredge-master /data/dbus-solaredge
/data/dbus-solaredge/install.sh
```

`install.sh` creates `config.ini` from `config.sample.ini`, links the service and adds itself to
`/data/rc.local`, so the service is restored automatically after a firmware update. Adapt the configuration
(at least `host` and `unit`) and restart the driver:

```sh
vi /data/dbus-solaredge/config.ini
/data/dbus-solaredge/restart.sh
```

If the grid meter does not show up, configure the AC input of the Multi/Quattro as "Grid".

To remove the driver run `/data/dbus-solaredge/uninstall.sh`.

## Configuration

`config.ini` (all options are optional, see `config.sample.ini`):

| Section | Option | Default | Description |
| --- | --- | --- | --- |
| modbus | host | 192.168.178.80 | IP address of the inverter |
| modbus | port | 502 | Modbus TCP port |
| modbus | unit | 126 | Modbus device id (SetApp: Communication -> RS485 -> Protocol -> SunSpec -> Device ID) |
| modbus | timeout | 2.0 | Timeout of one Modbus request in seconds |
| inverter | max_power | 0 | Max power in W, 0 = read from the inverter |
| inverter | power_limit | yes | Offer the ESS zero feed-in power limit |
| inverter | power_limit_timeout | 120 | Fallback of the inverter to 100 % after this time without new limit (30 - 600 s) |
| inverter | throttle_input | no | Publish the digital input "PV inverter throttled" |
| driver | update_interval | 1.0 | Update interval in seconds |
| driver | fail_timeout | 10 | Exit when the inverter is not reachable for this time (the service is restarted) |

Host, port and unit can also be given on the command line, see `dbus-solaredge.py --help`.

Device instance, custom name, PV inverter position and temperature type are stored in the Venus OS settings
(`/Settings/Devices/solaredge_<serial>...`) and can be changed in the GUI.

## D-Bus paths

Besides the paths of the [D-Bus API](https://github.com/victronenergy/venus/wiki/dbus) for the service types,
the PV inverter publishes two SolarEdge specific paths, which can be written e.g. via MQTT:

| Path | Description |
| --- | --- |
| `/Ac/AdvancedPwrControlEn` | SolarEdge advanced power control (register 0xF142), 0 or 1 |
| `/Ac/ActivePowerLimit` | SolarEdge active power limit in % (register 0xF001), 0 - 100 |

## Upgrading from version 0.x

- The configuration moved from `dbus-solaredge.py` to `config.ini`.
- The D-Bus service names contain the serial number now, e.g. `com.victronenergy.pvinverter.solaredge_7E123456`
  instead of `com.victronenergy.pvinverter.pv0.pvinverter_id00`. The device instances (grid 0, PV inverter 20,
  temperature 26) stay the same, so the VRM history continues.
- `kill_me.sh` is replaced by `restart.sh`.
- Run `install.sh` once.

## Troubleshooting

```sh
svstat /service/dbus-solaredge                         # up since how many seconds?
tail -F /var/log/dbus-solaredge/current | tai64nlocal  # log
python3 /data/dbus-solaredge/dbus-solaredge.py -d      # run manually with debug output (stop the service first)
```

If the service is always up for only a few seconds it keeps restarting, the log shows why. A message like
`unable to connect to 192.168.178.80:502` means the inverter is not reachable or another client is connected.

## Development

| File | Content |
| --- | --- |
| `dbus-solaredge.py` | Entry point: configuration, main loop, watchdog, driver |
| `solaredge.py` | Modbus access to inverter and meter, power limiter (no D-Bus) |
| `services.py` | D-Bus services (grid, pvinverter, temperature, digitalinput) |
| `sunspec.py` | Decoding of SunSpec registers |
| `sunspec.txt` | SunSpec register map of SolarEdge |

Code style as in all Victron projects: PEP8 with tabs, max. 110 characters per line (`flake8`).

```sh
pip install -r requirements-dev.txt
flake8 .
pytest                    # unit tests, 100 % line and branch coverage required
```

The integration tests run the driver on a real D-Bus with Victron's
[velib_python](https://github.com/victronenergy/velib_python), a simulated inverter (Modbus TCP server) and a
minimal localsettings service. They need `dbus-daemon`, dbus-python and PyGObject:

```sh
git clone https://github.com/victronenergy/velib_python /tmp/velib_python
VELIB_PYTHON=/tmp/velib_python pytest --no-cov tests_integration
```

## Hardware

Installation of the original author (version 0.x):

- SolarEdge SE16K with SolarEdge Modbus Meter
- 3x Victron MultiPlus-II (three phase), Cerbo GX

## Credits

Based on https://github.com/RalfZim/venus.dbus-fronius-smartmeter and the code of Paul1974 in
https://www.photovoltaikforum.com/thread/161496-solaredge-smartmeter-mit-victron/?pageNo=1.
The ESS zero feed-in was added by [irudi](https://github.com/irudi).
