#!/bin/sh
# Restart the service, e.g. after changing config.ini
exec "${SVC:-svc}" -t /service/dbus-solaredge
