#!/bin/sh
# Dump BLE advertisements while scanning, so we can compare our own
# OShare advertisement with what real phones broadcast.
#
#   sudo pacman -S bluez bluez-utils
#   ./tools/ble_scan.sh 30            # 30 seconds
#
# Output: /tmp/ble-scan.log (btmon text) + a short summary on stdout.
set -e
DURATION="${1:-30}"
LOG="${2:-/tmp/ble-scan.log}"

if [ "$(id -u)" != "0" ]; then
    echo "run as root (btmon needs HCI access)" >&2
    exit 1
fi

echo ">> scanning for ${DURATION}s, log: $LOG"
rm -f "$LOG"

# btmon decodes HCI and prints AD structures; bluetoothctl drives the scan.
btmon -w /tmp/ble.snoop >"$LOG" 2>&1 &
BTMON=$!
sleep 1
bluetoothctl --timeout "$DURATION" scan le >/dev/null 2>&1 || true
sleep 1
kill "$BTMON" 2>/dev/null || true
wait "$BTMON" 2>/dev/null || true

echo ">> advertisements seen:"
# btmon prints "Service Data" blocks; keep the address + uuid + payload lines.
grep -E "Address:|Service Data|UUID:|Name \(complete\)|Manufacturer" "$LOG" | head -200

echo
echo ">> looking for OShare adverts (0x6666/0x6667/0x8881):"
grep -n -B6 -A2 -E "0x6666|0x6667|0x8881" "$LOG" | head -120 || true

echo
echo "raw log: $LOG  (btsnoop: /tmp/ble.snoop, open with wireshark)"
