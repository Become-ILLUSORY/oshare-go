package protocol

import (
	"strconv"
	"strings"
)

// Separator joins multi-field command payloads (pb/a.java: j(String[]) and
// m(String) in com.coloros.oshare 17.2.36).
const Separator = "&&_&&"

// Command codes. Recovered from r9/b.java, r9/g.java,
// com/oplus/oshare/transfer/msg and the seeding/UI call sites.
//
// Direction legend: S -> R means sender (file source) to receiver.
const (
	// CmdKeepAlive is the command-frame keepalive (cmd 0, flag 1, empty
	// payload) used right after a session is established.
	CmdKeepAlive int32 = 0

	// CmdAnnounce (S -> R) starts a transfer. Payload fields:
	// 0 number of entries, 1 sender device name, 2 total bytes,
	// 3 destination directory hint, 4 data type
	// ("file/*", "image/*", "video/*", "text/x-vcard", "text/plain",
	// "http/*", "vision/*"), 5 optional paired version,
	// 6 optional account id, 7 optional verification key.
	CmdAnnounce int32 = 1

	// CmdAckAllow (R -> S) accepts; payload is the negotiated data type.
	CmdAckAllow int32 = 2

	// CmdAckReject (R -> S) refuses; payload "reject" or "cancel".
	CmdAckReject int32 = 3

	// CmdAckTimeout (R -> S) the user did not answer in time (payload "TEST").
	CmdAckTimeout int32 = 4

	// CmdTransferOver is sent by both sides when the payload is done.
	// S -> R: {sent count, data type, destination directory, entrance}
	// R -> S: data type.
	CmdTransferOver int32 = 5

	// CmdNoSpace (R -> S) not enough free space (payload "TEST").
	CmdNoSpace int32 = 6

	// CmdCancel (R -> S) aborts the transfer (payload "TEST").
	CmdCancel int32 = 7

	// CmdCancelOK (S -> R) acknowledges a cancel (payload "TEST").
	CmdCancelOK int32 = 8

	// CmdThumbnail (S -> R) one element: encoded preview + 8 byte key.
	CmdThumbnail int32 = 9

	// CmdShortText (S -> R) one element: the shared text.
	CmdShortText int32 = 10

	// CmdClientInfo (R -> S) sent on connect: {device id, unknown, version}.
	CmdClientInfo int32 = 11

	// CmdProgress (R -> S) percent as decimal string.
	CmdProgress int32 = 12

	// CmdNothing (S -> R) keepalive-ish no-op (payload "TEST").
	CmdNothing int32 = 13

	// CmdServerInfo (S -> R) reply to CmdClientInfo: {device id}.
	CmdServerInfo int32 = 21

	// CmdLogInstruction (either) payload is a log directive.
	CmdLogInstruction int32 = 22
)

// Data types used in announcements.
const (
	DataTypeFile   = "file/*"
	DataTypeImage  = "image/*"
	DataTypeVideo  = "video/*"
	DataTypeVCard  = "text/x-vcard"
	DataTypeText   = "text/plain"
	DataTypeHTTP   = "http/*"
	DataTypeVision = "vision/*"
)

// Dummy payload used by the original app where the payload carries no data.
const DummyPayload = "TEST"

// JoinFields joins payload fields with Separator.
func JoinFields(fields ...string) string { return strings.Join(fields, Separator) }

// SplitFields splits a payload created with JoinFields.
func SplitFields(payload string) []string {
	if payload == "" {
		return nil
	}
	return strings.Split(payload, Separator)
}

// Field returns the i-th field or "".
func Field(payload string, i int) string {
	f := SplitFields(payload)
	if i < 0 || i >= len(f) {
		return ""
	}
	return f[i]
}

// Announce is the decoded CmdAnnounce payload.
type Announce struct {
	Count      int64
	DeviceName string
	TotalSize  int64
	DirHint    string
	DataType   string
	Version    string
	AccountID  string
	VerifyKey  string
}

// ParseAnnounce decodes a CmdAnnounce payload.
func ParseAnnounce(payload string) Announce {
	f := SplitFields(payload)
	get := func(i int) string {
		if i < len(f) {
			return f[i]
		}
		return ""
	}
	count, _ := strconv.ParseInt(strings.TrimSpace(get(0)), 10, 64)
	total, _ := strconv.ParseInt(strings.TrimSpace(get(2)), 10, 64)
	return Announce{
		Count:      count,
		DeviceName: get(1),
		TotalSize:  total,
		DirHint:    get(3),
		DataType:   get(4),
		Version:    get(5),
		AccountID:  get(6),
		VerifyKey:  get(7),
	}
}

// Encode serialises the announcement back into a payload string.
func (a Announce) Encode() string {
	fields := []string{
		strconv.FormatInt(a.Count, 10),
		a.DeviceName,
		strconv.FormatInt(a.TotalSize, 10),
		a.DirHint,
		a.DataType,
	}
	if a.Version != "" || a.AccountID != "" || a.VerifyKey != "" {
		fields = append(fields, a.Version, a.AccountID, a.VerifyKey)
	}
	return JoinFields(fields...)
}

// Over is the decoded CmdTransferOver payload.
type Over struct {
	Count    int64
	DataType string
	Dir      string
	Entrance int64
}

// ParseOver decodes a CmdTransferOver payload. The receiver-side variant only
// carries the data type, so missing fields decode to zero values.
func ParseOver(payload string) Over {
	f := SplitFields(payload)
	get := func(i int) string {
		if i < len(f) {
			return f[i]
		}
		return ""
	}
	count, _ := strconv.ParseInt(strings.TrimSpace(get(0)), 10, 64)
	entrance, _ := strconv.ParseInt(strings.TrimSpace(get(3)), 10, 64)
	return Over{Count: count, DataType: get(1), Dir: get(2), Entrance: entrance}
}

// Encode serialises the over payload.
func (o Over) Encode() string {
	return JoinFields(
		strconv.FormatInt(o.Count, 10),
		o.DataType,
		o.Dir,
		strconv.FormatInt(o.Entrance, 10),
	)
}

// ClientInfo is the decoded CmdClientInfo payload.
type ClientInfo struct {
	DeviceID string
	Unknown  string
	Version  string
}

// ParseClientInfo decodes a CmdClientInfo payload.
func ParseClientInfo(payload string) ClientInfo {
	f := SplitFields(payload)
	get := func(i int) string {
		if i < len(f) {
			return f[i]
		}
		return ""
	}
	return ClientInfo{DeviceID: get(0), Unknown: get(1), Version: get(2)}
}

// Encode serialises the client info payload.
func (c ClientInfo) Encode() string {
	return JoinFields(c.DeviceID, c.Unknown, c.Version)
}

// ParsePercent decodes a CmdProgress payload.
func ParsePercent(payload string) int {
	v, err := strconv.Atoi(strings.TrimSpace(payload))
	if err != nil {
		return 0
	}
	if v < 0 {
		return 0
	}
	if v > 100 {
		return 100
	}
	return v
}
