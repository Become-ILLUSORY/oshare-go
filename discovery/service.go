package discovery

import (
	"context"
	"net"
	"os"
	"strings"
	"time"
)

// Advertiser publishes this host as an OShare endpoint.
type Advertiser struct {
	Instance string            // "INSTANCE_NAME" + random suffix by default
	Port     int               // control port, 8959 in the Android app
	TXT      map[string]string // defaults: SERVICE_NAME=oshare, SERVICE_MAC=<mac>
	IP       net.IP            // optional override of the local address
	Interval time.Duration     // unsolicited announcement period, default 60s
}

// NewAdvertiser fills in the defaults the ColorOS app uses.
func NewAdvertiser(port int, mac string) *Advertiser {
	if mac == "" {
		mac = localMAC()
	}
	return &Advertiser{
		Instance: "INSTANCE_NAME" + randomHex(6),
		Port:     port,
		TXT: map[string]string{
			"SERVICE_NAME": "oshare",
			"SERVICE_MAC":  mac,
		},
	}
}

func (a *Advertiser) instanceFQDN() string {
	inst := a.Instance
	if inst == "" {
		inst = "INSTANCE_NAME" + randomHex(6)
	}
	return inst + "." + ServiceType
}

func (a *Advertiser) hostName() string {
	host, err := hostname()
	if err != nil || host == "" {
		host = "oshare-go"
	}
	return host + ".local"
}

func (a *Advertiser) ip() net.IP {
	if a.IP != nil {
		return a.IP
	}
	return primaryIPv4()
}

func (a *Advertiser) txtStrings() []string {
	out := make([]string, 0, len(a.TXT))
	for k, v := range a.TXT {
		out = append(out, k+"="+v)
	}
	return out
}

// Records returns the DNS-SD record set describing this endpoint.
func (a *Advertiser) Records() []record {
	return []record{
		{Name: ServiceType, Type: typePTR, NameData: a.instanceFQDN()},
		{Name: a.instanceFQDN(), Type: typeSRV, NameData: a.hostName(), Port: a.Port},
		{Name: a.instanceFQDN(), Type: typeTXT, TXT: a.txtStrings()},
		{Name: a.hostName(), Type: typeA, IP: a.ip()},
	}
}

// Answer builds a response for the given query questions.
func (a *Advertiser) Answer(questions []question) []byte {
	want := false
	for _, q := range questions {
		name := strings.ToLower(strings.TrimSuffix(q.Name, "."))
		if q.Type != typePTR && q.Type != typeANY {
			continue
		}
		if name == strings.ToLower(ServiceType) || name == "" {
			want = true
		}
	}
	if !want {
		return nil
	}
	return buildResponse(a.Records())
}

// Announce periodically multicasts the record set until ctx is done.
func (a *Advertiser) Announce(ctx context.Context, c *Conn) {
	interval := a.Interval
	if interval <= 0 {
		interval = 60 * time.Second
	}
	_ = c.send(buildResponse(a.Records()))
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			_ = c.send(buildResponse(a.Records()))
		}
	}
}

// Serve answers queries received on c. It also keeps announcing.
func (a *Advertiser) Serve(ctx context.Context, c *Conn) error {
	go a.Announce(ctx, c)
	buf := make([]byte, 9000)
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		_ = c.udp.SetReadDeadline(time.Now().Add(time.Second))
		n, err := c.read(buf)
		if err != nil {
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				continue
			}
			return err
		}
		msg, err := parseMessage(buf[:n])
		if err != nil || len(msg.questions) == 0 {
			continue
		}
		if resp := a.Answer(msg.questions); resp != nil {
			_ = c.send(resp)
		}
	}
}

