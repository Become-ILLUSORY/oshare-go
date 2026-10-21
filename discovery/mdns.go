// Package discovery implements the mDNS (DNS-SD) subset used by OShare.
//
// The Android app announces itself through NsdManager (u9/n.java) with
// service type "_oshare._tcp", instance name "INSTANCE_NAME…", port 50489 and
// the TXT key/value pairs SERVICE_NAME=oshare and SERVICE_MAC=<mac>. This
// package speaks the same records on the wire, so a Linux box calls itself the
// way a ColorOS phone does and can find phones that are already browsing.
package discovery

import (
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"strings"
	"sync"
	"time"
)

// ServiceType is the DNS-SD service the OShare app registers.
const (
	ServiceType   = "_oshare._tcp.local"
	MDNSGroupAddr = "224.0.0.251:5353"
	DefaultTTL    = 120
)

// DNS record types used by DNS-SD.
const (
	typeA      uint16 = 1
	typePTR    uint16 = 12
	typeTXT    uint16 = 16
	typeSRV    uint16 = 33
	typeANY    uint16 = 255
	classINET         = 1
	cacheFlush        = 0x8000
)

// Peer is a discovered OShare endpoint.
type Peer struct {
	Instance string            // full instance name, e.g. "INSTANCE_NAME…._oshare._tcp.local"
	Name     string            // SERVICE_NAME TXT value ("oshare")
	MAC      string            // SERVICE_MAC TXT value
	Host     string            // SRV target hostname
	Addr     net.IP            // A record
	Port     int               // SRV port, the control channel
	TXT      map[string]string // raw TXT records
	Seen     time.Time
}

// Address returns the dial address for the peer.
func (p Peer) Address() string {
	if p.Addr == nil {
		return ""
	}
	return net.JoinHostPort(p.Addr.String(), fmt.Sprint(p.Port))
}

func (p Peer) String() string {
	return fmt.Sprintf("%s (%s) %s mac=%s", trimSuffix(p.Instance), p.Address(), p.Name, p.MAC)
}

func trimSuffix(instance string) string {
	s := strings.TrimSuffix(instance, ".")
	return strings.TrimSuffix(s, "."+ServiceType)
}

// ---------------------------------------------------------------------------
// DNS wire format
// ---------------------------------------------------------------------------

type question struct {
	Name  string
	Type  uint16
	Class uint16
}

type record struct {
	Name  string
	Type  uint16
	Class uint16
	TTL   uint32
	// Exactly one of the following is used, depending on Type.
	NameData string // PTR, SRV target
	IP       net.IP // A
	TXT      []string
	Port     int // SRV
}

// encodeName writes a DNS name without compression.
func encodeName(buf []byte, name string) []byte {
	name = strings.TrimSuffix(name, ".")
	if name == "" {
		return append(buf, 0)
	}
	for _, label := range strings.Split(name, ".") {
		if len(label) > 63 {
			label = label[:63]
		}
		buf = append(buf, byte(len(label)))
		buf = append(buf, label...)
	}
	return append(buf, 0)
}

// decodeName reads a (possibly compressed) name, returning it and the offset
// just past the name at the original position.
func decodeName(msg []byte, off int) (string, int, error) {
	var labels []string
	next := -1
	for {
		if off >= len(msg) {
			return "", 0, errors.New("mdns: truncated name")
		}
		l := int(msg[off])
		if l == 0 {
			off++
			break
		}
		if l&0xC0 == 0xC0 {
			if off+1 >= len(msg) {
				return "", 0, errors.New("mdns: truncated pointer")
			}
			ptr := int(binary.BigEndian.Uint16(msg[off:off+2]) & 0x3FFF)
			if next < 0 {
				next = off + 2
			}
			off = ptr
			continue
		}
		if off+1+l > len(msg) {
			return "", 0, errors.New("mdns: truncated label")
		}
		labels = append(labels, string(msg[off+1:off+1+l]))
		off += 1 + l
	}
	if next < 0 {
		next = off
	}
	return strings.Join(labels, "."), next, nil
}

// buildQuery serialises a single-question query.
func buildQuery(name string, qtype uint16) []byte {
	buf := make([]byte, 12, 12+len(name)+6)
	binary.BigEndian.PutUint16(buf[4:6], 1) // QDCOUNT
	buf = encodeName(buf, name)
	buf = binary.BigEndian.AppendUint16(buf, qtype)
	buf = binary.BigEndian.AppendUint16(buf, classINET)
	return buf
}

