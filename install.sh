#!/bin/sh
# Install dbus-solaredge as daemontools service on Venus OS.
#
# The installation directory on /data survives firmware updates, the service link in the
# root filesystem does not. install.sh therefore adds itself to /data/rc.local, which
# Venus OS runs at every boot, and restores the link after an update.
# See https://github.com/victronenergy/venus/wiki/howto-add-a-driver-to-Venus
#
# Usage: install.sh [--boot]   (--boot: quiet, used by /data/rc.local)

set -e

NAME=dbus-solaredge
BASE=$(dirname "$(readlink -f "$0")")
RC_LOCAL=${RC_LOCAL:-/data/rc.local}
SERVICE_DIR=${SERVICE_DIR:-/opt/victronenergy/service}
SERVICE_TMPFS=${SERVICE_TMPFS:-/service}
REMOUNT_RW=${REMOUNT_RW:-/opt/victronenergy/swupdate-scripts/remount-rw.sh}
BOOT_LINE="[ -x $BASE/install.sh ] && $BASE/install.sh --boot"

info() {
	[ "$1" = "--boot" ] || echo "$2"
}

chmod 755 "$BASE/service/run" "$BASE/service/log/run" "$BASE"/*.sh

if [ ! -f "$BASE/config.ini" ]; then
	cp "$BASE/config.sample.ini" "$BASE/config.ini"
	info "$1" "created $BASE/config.ini, please adapt it or run setup.sh"
fi

# the root filesystem is read-only, it is only remounted when the link is missing (after a firmware update)
if [ ! -L "$SERVICE_DIR/$NAME" ]; then
	if [ -x "$REMOUNT_RW" ]; then
		"$REMOUNT_RW"
	fi
	ln -sfn "$BASE/service" "$SERVICE_DIR/$NAME"
fi

# /service is a tmpfs copy of $SERVICE_DIR on current Venus OS versions, link it there as well to start now
if [ -d "$SERVICE_TMPFS" ] && [ ! -e "$SERVICE_TMPFS/$NAME" ]; then
	ln -sfn "$BASE/service" "$SERVICE_TMPFS/$NAME"
fi

# boot hook, venus-platform renames rc.local to rc.local.disabled when modifications are disabled in the GUI
if [ -f "$RC_LOCAL.disabled" ] && [ ! -f "$RC_LOCAL" ]; then
	hook="$RC_LOCAL.disabled"
	echo "WARNING: $RC_LOCAL is disabled (Settings -> General -> Modification checks)," \
		"$NAME will not be restored after a firmware update until it is enabled again"
else
	hook="$RC_LOCAL"
fi
if [ ! -f "$hook" ]; then
	printf '#!/bin/sh\n' > "$hook"
fi
if ! grep -qxF "$BOOT_LINE" "$hook"; then
	echo "$BOOT_LINE" >> "$hook"
fi
chmod 755 "$hook"  # Venus OS runs rc.local only when it is executable

info "$1" "$NAME installed, the service starts within a few seconds"