// Browse sends PTR queries for the OShare service and reports peers as they
// are discovered or refreshed.
func Browse(ctx context.Context, c *Conn) (<-chan Peer, error) {
	out := make(chan Peer, 16)
	go func() {
		defer close(out)
		seen := map[string]Peer{}

		go func() { // query loop
			t := time.NewTicker(5 * time.Second)
			defer t.Stop()
			_ = c.send(buildQuery(ServiceType, typePTR))
			for {
				select {
				case <-ctx.Done():
					return
				case <-t.C:
					_ = c.send(buildQuery(ServiceType, typePTR))
				}
			}
		}()

		buf := make([]byte, 9000)
		for {
			if ctx.Err() != nil {
				return
			}
			_ = c.udp.SetReadDeadline(time.Now().Add(500 * time.Millisecond))
			n, err := c.read(buf)
			if err != nil {
				if ne, ok := err.(net.Error); ok && ne.Timeout() {
					continue
				}
				return
			}
			msg, err := parseMessage(buf[:n])
			if err != nil {
				continue
			}
			for _, p := range extractPeers(msg) {
				old, ok := seen[p.Instance]
				if ok && old.Port == p.Port && old.Addr.Equal(p.Addr) && old.MAC == p.MAC {
					continue
				}
				seen[p.Instance] = p
				select {
				case out <- p:
				case <-ctx.Done():
					return
				}
			}
		}
	}()
	return out, nil
}

// extractPeers assembles peers out of the answer section of one message. DNS-SD
// spreads PTR/SRV/TXT/A over several records, so only complete sets are
// returned.
func extractPeers(m *message) []Peer {
	type partial struct {
		peer   Peer
		srv    bool
		hasTXT bool
	}
	sets := map[string]*partial{}
	get := func(instance string) *partial {
		p, ok := sets[instance]
		if !ok {
			p = &partial{peer: Peer{Instance: instance, TXT: map[string]string{}}}
			sets[instance] = p
		}
		return p
	}
	for _, r := range m.answers {
		switch r.Type {
		case typePTR:
			if strings.HasSuffix(r.NameData, ServiceType) {
				get(r.NameData)
			}
		case typeSRV:
			p := get(r.Name)
			p.peer.Host = r.NameData
			p.peer.Port = r.Port
			p.srv = true
		case typeTXT:
			p := get(r.Name)
			p.hasTXT = true
			for _, kv := range r.TXT {
				if k, v, ok := strings.Cut(kv, "="); ok {
					p.peer.TXT[k] = v
				}
			}
		case typeA:
			for _, p := range sets {
				if p.peer.Host != "" && strings.EqualFold(p.peer.Host, r.Name) {
					p.peer.Addr = r.IP
				}
			}
		}
	}
	out := make([]Peer, 0, len(sets))
	for _, p := range sets {
		if !p.srv || p.peer.Addr == nil || p.peer.Port == 0 {
			continue
		}
		p.peer.Name = p.peer.TXT["SERVICE_NAME"]
		p.peer.MAC = p.peer.TXT["SERVICE_MAC"]
		p.peer.Seen = time.Now()
		out = append(out, p.peer)
	}
	return out
}

func localMAC() string {
	ifaces, err := net.Interfaces()
	if err != nil {
		return ""
	}
	for _, ifc := range ifaces {
		if ifc.Flags&net.FlagLoopback != 0 || len(ifc.HardwareAddr) == 0 {
			continue
		}
		return strings.ToUpper(strings.ReplaceAll(ifc.HardwareAddr.String(), ":", ""))
	}
	return ""
}

func primaryIPv4() net.IP {
	ifaces, err := net.Interfaces()
	if err != nil {
		return net.IPv4zero
	}
	for _, ifc := range ifaces {
		if ifc.Flags&net.FlagUp == 0 || ifc.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, err := ifc.Addrs()
		if err != nil {
			continue
		}
		for _, a := range addrs {
			if ipnet, ok := a.(*net.IPNet); ok {
				if ip4 := ipnet.IP.To4(); ip4 != nil {
					return ip4
				}
			}
		}
	}
	return net.IPv4zero
}

func randomHex(n int) string {
	const hexdigits = "0123456789ABCDEF"
	b := make([]byte, n)
	seed := time.Now().UnixNano()
	for i := range b {
		seed = seed*6364136223846793005 + 1442695040888963407
		b[i] = hexdigits[(seed>>33)&0xF]
	}
	return string(b)
}

func hostname() (string, error) { return os.Hostname() }
