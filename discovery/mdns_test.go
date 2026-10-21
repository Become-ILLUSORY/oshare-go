package discovery

import (
	"encoding/binary"
	"net"
	"testing"
)

func bin16(b []byte, v uint16) []byte { return binary.BigEndian.AppendUint16(b, v) }
func bin32(b []byte, v uint32) []byte { return binary.BigEndian.AppendUint32(b, v) }

func TestAdvertiserRecordsRoundTrip(t *testing.T) {
	adv := &Advertiser{
		Instance: "INSTANCE_NAMEAB12CD",
		Port:     8959,
		TXT:      map[string]string{"SERVICE_NAME": "oshare", "SERVICE_MAC": "AABBCCDDEEFF"},
		IP:       net.IPv4(192, 168, 1, 5),
	}

	// Only answer PTR/ANY queries for our service type.
	resp := adv.Answer([]question{{Name: ServiceType, Type: typePTR}})
	if resp == nil {
		t.Fatal("no answer for a matching query")
	}
	if adv.Answer([]question{{Name: "_other._tcp.local", Type: typePTR}}) != nil {
		t.Fatal("answered a query for another service")
	}

	msg, err := parseMessage(resp)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	peers := extractPeers(msg)
	if len(peers) != 1 {
		t.Fatalf("got %d peers, want 1: %+v", len(peers), peers)
	}
	p := peers[0]
	if p.Port != 8959 || !p.Addr.Equal(net.IPv4(192, 168, 1, 5)) {
		t.Fatalf("peer = %+v", p)
	}
	if p.Name != "oshare" || p.MAC != "AABBCCDDEEFF" {
		t.Fatalf("txt = %+v", p.TXT)
	}
	if got := p.Address(); got != "192.168.1.5:8959" {
		t.Fatalf("address = %s", got)
	}
	if trimSuffix(p.Instance) != "INSTANCE_NAMEAB12CD" {
		t.Fatalf("instance = %s", p.Instance)
	}
}

// TestCompressedNames checks the pointer handling that real mDNS responders
// rely on heavily.
func TestCompressedNames(t *testing.T) {
	b := make([]byte, 12)
	binary.BigEndian.PutUint16(b[2:4], 0x8400) // response
	binary.BigEndian.PutUint16(b[6:8], 4)      // 4 answers

	// 1. PTR "_oshare._tcp.local" -> "INST._oshare._tcp.local" (compressed tail)
	svcOff := len(b)
	b = encodeName(b, ServiceType)
	b = bin16(b, typePTR)
	b = bin16(b, classINET)
	b = bin32(b, 120)
	rdLenOff := len(b)
	b = append(b, 0, 0)
	rdStart := len(b)
	instOff := len(b)
	b = append(b, 4)
	b = append(b, []byte("INST")...)
	b = append(b, 0xC0|byte(svcOff>>8), byte(svcOff))
	binary.BigEndian.PutUint16(b[rdLenOff:], uint16(len(b)-rdStart))

	// 2. SRV with a name that is a bare pointer to the instance
	b = append(b, 0xC0|byte(instOff>>8), byte(instOff))
	b = bin16(b, typeSRV)
	b = bin16(b, classINET)
	b = bin32(b, 120)
	rdLenOff = len(b)
	b = append(b, 0, 0)
	rdStart = len(b)
	b = bin16(b, 0)
	b = bin16(b, 0)
	b = bin16(b, 50489)
	b = encodeName(b, "linux-box.local")
	binary.BigEndian.PutUint16(b[rdLenOff:], uint16(len(b)-rdStart))

	// 3. TXT, name = pointer to the instance
	b = append(b, 0xC0|byte(instOff>>8), byte(instOff))
	b = bin16(b, typeTXT)
	b = bin16(b, classINET|cacheFlush)
	b = bin32(b, 120)
	rdLenOff = len(b)
	b = append(b, 0, 0)
	rdStart = len(b)
	for _, kv := range []string{"SERVICE_NAME=oshare", "SERVICE_MAC=112233445566"} {
		b = append(b, byte(len(kv)))
		b = append(b, []byte(kv)...)
	}
	binary.BigEndian.PutUint16(b[rdLenOff:], uint16(len(b)-rdStart))

	// 4. A record for linux-box.local
	b = encodeName(b, "linux-box.local")
	b = bin16(b, typeA)
	b = bin16(b, classINET|cacheFlush)
	b = bin32(b, 120)
	b = bin16(b, 4)
	b = append(b, 10, 0, 0, 7)

	msg, err := parseMessage(b)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	peers := extractPeers(msg)
	if len(peers) != 1 {
		t.Fatalf("got %d peers, want 1", len(peers))
	}
	p := peers[0]
	if p.Instance != "INST."+ServiceType {
		t.Fatalf("instance = %q", p.Instance)
	}
	if p.Port != 50489 || !p.Addr.Equal(net.IPv4(10, 0, 0, 7)) || p.Host != "linux-box.local" {
		t.Fatalf("peer = %+v", p)
	}
	if p.MAC != "112233445566" {
		t.Fatalf("mac = %q", p.MAC)
	}
}

func TestParseQueryAndAnswer(t *testing.T) {
	q := buildQuery(ServiceType, typePTR)
	msg, err := parseMessage(q)
	if err != nil {
		t.Fatalf("parse query: %v", err)
	}
	if len(msg.questions) != 1 || msg.questions[0].Name != ServiceType || msg.questions[0].Type != typePTR {
		t.Fatalf("questions = %+v", msg.questions)
	}
	if binary.BigEndian.Uint16(q[4:6]) != 1 {
		t.Fatal("QDCOUNT not set")
	}
}

func TestTruncatedMessage(t *testing.T) {
	if _, err := parseMessage([]byte{0, 0, 0}); err == nil {
		t.Fatal("expected an error for a short message")
	}
	resp := buildResponse([]record{{Name: ServiceType, Type: typePTR, NameData: "x." + ServiceType}})
	if _, err := parseMessage(resp[:len(resp)-3]); err == nil {
		t.Fatal("expected an error for a truncated rdata")
	}
}
