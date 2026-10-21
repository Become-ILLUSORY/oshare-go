#!/bin/bash
# oshare-go.sh - one command that makes this laptop a 互传 peer.
#
# Mirrors what an OPPO/realme phone does when you tap "send": advertise over
# BLE, answer the phone's share request, hand it our Wi-Fi Direct AP and port,
# and serve the file. Nothing else to type.
#
#   sudo ./tools/oshare-go.sh                    # send tools/README-ish default file
#   sudo ./tools/oshare-go.sh -f ~/movie.mp4     # send a real file
#   sudo ./tools/oshare-go.sh -f ~/movie.mp4 --recv   # (future) also accept files
#
# Steps, all automatic:
#   1. bring up a P2P GO with a random SSID/PSK and 192.168.49.1/24 + DHCP
#   2. start the HTTPS/WebSocket server on a free port with the file ready
#   3. advertise over BLE and, when the phone asks, push it our AP + port
#   4. wait for the phone to connect, negotiate and fetch /download
#
# Ctrl-C restores wlan0 to NetworkManager.

set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)

FILE=""
PORT=""
SSID="DIRECT-aa-fastCon"
PSK=""
FREQ=2437
NAME="LinuxBox"

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        -f|--file)  FILE="$2"; shift 2 ;;
        -p|--port)  PORT="$2"; shift 2 ;;
        -n|--name)  NAME="$2"; shift 2 ;;
        -s|--ssid)  SSID="$2"; shift 2 ;;
        -k|--psk)   PSK="$2"; shift 2 ;;
        -h|--help)  usage ;;
        *) echo "unknown option: $1"; usage ;;
    esac
done

[ "$(id -u)" = 0 ] || { echo "run as root (needs wpa_supplicant, ip, dnsmasq)"; exit 1; }

say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# --- pick a port and credentials -----------------------------------------
[ -n "$PORT" ] || PORT=$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("", 0))
print(s.getsockname()[1])
s.close()
PY
)
[ -n "$PSK" ] || PSK=$(python3 -c "import secrets;print(''.join(secrets.choice('0123456789') for _ in range(8)))")
[ -n "$FILE" ] || FILE=/tmp/oshare-default.txt
[ -f "$FILE" ] || printf 'hello from oshare-go\n' > "$FILE"
say "file=$FILE port=$PORT ssid=$SSID psk=$PSK"

CTRL=/run/oshare-p2p
CONF=/run/oshare-p2p.conf
GO_IP_CIDR=192.168.49.1/24
CERT=/tmp/oshare-srv.crt
KEY=/tmp/oshare-srv.key
IFACE=${IFACE:-wlan0}

PREV=$(nmcli -t -f NAME,DEVICE con show --active | awk -F: '$2=="'$IFACE'"{print $1}' | head -1)
PROXY=$(nmcli -t -f NAME,TYPE con show --active | awk -F: '$2=="tun"' | head -1)
GO_IF=""
BLE_PID=""

cleanup() {
    say "shutting down"
    [ -n "$BLE_PID" ] && kill "$BLE_PID" 2>/dev/null
    [ -f /run/oshare-dnsmasq.pid ] && kill "$(cat /run/oshare-dnsmasq.pid)" 2>/dev/null
    if [ -n "$GO_IF" ]; then
        iptables -D INPUT -i "$GO_IF" -j ACCEPT 2>/dev/null
    fi
    if [ -S "$CTRL/$IFACE" ]; then
        wpa_cli -p "$CTRL" -i "$IFACE" p2p_group_remove "${GO_IF:-$IFACE}" >/dev/null 2>&1
    fi
    pkill -f "wpa_supplicant.*$CTRL" 2>/dev/null
    rm -f /run/oshare-dnsmasq.pid
    [ -n "$PROXY" ] && nmcli con up "$PROXY" >/dev/null 2>&1 && say "proxy back"
    nmcli device set "$IFACE" managed yes 2>/dev/null
    [ -n "$PREV" ] && nmcli con up "$PREV" >/dev/null 2>&1 && say "rejoined $PREV"
    say "done"
}
trap cleanup EXIT INT TERM

# --- 1. P2P group owner --------------------------------------------------
say "taking $IFACE from NetworkManager"
nmcli device set "$IFACE" managed no
pkill -f "wpa_supplicant.*-i *$IFACE" 2>/dev/null
sleep 1

