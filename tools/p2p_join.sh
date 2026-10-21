#!/bin/bash
# p2p_join.sh - join the phone's Wi-Fi Direct group (phone sends -> we receive).
#
# The phone creates a persistent group named DIRECT-xx-fastCon when it starts
# sending; its PSK lives in /data/vendor/wifi/wpa/p2p_supplicant.conf and is
# regenerated every session, so read it fresh over adb each time.
#
#   sudo ./tools/p2p_join.sh
#
# Leaves wlan0 in the group and prints the phone's address; run
# tools/oshare_recv.py afterwards. Ctrl-C restores NetworkManager.

set -uo pipefail

IFACE="${IFACE:-wlan0}"
CONF=/data/vendor/wifi/wpa/p2p_supplicant.conf
ADB="${ADB:-adb}"

say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

PREV=$(nmcli -t -f NAME,DEVICE con show --active | awk -F: '$2=="'$IFACE'"{print $1}' | head -1)
PROXY=$(nmcli -t -f NAME,TYPE con show --active | awk -F: '$2=="tun"' | head -1)

cleanup() {
    say "restoring network"
    nmcli con delete oshare-join >/dev/null 2>&1
    [ -n "$PREV" ] && nmcli con up "$PREV" >/dev/null 2>&1 && say "rejoined $PREV"
    [ -n "$PROXY" ] && nmcli con up "$PROXY" >/dev/null 2>&1 && say "proxy back"
    say "done"
}
trap cleanup EXIT INT TERM

SSID=$($ADB shell "su -c 'grep -o \"ssid=\\\"[^\\\"]*\\\"\" $CONF | head -1'" | tr -d '\r' | cut -d'"' -f2)
PSK=$($ADB shell "su -c 'grep -o \"psk=\\\"[^\\\"]*\\\"\" $CONF | head -1'" | tr -d '\r' | cut -d'"' -f2)
say "phone group: ssid='$SSID' psk='$PSK'"
[ -z "$SSID" ] || [ -z "$PSK" ] && { say "no group on the phone; keep it on the 发送 page"; exit 1; }

# The TUN proxy hijacks 192.168.49.0/24 through its own table.
[ -n "$PROXY" ] && nmcli con down "$PROXY" >/dev/null 2>&1 && say "proxy $PROXY down"

nmcli con delete oshare-join >/dev/null 2>&1
nmcli con add type wifi ifname "$IFACE" con-name oshare-join ssid "$SSID" \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK" \
      ipv4.never-default yes ipv6.never-default yes >/dev/null 2>&1
say "joining $SSID"
nmcli con up oshare-join >/dev/null 2>&1 || { say "join failed"; exit 1; }

IP=$(nmcli -t -f IP4.ADDRESS con show oshare-join | head -1 | cut -d: -f2 | cut -d/ -f1)
GW=$(nmcli -t -f IP4.GATEWAY con show oshare-join | head -1 | cut -d: -f2)
GW=${GW:-192.168.49.1}
say "local ${IP:-?} gateway $GW"

iw dev "$IFACE" info | grep -E '^\s+(ssid|type)' || true
ping -c 2 -W 1 "$GW" >/dev/null 2>&1 && say "phone reachable" || say "ping failed"

# A default-DROP firewall would also block the HTTP connection we dial.
if [ -n "$IP" ]; then
    iptables -I INPUT -i "$IFACE" -j ACCEPT 2>/dev/null && say "iptables accept on $IFACE"
    iptables -I OUTPUT -o "$IFACE" -j ACCEPT 2>/dev/null || true
fi

say "ready - now run: python3 tools/oshare_recv.py --host $GW"
say "Ctrl-C to leave the group"
while true; do sleep 5; done