// buildResponse serialises answers (used for announcements and replies).
func buildResponse(records []record) []byte {
	buf := make([]byte, 12)
	binary.BigEndian.PutUint16(buf[2:4], 0x8400) // response, authoritative
	binary.BigEndian.PutUint16(buf[6:8], uint16(len(records)))
	for _, r := range records {
		buf = encodeName(buf, r.Name)
		buf = binary.BigEndian.AppendUint16(buf, r.Type)
		cls := r.Class
		if cls == 0 {
			cls = classINET
		}
		if r.Type != typePTR {
			cls |= cacheFlush
		}
		buf = binary.BigEndian.AppendUint16(buf, cls)
		ttl := r.TTL
		if ttl == 0 {
			ttl = DefaultTTL
		}
		buf = binary.BigEndian.AppendUint32(buf, ttl)
		lenOff := len(buf)
		buf = append(buf, 0, 0)
		start := len(buf)
		switch r.Type {
		case typePTR:
			buf = encodeName(buf, r.NameData)
		case typeSRV:
			buf = binary.BigEndian.AppendUint16(buf, 0) // priority
			buf = binary.BigEndian.AppendUint16(buf, 0) // weight
			buf = binary.BigEndian.AppendUint16(buf, uint16(r.Port))
			buf = encodeName(buf, r.NameData)
		case typeTXT:
			if len(r.TXT) == 0 {
				buf = append(buf, 0)
			}
			for _, s := range r.TXT {
				if len(s) > 255 {
					s = s[:255]
				}
				buf = append(buf, byte(len(s)))
				buf = append(buf, s...)
			}
		case typeA:
			ip := r.IP.To4()
			if ip == nil {
				ip = net.IPv4zero.To4()
			}
			buf = append(buf, ip...)
		}
		binary.BigEndian.PutUint16(buf[lenOff:], uint16(len(buf)-start))
	}
	return buf
}

type message struct {
	questions []question
	answers   []record
}

func parseMessage(msg []byte) (*message, error) {
	if len(msg) < 12 {
		return nil, errors.New("mdns: short message")
	}
	qd := int(binary.BigEndian.Uint16(msg[4:6]))
	an := int(binary.BigEndian.Uint16(msg[6:8]))
	ns := int(binary.BigEndian.Uint16(msg[8:10]))
	ar := int(binary.BigEndian.Uint16(msg[10:12]))
	off := 12
	m := &message{}
	for i := 0; i < qd; i++ {
		name, next, err := decodeName(msg, off)
		if err != nil {
			return nil, err
		}
		off = next
		if off+4 > len(msg) {
			return nil, errors.New("mdns: truncated question")
		}
		m.questions = append(m.questions, question{
			Name:  name,
			Type:  binary.BigEndian.Uint16(msg[off : off+2]),
			Class: binary.BigEndian.Uint16(msg[off+2:off+4]) &^ cacheFlush,
		})
		off += 4
	}
	for i := 0; i < an+ns+ar; i++ {
		name, next, err := decodeName(msg, off)
		if err != nil {
			return nil, err
		}
		off = next
		if off+10 > len(msg) {
			return nil, errors.New("mdns: truncated record")
		}
		r := record{
			Name:  name,
			Type:  binary.BigEndian.Uint16(msg[off : off+2]),
			Class: binary.BigEndian.Uint16(msg[off+2:off+4]) &^ cacheFlush,
			TTL:   binary.BigEndian.Uint32(msg[off+4 : off+8]),
		}
		rdlen := int(binary.BigEndian.Uint16(msg[off+8 : off+10]))
		off += 10
		if off+rdlen > len(msg) {
			return nil, errors.New("mdns: truncated rdata")
		}
		data := msg[off : off+rdlen]
		switch r.Type {
		case typePTR:
			target, _, err := decodeName(msg, off)
			if err != nil {
				return nil, err
			}
			r.NameData = target
		case typeSRV:
			if rdlen < 7 {
				return nil, errors.New("mdns: short SRV")
			}
			r.Port = int(binary.BigEndian.Uint16(data[4:6]))
			target, _, err := decodeName(msg, off+6)
			if err != nil {
				return nil, err
			}
			r.NameData = target
		case typeTXT:
			for i := 0; i < len(data); {
				l := int(data[i])
				if l == 0 || i+1+l > len(data) {
					break
				}
				r.TXT = append(r.TXT, string(data[i+1:i+1+l]))
				i += 1 + l
			}
		case typeA:
			if rdlen == 4 {
				r.IP = net.IPv4(data[0], data[1], data[2], data[3])
			}
		}
		m.answers = append(m.answers, r)
		off += rdlen
	}
	return m, nil
}

// ---------------------------------------------------------------------------
// Socket
// ---------------------------------------------------------------------------

// Conn is a shared mDNS socket. One process should own a single Conn and use
// it both to browse and to advertise.
type Conn struct {
	udp     *net.UDPConn
	writeMu sync.Mutex
}

// Open binds 0.0.0.0:5353 and joins the mDNS group.
func Open() (*Conn, error) {
	group, err := net.ResolveUDPAddr("udp4", MDNSGroupAddr)
	if err != nil {
		return nil, err
	}
	c, err := net.ListenMulticastUDP("udp4", nil, group)
	if err != nil {
		return nil, fmt.Errorf("mdns: listen %s: %w", MDNSGroupAddr, err)
	}
	_ = c.SetReadBuffer(1 << 20)
	return &Conn{udp: c}, nil
}

// Close releases the socket.
func (c *Conn) Close() error { return c.udp.Close() }

func (c *Conn) send(b []byte) error {
	addr, err := net.ResolveUDPAddr("udp4", MDNSGroupAddr)
	if err != nil {
		return err
	}
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	_, err = c.udp.WriteToUDP(b, addr)
	return err
}

// read returns the next packet and its length.
func (c *Conn) read(buf []byte) (int, error) { return c.udp.Read(buf) }

// InstanceLabel returns the human friendly label of an instance name
// ("INSTANCE_NAMEABC._oshare._tcp.local" -> "INSTANCE_NAMEABC").
func InstanceLabel(instance string) string { return trimSuffix(instance) }
