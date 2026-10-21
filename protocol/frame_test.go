package protocol

import (
	"bytes"
	"encoding/binary"
	"io"
	"reflect"
	"strings"
	"testing"
)

// buildCommandFrame builds a command frame manually, independently of the
// Writer implementation, following mb/f.java: b() + putInt(cmd) + putInt(len) +
// bytes + putInt(flag), then the payload length is patched in at offset 8.
func buildCommandFrame(cmd int32, payload string, flag int32) []byte {
	var b bytes.Buffer
	_ = binary.Write(&b, binary.BigEndian, int32(Magic))
	_ = binary.Write(&b, binary.BigEndian, TypeCommand)
	_ = binary.Write(&b, binary.BigEndian, int64(12+len(payload)))
	_ = binary.Write(&b, binary.BigEndian, cmd)
	_ = binary.Write(&b, binary.BigEndian, int32(len(payload)))
	b.WriteString(payload)
	_ = binary.Write(&b, binary.BigEndian, flag)
	return b.Bytes()
}

// buildFileFrame builds a file frame as mb/i.java does: the length field at
// offset 8 is the total frame size, the field at offset 16 is the body size.
func buildFileFrame(h FileHeader, body []byte) []byte {
	var b bytes.Buffer
	bodyLen := int64(28 + len(h.Name))
	_ = binary.Write(&b, binary.BigEndian, int32(Magic))
	_ = binary.Write(&b, binary.BigEndian, TypeFile)
	_ = binary.Write(&b, binary.BigEndian, int64(16+bodyLen))
	_ = binary.Write(&b, binary.BigEndian, bodyLen)
	_ = binary.Write(&b, binary.BigEndian, int32(len(h.Name)))
	b.WriteString(h.Name)
	_ = binary.Write(&b, binary.BigEndian, h.FileType)
	_ = binary.Write(&b, binary.BigEndian, h.Size)
	_ = binary.Write(&b, binary.BigEndian, h.Flag)
	b.Write(body)
	return b.Bytes()
}

func TestCommandFrameGolden(t *testing.T) {
	payload := JoinFields("3", "Pixel 8", "12345", "/sdcard/Download", DataTypeImage)
	want := buildCommandFrame(CmdAnnounce, payload, 1)

	var buf bytes.Buffer
	w := NewWriter(&buf)
	if err := w.WriteCommand(CmdAnnounce, payload, 1); err != nil {
		t.Fatalf("WriteCommand: %v", err)
	}
	if !bytes.Equal(buf.Bytes(), want) {
		t.Fatalf("command frame mismatch\n got %x\nwant %x", buf.Bytes(), want)
	}
	if payloadLen := binary.BigEndian.Uint64(want[8:16]); int(payloadLen) != 12+len(payload) {
		t.Fatalf("length field = %d, want %d", payloadLen, 12+len(payload))
	}

	r := NewReader(bytes.NewReader(want))
	f, err := r.Next()
	if err != nil {
		t.Fatalf("Next: %v", err)
	}
	if f.Type != TypeCommand || f.Command.Cmd != CmdAnnounce || f.Command.Payload != payload || f.Command.Flag != 1 {
		t.Fatalf("decoded %+v", f.Command)
	}
	a := ParseAnnounce(f.Command.Payload)
	if a.Count != 3 || a.DeviceName != "Pixel 8" || a.TotalSize != 12345 ||
		a.DirHint != "/sdcard/Download" || a.DataType != DataTypeImage {
		t.Fatalf("announce = %+v", a)
	}
}

func TestHeartbeatFrame(t *testing.T) {
	var buf bytes.Buffer
	w := NewWriter(&buf)
	if err := w.WriteHeartbeat(1); err != nil {
		t.Fatalf("WriteHeartbeat: %v", err)
	}
	if got, want := buf.Len(), 20; got != want {
		t.Fatalf("heartbeat frame size = %d, want %d", got, want)
	}
	f, err := NewReader(bytes.NewReader(buf.Bytes())).Next()
	if err != nil {
		t.Fatalf("Next: %v", err)
	}
	if f.Type != TypeHeartbeat || f.Heartbeat != 1 {
		t.Fatalf("heartbeat = %+v", f)
	}
}

