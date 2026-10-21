#!/bin/bash
# p2p_go.sh - make this laptop a Wi-Fi Direct group owner the way ColorOS 互传
# expects, then serve the OShare HTTP/TLS endpoint the phone will dial.
#
# Why: after we push {"port","mac","ssid","psk"} to the phone's 0x9953, the
# phone runs u9.n.fastConnectPeers -> manager.connect(deviceAddress=<our mac>,
# NetworkName=<ssid>, Passphrase=<psk>). That is a *P2P* connect, so a plain
# hostapd AP is invisible to it - our side has to be a P2P GO with exactly the
# SSID/PSK we advertised.
#
#   sudo ./tools/p2p_go.sh [SSID] [PSK] [PORT]
#
# Defaults match what tools/push_json.py sends, so run push_json with the same
# values afterwards.

set -euo pipefail

SSID="${1:-DIRECT-aa-fastCon}"
PSK="${2:-12345678}"
PORT="${3:-50856}"
IFACE="${IFACE:-wlan0}"
CTRL=/run/oshare-p2p
CONF=/run/oshare-p2p.conf
CERT=/tmp/oshare-srv.crt
KEY=/tmp/oshare-srv.key

say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

cleanup() {
    say "cleaning up"
    if [ "${IPT_RULE:-0}" = 1 ]; then
        iptables -D INPUT -i "$GO_IF" -j ACCEPT 2>/dev/null || true
    fi
    [ -f /run/oshare-dnsmasq.pid ] && kill "$(cat /run/oshare-dnsmasq.pid)" 2>/dev/null || true
    [ -S "$CTRL/$IFACE" ] && wpa_cli -p "$CTRL" -i "$IFACE" p2p_group_remove "$GO_IF" 2>/dev/null || true
    pkill -f "wpa_supplicant.*$CTRL" 2>/dev/null || true
    nmcli device set "$IFACE" managed yes 2>/dev/null || true
    say "done"
}
trap cleanup EXIT INT TERM

# --- 1. take wlan0 away from NetworkManager ------------------------------
say "unmanaging $IFACE"
nmcli device set "$IFACE" managed no
pkill -f "wpa_supplicant.*-i *$IFACE" 2>/dev/null || true
sleep 1

# --- 2. our own supplicant with P2P enabled ------------------------------
cat > "$CONF" <<EOF
ctrl_interface=$CTRL
update_config=1
device_name=LinuxBox
device_type=10-0050F204-5
config_methods=display push_button keypad virtual_push_button physical_display
p2p_add_cli_chan=1
p2p_no_group_iface=0
p2p_go_ht40=1
EOF
mkdir -p "$CTRL"
say "starting wpa_supplicant on $IFACE"
wpa_supplicant -B -i "$IFACE" -c "$CONF" -D nl80211 2>/dev/null || \
    wpa_supplicant -B -i "$IFACE" -c "$CONF"
sleep 2

# --- 3. a network block carrying our SSID/PSK, then group_add persistent -
NETID=$(wpa_cli -p "$CTRL" -i "$IFACE" add_network | tail -1)
wpa_cli -p "$CTRL" -i "$IFACE" set_network "$NETID" ssid "\"$SSID\"" >/dev/null
wpa_cli -p "$CTRL" -i "$IFACE" set_network "$NETID" psk "\"$PSK\"" >/dev/null
wpa_cli -p "$CTRL" -i "$IFACE" set_network "$NETID" mode 3 >/dev/null
wpa_cli -p "$CTRL" -i "$IFACE" set_network "$NETID" disabled 2 >/dev/null
say "group_add persistent=$NETID (ssid=$SSID)"
wpa_cli -p "$CTRL" -i "$IFACE" p2p_group_add persistent="$NETID" freq=2437 | tail -2

sleep 2
GO_IF=$(wpa_cli -p "$CTRL" -i "$IFACE" status | awk -F= '/^p2p_group_iface|^wpa_state/{ } /ifname/{print $2}' | head -1)
GO_IF=${GO_IF:-$(iw dev | awk '/Interface p2p-/{print $2; exit}')}
say "GO interface: ${GO_IF:-<none>}"
[ -n "${GO_IF:-}" ] || { say "no group interface - group_add failed"; exit 1; }

# The GO is the AP: wpa_supplicant does not assign an address or run DHCP, so
# the phone sits in DHCPDISCOVER forever unless we do it. ColorOS expects the
# peer to be 192.168.49.1 - that is what its own GO uses and what the captures
# show the receiver dialing.
GO_IP_CIDR="${GO_IP_CIDR:-192.168.49.1/24}"
if ! ip -4 addr show dev "$GO_IF" | grep -q "$GO_IP_CIDR"; then
    ip addr add "$GO_IP_CIDR" dev "$GO_IF"
    say "assigned $GO_IP_CIDR to $GO_IF"
fi
ip link set "$GO_IF" up

if ! pgrep -f "dnsmasq.*$GO_IF" >/dev/null; then
    # --bind-dynamic, not --bind-interfaces: the P2P GO interface appears after
    # wpa_supplicant creates the group, so a static bind misses its sockets.
    # --log-dhcp makes the offer/ack visible in the log when the phone gives up.
    rm -f /run/oshare-dnsmasq.pid
    DHCPLOG=/tmp/oshare-dnsmasq-$$.log
    : > "$DHCPLOG"; chmod 644 "$DHCPLOG"
    dnsmasq --interface="$GO_IF" --bind-dynamic \
            --dhcp-range=192.168.49.10,192.168.49.200,255.255.255.0,12h \
            --dhcp-option=3,"${GO_IP_CIDR%%/*}" --dhcp-option=6,"${GO_IP_CIDR%%/*}" \
            --dhcp-authoritative --log-dhcp \
            --no-resolv --no-hosts \
            --pid-file=/run/oshare-dnsmasq.pid --log-facility="$DHCPLOG"
    say "dnsmasq started on $GO_IF (192.168.49.10-200), log $DHCPLOG"
fi

GO_IP=$(ip -4 -o addr show dev "$GO_IF" | awk '{print $4}')
say "GO address: ${GO_IP:-<none>}"

# A firewall that defaults to DROP will silently eat the phone's DHCPDISCOVER:
# the interface counters still go up, so it looks like dnsmasq is ignoring it.
if command -v iptables >/dev/null 2>&1; then
    iptables -I INPUT -i "$GO_IF" -j ACCEPT 2>/dev/null && {
        say "iptables: INPUT accept on $GO_IF"
        IPT_RULE=1
    }
fi

# --- 4. HTTPS + WebSocket control channel --------------------------------
if [ ! -f "$CERT" ]; then
    openssl req -x509 -newkey rsa:2048 -keyout "$KEY" -out "$CERT" -days 3650 \
        -nodes -subj "/CN=localhost" 2>/dev/null
    say "generated self-signed cert ($CERT)"
fi
say "serving wss://${GO_IP%%/*}:$PORT/websocket"
exec python3 -I "$(dirname "$0")/oshare_ws.py" --port "$PORT" --cert "$CERT" --key "$KEY"
