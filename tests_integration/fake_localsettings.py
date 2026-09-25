#!/usr/bin/env python3
"""Minimal com.victronenergy.settings service (Venus OS localsettings) for the integration tests.

Implements what velib_python's SettingsDevice uses: AddSettings on '/' (velib since 2025),
AddSetting on '/Settings' (older velib) and the BusItem interface (GetValue, SetValue, GetText,
GetAttributes, PropertiesChanged) on every setting path.
"""

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

SETTINGS = 'com.victronenergy.Settings'
BUSITEM = 'com.victronenergy.BusItem'


class LocalSettings(dbus.service.Object):
	def __init__(self, bus):
		dbus.service.Object.__init__(self, bus, '/')
		self.values = {}
		self.items = {}

	@dbus.service.method(SETTINGS, in_signature='aa{sv}', out_signature='aa{sv}')
	def AddSettings(self, settings):
		result = []
		for setting in settings:
			path = str(setting['path'])
			self.add(path, setting['default'], setting['min'], setting['max'])
			result.append({'path': path, 'error': 0, 'value': self.values[path]})
		return result

	def add(self, path, default, minimum, maximum):
		self.values.setdefault(path, default)
		if path not in self.items:
			self.items[path] = SettingItem(self, path, (default, minimum, maximum, False))


class LegacySettings(dbus.service.Object):
	"""AddSetting interface on /Settings, used by velib_python before 2025."""

	def __init__(self, settings):
		dbus.service.Object.__init__(self, settings.connection, '/Settings')
		self.settings = settings

	@dbus.service.method(BUSITEM, in_signature='ssvsvv', out_signature='i')
	def AddSetting(self, group, name, default, item_type, minimum, maximum):
		self.settings.add('/Settings/' + name, default, minimum, maximum)
		return 0


class SettingItem(dbus.service.Object):
	def __init__(self, settings, path, attributes):
		dbus.service.Object.__init__(self, settings.connection, path)
		self.settings = settings
		self.path = path
		self.attributes = attributes

	@dbus.service.method(BUSITEM, out_signature='vvvi')
	def GetAttributes(self):
		return self.attributes

	@dbus.service.method(BUSITEM, out_signature='v')
	def GetValue(self):
		return self.settings.values[self.path]

	@dbus.service.method(BUSITEM, out_signature='s')
	def GetText(self):
		return str(self.settings.values[self.path])

	@dbus.service.method(BUSITEM, in_signature='v', out_signature='i')
	def SetValue(self, value):
		self.settings.values[self.path] = value
		self.PropertiesChanged({'Value': value, 'Text': str(value)})
		return 0

	@dbus.service.signal(BUSITEM, signature='a{sv}')
	def PropertiesChanged(self, changes):
		pass


def main():
	DBusGMainLoop(set_as_default=True)
	bus = dbus.SessionBus()
	settings = LocalSettings(bus)
	legacy = LegacySettings(settings)  # noqa: F841
	name = dbus.service.BusName('com.victronenergy.settings', bus)  # noqa: F841
	GLib.MainLoop().run()


if __name__ == '__main__':
	main()