func TestFileFrameRoundTrip(t *testing.T) {
	body := bytes.Repeat([]byte("oshare"), 1000)
	h := FileHeader{Name: "ColorOS/Oshare/IMG_0001.jpg", FileType: 1, Size: int64(len(body)), Flag: FileFlagLocalFile}

	var buf bytes.Buffer
	w := NewWriter(&buf)
	if err := w.SendFile(h, bytes.NewReader(body), nil); err != nil {
		t.Fatalf("SendFile: %v", err)
	}
	if !bytes.Equal(buf.Bytes(), buildFileFrame(h, body)) {
		t.Fatalf("file frame mismatch")
	}

	r := NewReader(&buf)
	f, err := r.Next()
	if err != nil {
		t.Fatalf("Next: %v", err)
	}
	if f.Type != TypeFile || !reflect.DeepEqual(*f.File, h) {
		t.Fatalf("header = %+v, want %+v", f.File, h)
	}
	var got bytes.Buffer
	n, err := r.CopyFileBody(f.File, &got, nil)
	if err != nil {
		t.Fatalf("CopyFileBody: %v", err)
	}
	if n != int64(len(body)) || !bytes.Equal(got.Bytes(), body) {
		t.Fatalf("body mismatch: n=%d", n)
	}
}

func TestSequentialFramesInterleavedWithFileBody(t *testing.T) {
	// The production decoder must cope with a command frame that arrives right
	// after a file body, as the sender never waits for an ack.
	var buf bytes.Buffer
	w := NewWriter(&buf)
	if err := w.SendFile(FileHeader{Name: "a.txt", Size: 3}, strings.NewReader("abc"), nil); err != nil {
		t.Fatal(err)
	}
	if err := w.WriteCommand(CmdTransferOver, JoinFields("1", DataTypeFile, "/tmp", "0"), 1); err != nil {
		t.Fatal(err)
	}

	r := NewReader(&buf)
	f, err := r.Next()
	if err != nil || f.Type != TypeFile {
		t.Fatalf("first frame = %v, %v", f, err)
	}
	if err := r.DiscardFileBody(f.File); err != nil {
		t.Fatal(err)
	}
	f, err = r.Next()
	if err != nil {
		t.Fatalf("second frame: %v", err)
	}
	if f.Command.Cmd != CmdTransferOver {
		t.Fatalf("second frame cmd = %d", f.Command.Cmd)
	}
	if o := ParseOver(f.Command.Payload); o.Count != 1 || o.DataType != DataTypeFile || o.Dir != "/tmp" {
		t.Fatalf("over = %+v", o)
	}
}

func TestBadMagic(t *testing.T) {
	buf := make([]byte, 16)
	binary.BigEndian.PutUint32(buf[0:4], 4242)
	_, err := NewReader(bytes.NewReader(buf)).Next()
	if err == nil || !strings.Contains(err.Error(), "magic") {
		t.Fatalf("err = %v, want bad magic", err)
	}
}

func TestTruncatedBody(t *testing.T) {
	frame := buildFileFrame(FileHeader{Name: "a.bin", Size: 10}, []byte("short"))
	r := NewReader(bytes.NewReader(frame))
	f, err := r.Next()
	if err != nil {
		t.Fatal(err)
	}
	_, err = io.Copy(io.Discard, io.LimitReader(r.r, 0))
	if err != nil {
		t.Fatal(err)
	}
	var sink bytes.Buffer
	if _, err := r.CopyFileBody(f.File, &sink, nil); err == nil {
		t.Fatal("expected an error for a truncated file body")
	}
}

func TestSplitJoinFields(t *testing.T) {
	fields := []string{"a", "", "c&&_&&d", "e"}
	got := SplitFields(JoinFields(fields...))
	want := []string{"a", "", "c", "d", "e"} // separator is not escaped by the original app
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("split = %q, want %q", got, want)
	}
	if Field("x&&_&&y", 1) != "y" || Field("x", 5) != "" {
		t.Fatal("Field failed")
	}
}
