# dbus-solaredge Service
Victron Venus integration for SolarEdge Inverters

### Purpose

This service is meant to be run on a raspberry Pi with Venus OS from Victron or a for example a Cerbo GX device.

The Python script cyclically reads data from the SolarEdge Inverter via Sunspec Modbus and publishes information on the dbus, using the services com.victronenergy.grid, com.victronenergy.pvinverter.pv0, com.victronenergy.temperature, com.victronenergy.digitalinput. This makes the Venus OS work as if you had a physical Victron Grid Meter installed and gives all information about PV Intervter load, temperature and if the inveter is in limit mode.

![Dashboard shows Energy flow](images/dashboard.png?raw=true "Dashboard")
![Menu shows Entries of the Inverter](images/menu.png?raw=true "Menu")

### Configuration

You need to modify the settings in the dbus-solaredge.py as needed:

`SERVER_HOST = "192.168.178.80"`

`SERVER_PORT = 502`

`UNIT = 2`

### Installation

1. Copy the files to the /data folder on your venus:

   - /data/dbus-solaredge/dbus-solaredge.py
   - /data/dbus-solaredge/kill_me.sh
   - /data/dbus-solaredge/service/run
   - /data/dbus-solaredge/service/log/run

2. Set permissions for files:

  `chmod 755 /data/dbus-solaredge/service/run`
  
  `chmod 755 /data/dbus-solaredge/service/log/run`
  
  `chmod 744 /data/dbus-solaredge/kill_me.sh`

4. Add a symlink to for auto starting:

   `ln -s /data/dbus-solaredge/service/ /opt/victronenergy/service/dbus-solaredge`

   The supervisor should automatically start this service within seconds.

### Debugging

You can check the status of the service with svstat:

`svstat /service/dbus-solaredge`

It will show something like this:

`/service/dbus-solaredge: up (pid 8179) 746 seconds`

If the number of seconds is always 0 or 1 or any other small number, it means that the service crashes and gets restarted all the time.

You could also take a look at the log-file:

`tail -f /var/log/dbus-solaredge/current`

and see if there are any error messages.

When you think that the script crashes, start it directly from the command line:

`python /data/dbus-solaredge/dbus-solaredge.py`

and see if it throws any error messages.

If the script stops with the message

`dbus.exceptions.NameExistsException: Bus name already exists: com.victronenergy.grid"`

it means that the service is still running or another service is using that bus name.

If you see something like:

`2022-06-05 10:39:04,238 - DbusSolarEdge - INFO - Startup, trying connection to Modbus-Server: ModbusTCP 192.168.178.80:502, UNIT 2`

`2022-06-05 10:39:04,247 - pymodbus.client.sync - ERROR - Connection to (192.168.178.80, 502) failed: [Errno 111] Connection refused`

`2022-06-05 10:39:04,249 - DbusSolarEdge - ERROR - unable to connect to 192.168.178.80:502`

Then you are not able to connect to your Inverter via Modbus. This can be a misconfiguration or another client is already connected. 
The inverter will accept only one concurrent client connected, if you need more than one client connection you may use a modbus proxy like
https://pypi.org/project/modbus-proxy/


#### Restart the script

If you want to restart the script, for example after changing it, just run the following command:

`/data/dbus-solaredge/kill_me.sh`

The supervisor will restart the scriptwithin a few seconds.

### Hardware

In my installation at home, I am using the following Hardware:

- SolarEdge SE16K
- SolarEdge Modbus Meter
- 3x Victron MultiPlus-II - Battery Inverter (three phase)
- Cerbo GX
- DIY Battery 32x 280AH Lifepo EVE Cells with BMS from Batrium 

### Credits

I have shamelessly copied and adapted the code from https://github.com/RalfZim/venus.dbus-fronius-smartmeter and the readme as well as the code from Paul1974 https://www.photovoltaikforum.com/thread/161496-solaredge-smartmeter-mit-victron/?pageNo=1
