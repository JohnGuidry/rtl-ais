#!/usr/bin/env bash
set -uo pipefail
VID="0bda"
PID="2838"
SYS="/sys/bus/usb/devices"
devpath=""
for d in "$SYS"/*; do
    [ -f "$d/idVendor" ] || continue
    v="$(cat "$d/idVendor" 2>/dev/null)"
    p="$(cat "$d/idProduct" 2>/dev/null)"
    if [ "$v" = "$VID" ] && [ "$p" = "$PID" ]; then
        devpath="$d"
        break
    fi
done
if [ -z "$devpath" ]; then
    echo "ais-usb-reset: device $VID:$PID not found, nothing to do" >&2
    exit 0
fi
port="$(basename "$devpath")"
hubloc="${port%.*}"
portnum="${port##*.}"
echo "ais-usb-reset: found $VID:$PID at $port (hub $hubloc, port $portnum)"
if command -v uhubctl >/dev/null 2>&1; then
    echo "ais-usb-reset: attempting VBUS power-cycle via uhubctl"
    if uhubctl -a off -l "$hubloc" -p "$portnum" >/dev/null 2>&1; then
        sleep 3
        uhubctl -a on -l "$hubloc" -p "$portnum" >/dev/null 2>&1
        sleep 3
        echo "ais-usb-reset: VBUS power-cycle complete"
        exit 0
    else
        echo "ais-usb-reset: uhubctl per-port cut failed; falling back" >&2
    fi
fi
echo "ais-usb-reset: soft re-enumeration (unbind/rebind)"
echo "$port" > /sys/bus/usb/drivers/usb/unbind 2>/dev/null || { echo "ais-usb-reset: unbind failed" >&2; exit 1; }
sleep 2
echo "$port" > /sys/bus/usb/drivers/usb/bind 2>/dev/null || { echo "ais-usb-reset: bind failed" >&2; exit 1; }
sleep 3
echo "ais-usb-reset: $port reset complete"
