#!/bin/sh
# Interactive setup of dbus-solaredge: asks for the connection to the inverter,
# tests it and installs the service (see install.sh).

set -e

NAME=dbus-solaredge
BASE=$(dirname "$(readlink -f "$0")")
CONFIG=${CONFIG:-$BASE/config.ini}
PYTHON=${PYTHON:-python3}
SERVICE=${SERVICE_TMPFS:-/service}/$NAME
SVC=${SVC:-svc}

get_option() {
	sed -n "s/^$1[[:space:]]*=[[:space:]]*//p" "$CONFIG" | head -n 1
}

set_option() {  # section option value
	if grep -q "^$2[[:space:]]*=" "$CONFIG"; then
		sed -i "s|^$2[[:space:]]*=.*|$2 = $3|" "$CONFIG"
	elif grep -q "^\[$1\]" "$CONFIG"; then
		sed -i "/^\[$1\]/a $2 = $3" "$CONFIG"
	else
		printf '\n[%s]\n%s = %s\n' "$1" "$2" "$3" >> "$CONFIG"
	fi
}

ask() {  # question default -> $answer
	printf '%s [%s]: ' "$1" "$2"
	read -r answer || answer=''
	[ -n "$answer" ] || answer=$2
}

ask_number() {  # question default min max -> $answer
	while :; do
		ask "$1" "$2"
		case $answer in
			''|*[!0-9]*) ;;
			*) [ "$answer" -ge "$3" ] && [ "$answer" -le "$4" ] && return ;;
		esac
		echo "please enter a number between $3 and $4"
	done
}

ask_yes_no() {  # question default -> $answer (yes/no)
	while :; do
		ask "$1" "$2"
		case $answer in
			y|Y|yes|Yes|YES) answer=yes; return ;;
			n|N|no|No|NO) answer=no; return ;;
		esac
		echo "please answer yes or no"
	done
}

service() {  # svc option, ignored when the service is not running under daemontools
	if [ -e "$SERVICE/supervise" ] && command -v "$SVC" > /dev/null; then
		"$SVC" "$1" "$SERVICE"
	fi
}

echo "$NAME setup"
echo

if [ ! -f "$CONFIG" ]; then
	cp "$BASE/config.sample.ini" "$CONFIG"
fi

while :; do
	ask "IP address of the SolarEdge inverter" "$(get_option host)"
	case $answer in
		''|*[!0-9A-Za-z.:-]*) echo "please enter an IP address or host name" ;;
		*) host=$answer; break ;;
	esac
done
ask_number "Modbus TCP port" "$(get_option port)" 1 65535
port=$answer
ask_number "Modbus device id (SetApp: Communication -> RS485 -> SunSpec -> Device ID)" "$(get_option unit)" 1 247
unit=$answer
ask_yes_no "Offer ESS zero feed-in power limit" "$(get_option power_limit)"
power_limit=$answer

set_option modbus host "$host"
set_option modbus port "$port"
set_option modbus unit "$unit"
set_option inverter power_limit "$power_limit"
echo "saved to $CONFIG"
echo

# the inverter accepts only one Modbus TCP connection, stop the running driver for the test
service -d
sleep "${STOP_DELAY:-2}"

echo "testing the connection to $host:$port unit $unit ..."
if ! "$PYTHON" "$BASE/dbus-solaredge.py" -c "$CONFIG" --check; then
	echo
	echo "The inverter is not reachable. Check the IP address, that Modbus TCP is enabled in SetApp"
	echo "and that no other Modbus client (e.g. the SolarEdge support of Venus OS) is connected."
	ask_yes_no "Install anyway" no
	if [ "$answer" = no ]; then
		service -u
		exit 1
	fi
fi
echo

"$BASE/install.sh"
service -u
echo
echo "Log: tail -F /var/log/$NAME/current | tai64nlocal"
