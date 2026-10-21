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
    dnsmasq --interface="$GO_IF" --bind-dynamic \
            --dhcp-range=192.168.49.10,192.168.49.200,255.255.255.0,12h \
            --dhcp-option=3,"${GO_IP_CIDR%%/*}" --dhcp-option=6,"${GO_IP_CIDR%%/*}" \
            --dhcp-authoritative --log-dhcp \
            --no-resolv --no-hosts \
            --pid-file=/run/oshare-dnsmasq.pid --log-facility=/tmp/oshare-dnsmasq.log
    chmod 644 /tmp/oshare-dnsmasq.log 2>/dev/null || true
    say "dnsmasq started on $GO_IF (192.168.49.10-200)"
fi

GO_IP=$(ip -4 -o addr show dev "$GO_IF" | awk '{print $4}')
say "GO address: ${GO_IP:-<none>}"

# --- 4. TLS server on the port the phone was told ------------------------
if [ ! -f "$CERT" ]; then
    openssl req -x509 -newkey rsa:2048 -keyout "$KEY" -out "$CERT" -days 3650 \
        -nodes -subj "/CN=localhost" 2>/dev/null
    say "generated self-signed cert ($CERT)"
fi
say "serving https://${GO_IP%%/*}:$PORT/"
exec python3 -I - "$PORT" <<'PY'
import http.server, ssl, sys, time
port = int(sys.argv[1])

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"GET {self.path}", flush=True)
        for k, v in self.headers.items():
            print(f"    {k}: {v}", flush=True)
        body = b'{"result":0}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain("/tmp/oshare-srv.crt", "/tmp/oshare-srv.key")
srv = http.server.HTTPServer(("0.0.0.0", port), H)
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
print(f"listening on {port}", flush=True)
srv.serve_forever()
PY
