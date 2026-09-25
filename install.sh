#!/bin/sh
# Install dbus-solaredge as daemontools service on Venus OS.
#
# The script is idempotent and adds itself to /data/rc.local, so the service
# link is restored at boot after a firmware update removed it.
# See https://github.com/victronenergy/venus/wiki/howto-add-a-driver-to-Venus

set -e

NAME=dbus-solaredge
BASE=$(dirname "$(readlink -f "$0")")
RC_LOCAL=${RC_LOCAL:-/data/rc.local}
SERVICE_DIR=${SERVICE_DIR:-/opt/victronenergy/service}
SERVICE_TMPFS=${SERVICE_TMPFS:-/service}
REMOUNT_RW=${REMOUNT_RW:-/opt/victronenergy/swupdate-scripts/remount-rw.sh}

chmod 755 "$BASE/service/run" "$BASE/service/log/run" "$BASE/install.sh" "$BASE/uninstall.sh" "$BASE/restart.sh"

if [ ! -f "$BASE/config.ini" ]; then
	cp "$BASE/config.sample.ini" "$BASE/config.ini"
	echo "created $BASE/config.ini, please adapt it"
fi

# the root filesystem is read-only, link the service only when the link is missing
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

if [ ! -f "$RC_LOCAL" ]; then
	printf '#!/bin/sh\n' > "$RC_LOCAL"
	chmod 755 "$RC_LOCAL"
fi
if ! grep -qxF "$BASE/install.sh" "$RC_LOCAL"; then
	echo "$BASE/install.sh" >> "$RC_LOCAL"
fi

echo "$NAME installed, the service starts within a few seconds"