cat > "$CONF" <<EOF
ctrl_interface=$CTRL
update_config=1
device_name=$NAME
device_type=10-0050F204-5
config_methods=display push_button keypad virtual_push_button physical_display
p2p_add_cli_chan=1
p2p_no_group_iface=0
p2p_go_ht40=1
EOF
mkdir -p "$CTRL"
wpa_supplicant -B -i "$IFACE" -c "$CONF" 2>/dev/null || {
    say "wpa_supplicant failed to start"; exit 1; }
sleep 2

NETID=$(wpa_cli -p "$CTRL" -i "$IFACE" add_network | tail -1)
wpa_cli -p "$CTRL" -i "$IFACE" set_network "$NETID" ssid "\"$SSID\"" >/dev/null
wpa_cli -p "$CTRL" -i "$IFACE" set_network "$NETID" psk "\"$PSK\"" >/dev/null
wpa_cli -p "$CTRL" -i "$IFACE" set_network "$NETID" mode 3 >/dev/null
wpa_cli -p "$CTRL" -i "$IFACE" set_network "$NETID" disabled 2 >/dev/null
wpa_cli -p "$CTRL" -i "$IFACE" p2p_group_add persistent="$NETID" freq="$FREQ" >/dev/null
sleep 2

GO_IF=$(iw dev | awk '/Interface p2p-/{print $2; exit}')
[ -n "$GO_IF" ] || { say "no P2P group interface"; exit 1; }
say "group owner on $GO_IF"

ip addr replace "$GO_IP_CIDR" dev "$GO_IF"
ip link set "$GO_IF" up

DHCPLOG=/tmp/oshare-dnsmasq-$$.log
: > "$DHCPLOG"; chmod 644 "$DHCPLOG"
dnsmasq --interface="$GO_IF" --bind-dynamic \
        --dhcp-range=192.168.49.10,192.168.49.200,255.255.255.0,12h \
        --dhcp-option=3,192.168.49.1 --dhcp-option=6,192.168.49.1 \
        --dhcp-authoritative --log-dhcp --no-resolv --no-hosts \
        --pid-file=/run/oshare-dnsmasq.pid --log-facility="$DHCPLOG"
iptables -I INPUT -i "$GO_IF" -j ACCEPT 2>/dev/null && say "firewall accepts $GO_IF"
[ -n "$PROXY" ] && nmcli con down "$PROXY" >/dev/null 2>&1 && say "proxy $PROXY down (so the route stays ours)"

# --- 2. TLS + WebSocket server -------------------------------------------
if [ ! -f "$CERT" ]; then
    openssl req -x509 -newkey rsa:2048 -keyout "$KEY" -out "$CERT" -days 3650 \
        -nodes -subj "/CN=localhost" 2>/dev/null
fi
WSLOG=/tmp/oshare-ws.log
PYTHONUNBUFFERED=1 python3 -I "$HERE/oshare_ws.py" --port "$PORT" \
    --cert "$CERT" --key "$KEY" --file "$FILE" --log "$WSLOG" \
    > /tmp/oshare-ws.out 2>&1 &
WS_PID=$!
sleep 1
ss -ltn | grep -q ":$PORT " || { say "server failed to listen on $PORT"; exit 1; }
say "serving $(basename "$FILE") on wss://192.168.49.1:$PORT/websocket"

# --- 3. BLE: advertise, and push the AP info when the phone asks ----------
say "starting BLE peer (advertising as $NAME)"
PYTHONUNBUFFERED=1 python3 -I "$HERE/oshare_ble.py" serve --name "$NAME" --no-ack \
    --share-port "$PORT" --share-ssid "$SSID" --share-psk "$PSK" --share-freq "$FREQ" \
    > /tmp/oshare-ble.out 2>&1 &
BLE_PID=$!
sleep 2

say "ready - open 互传 on the phone, pick a file, tap $NAME"
say "watching /tmp/oshare-ble.out and $WSLOG"

SENT=0
while kill -0 "$WS_PID" 2>/dev/null; do
    if grep -q "phone accepted the transfer request" "$WSLOG" 2>/dev/null; then
        SENT=1
        say "phone accepted, serving the file ..."
    fi
    if grep -q "sent .* for /download" "$WSLOG" 2>/dev/null; then
        say "transfer complete"
        break
    fi
    sleep 1
done

# Give the phone a moment to report status, then stop.
sleep 3
[ "$SENT" = 1 ] || say "no transfer happened (see $WSLOG)"
kill "$WS_PID" 2>/dev/null
