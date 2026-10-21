#!/bin/bash
# p2p_probe.sh - join the phone's Wi-Fi Direct group, probe its TLS port,
# then restore the previous Wi-Fi connection no matter what happens.
#
# The laptop has one radio: joining DIRECT-xx-fastCon disconnects it from
# whatever network it was on (including the internet). Everything here runs
# inside a single script so the outage is short and always restored by the
# exit trap.
#
#   sudo ./tools/p2p_probe.sh
#
# Reads the group SSID/PSK straight off the phone over adb (root), so it
# always uses the credentials of the *current* round - the phone regenerates
# the PSK every time it creates the group.

set -u
ADB="${ADB:-adb}"
CONF=/data/vendor/wifi/wpa/p2p_supplicant.conf
LOG=${LOG:-/tmp/p2p_probe.log}
PORT_LO=40000
PORT_HI=65535

exec > >(tee "$LOG") 2>&1
say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

if [ "$(id -u)" != "0" ]; then
    say "run as root (needs nmcli to switch the interface)"
    exit 1
fi

# Remember where we came from so the trap can put it back.
PREV=$(nmcli -t -f NAME,DEVICE con show --active \
        | awk -F: '$2=="wlan0"{print $1}' | head -1)
PREV=${PREV:-}
PROXY_ACTIVE=$(nmcli -t -f NAME,TYPE con show --active \
        | awk -F: '$2=="tun"{print $1}' | head -1)
say "previous connection: ${PREV:-<none>}  proxy: ${PROXY_ACTIVE:-<off>}"

restore() {
    say "restoring network ..."
    if [ -n "$PREV" ]; then
        nmcli con up "$PREV" >/dev/null 2>&1 && say "rejoined $PREV"
    fi
    if [ -n "$PROXY_ACTIVE" ]; then
        nmcli con up "$PROXY_ACTIVE" >/dev/null 2>&1 && say "proxy $PROXY_ACTIVE back up"
    fi
    say "done; log at $LOG"
}
trap restore EXIT INT TERM

# --- 1. current credentials from the phone -------------------------------
SSID=$($ADB shell "su -c 'grep -o \"ssid=\\\"[^\\\"]*\\\"\" $CONF | head -1'" \
       | tr -d '\r' | cut -d'"' -f2)
PSK=$($ADB shell "su -c 'grep -o \"psk=\\\"[^\\\"]*\\\"\" $CONF | head -1'" \
      | tr -d '\r' | cut -d'"' -f2)
BSSID=$($ADB shell "su -c 'grep -o \"bssid=[0-9a-f:]*\" $CONF | head -1'" \
        | tr -d '\r' | cut -d= -f2)
say "phone group: ssid='$SSID' bssid=$BSSID psk='$PSK'"
[ -z "$SSID" ] || [ -z "$PSK" ] && { say "no group on the phone; open 互传 first"; exit 1; }

# --- 2. drop the proxy so its ip rules stop hijacking 192.168.49.0/24 ----
if [ -n "$PROXY_ACTIVE" ]; then
    nmcli con down "$PROXY_ACTIVE" >/dev/null 2>&1 && say "proxy $PROXY_ACTIVE down"
fi

# --- 3. join the group ---------------------------------------------------
nmcli con delete p2p-probe >/dev/null 2>&1
nmcli con add type wifi ifname wlan0 con-name p2p-probe ssid "$SSID" \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK" >/dev/null 2>&1 || {
    say "could not create the profile"; exit 1; }
say "joining $SSID ..."
nmcli con up p2p-probe >/dev/null 2>&1
sleep 3

IP=$(nmcli -t -f IP4.ADDRESS con show p2p-probe | head -1 | cut -d: -f2 | cut -d/ -f1)
GW=$(nmcli -t -f IP4.GATEWAY con show p2p-probe | head -1 | cut -d: -f2)
say "local ip=${IP:-?} gateway=${GW:-?}"
[ -z "$IP" ] && { say "did not get a lease"; exit 1; }

# --- 4. is the phone reachable at all ------------------------------------
say "arp/ping the group owner ..."
ping -c 2 -W 1 "${GW:-192.168.49.1}" >/dev/null 2>&1 && say "ping ok" || say "ping failed"
ip neigh show | grep -i "${GW:-192.168.49.1}" || true

# --- 5. find the TLS port ------------------------------------------------
say "scanning $PORT_LO-$PORT_HI on ${GW:-192.168.49.1} (this takes a while)"
FOUND=""
for port in $(seq "$PORT_LO" "$PORT_HI"); do
    if timeout 0.15 bash -c "echo > /dev/tcp/${GW:-192.168.49.1}/$port" 2>/dev/null; then
        say "open: $port"
        FOUND="$FOUND $port"
    fi
done
say "open ports:${FOUND:- none}"

# --- 6. try the OShare HTTP endpoints on whatever we found ---------------
for port in $FOUND; do
    for path in /versionNegotiation "/download?taskId=1" /; do
        say "GET https://${GW:-192.168.49.1}:$port$path"
        timeout 5 curl -sk --http1.1 "https://${GW:-192.168.49.1}:$port$path" | head -c 400
        echo
    done
done

say "probe finished"
