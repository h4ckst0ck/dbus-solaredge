#!/bin/sh
# Stop dbus-solaredge and remove it from the services and the boot hook.
# The files in the installation directory are not removed.

NAME=dbus-solaredge
BASE=$(dirname "$(readlink -f "$0")")
RC_LOCAL=${RC_LOCAL:-/data/rc.local}
SERVICE_DIR=${SERVICE_DIR:-/opt/victronenergy/service}
SERVICE_TMPFS=${SERVICE_TMPFS:-/service}
REMOUNT_RW=${REMOUNT_RW:-/opt/victronenergy/swupdate-scripts/remount-rw.sh}
SVC=${SVC:-svc}

for hook in "$RC_LOCAL" "$RC_LOCAL.disabled"; do
	if [ -f "$hook" ]; then
		grep -vF "$BASE/install.sh" "$hook" > "$hook.tmp" || true
		cat "$hook.tmp" > "$hook"
		rm -f "$hook.tmp"
	fi
done

if [ -e "$SERVICE_TMPFS/$NAME" ]; then
	"$SVC" -d "$SERVICE_TMPFS/$NAME" 2>/dev/null
	rm -f "$SERVICE_TMPFS/$NAME"
fi

if [ -L "$SERVICE_DIR/$NAME" ]; then
	if [ -x "$REMOUNT_RW" ]; then
		"$REMOUNT_RW"
	fi
	rm -f "$SERVICE_DIR/$NAME"
fi

pkill -f "$BASE/dbus-solaredge.py" 2>/dev/null
echo "$NAME uninstalled"
exit 0
