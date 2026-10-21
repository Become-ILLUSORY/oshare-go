// Package protocol implements the OShare (ColorOS / OPPO 互传) LAN wire format.
//
// The format was recovered from com.coloros.oshare 17.2.36
// (mb/c.java codec factory, mb/f.java encoder, mb/d.java decoder,
// mb/i.java file header writer). Every frame is big-endian:
//
//	+--------+--------+----------+---------------------------+
//	| magic  | type   | length   | payload ...               |
//	| int32  | int32  | int64    |                           |
//	+--------+--------+----------+---------------------------+
//
//	magic  always 1001 (0x000003E9)
//	type   4096 = command, 8192 = file, 16384 = keepalive
//	length depends on type (see below)
//
// Command payload (type 4096):
//
//	int32 cmd
//	int32 nameLen + nameLen bytes (UTF-8)
//	int32 flag
//	length = 12 + nameLen          (payload only, header excluded)
//
// Keepalive payload (type 16384):
//
//	int32 value                    (1 = request, 2 = response)
//	length = 4
//
// File payload (type 8192):
//
//	int64 bodyLen                  (= 28 + nameLen, counts this field)
//	int32 nameLen + nameLen bytes  (destination name/path, UTF-8)
//	int32 fileType
//	int64 size
//	int32 flag
//	size bytes of raw file data
//	length = 44 + nameLen          (whole frame, header included)
//
// The length field of a file frame counts the 16 byte header, unlike the
// command/keepalive frames, because that is what mb/i.java writes and what
// mb/d.java expects.
package protocol

import (
	"bufio"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"sync"
)

// Wire constants.
const (
	Magic = 1001

	TypeCommand   int32 = 4096
	TypeFile      int32 = 8192
	TypeHeartbeat int32 = 16384
)

// File flag bits (see mb/d.java: a file is a directory when both bit 2 and
// bit 4 are set; the name is then relative to the session base directory).
const (
	FileFlagDirectory = 0x02 | 0x04
	FileFlagLocalFile = 0x10 // source was a java.io.File instead of a content Uri
	FileFlagConverted = 0x20 // sender converted/re-encoded the payload
)

// MaxNameLen guards against corrupted or hostile length fields.
const MaxNameLen = 1 << 20

// Errors.
var (
	ErrBadMagic = errors.New("oshare: bad frame magic")
	ErrBadType  = errors.New("oshare: unknown frame type")
	ErrTooLarge = errors.New("oshare: length field too large")
)

// Command is a decoded command frame.
type Command struct {
	Cmd     int32
	Payload string
	Flag    int32
}

func (c Command) String() string {
	return fmt.Sprintf("cmd(%d, %q, flag=%d)", c.Cmd, c.Payload, c.Flag)
}

// FileHeader is the metadata that precedes the raw bytes of a file frame.
type FileHeader struct {
	Name     string
	FileType int32
	Size     int64
	Flag     int32
}

func (h FileHeader) String() string {
	return fmt.Sprintf("file(%q, type=%d, size=%d, flag=%d)", h.Name, h.FileType, h.Size, h.Flag)
}

// IsDir reports whether the entry is a directory marker.
func (h FileHeader) IsDir() bool {
	return h.Flag&FileFlagDirectory == FileFlagDirectory
}

// Frame is one decoded frame. Exactly one of Command, File and Heartbeat is
// meaningful, selected by Type.
type Frame struct {
	Type      int32
	Command   *Command
	File      *FileHeader
	Heartbeat int32
}

func (f *Frame) String() string {
	switch f.Type {
	case TypeCommand:
		if f.Command != nil {
			return f.Command.String()
		}
	case TypeFile:
		if f.File != nil {
			return f.File.String()
		}
	case TypeHeartbeat:
		return fmt.Sprintf("heartbeat(%d)", f.Heartbeat)
	}
	return fmt.Sprintf("frame(type=%d)", f.Type)
}

// Writer serialises frames. It is safe for concurrent use.
type Writer struct {
	w  io.Writer
	mu sync.Mutex
}

// NewWriter wraps w.
func NewWriter(w io.Writer) *Writer { return &Writer{w: w} }

// WriteCommand emits a command frame.
func (w *Writer) WriteCommand(cmd int32, payload string, flag int32) error {
	name := []byte(payload)
	if len(name) > MaxNameLen {
		return ErrTooLarge
	}
	buf := make([]byte, 0, 28+len(name))
	buf = appendInt32(buf, Magic)
	buf = appendInt32(buf, TypeCommand)
	buf = appendInt64(buf, int64(12+len(name)))
	buf = appendInt32(buf, cmd)
	buf = appendInt32(buf, int32(len(name)))
	buf = append(buf, name...)
	buf = appendInt32(buf, flag)
	return w.write(buf)
}

// WriteHeartbeat emits a keepalive frame (1 = request, 2 = response).
func (w *Writer) WriteHeartbeat(value int32) error {
	buf := make([]byte, 0, 20)
	buf = appendInt32(buf, Magic)
	buf = appendInt32(buf, TypeHeartbeat)
	buf = appendInt64(buf, 4)
	buf = appendInt32(buf, value)
	return w.write(buf)
}

// WriteFileHeader emits the file header; the caller must follow it with
// exactly h.Size raw bytes (use WriteFileBody).
func (w *Writer) WriteFileHeader(h FileHeader) error {
	name := []byte(h.Name)
	if len(name) > MaxNameLen {
		return ErrTooLarge
	}
	bodyLen := int64(28 + len(name)) // includes this 8 byte field
	buf := make([]byte, 0, 44+len(name))
	buf = appendInt32(buf, Magic)
	buf = appendInt32(buf, TypeFile)
	buf = appendInt64(buf, 16+bodyLen) // whole frame
	buf = appendInt64(buf, bodyLen)
	buf = appendInt32(buf, int32(len(name)))
	buf = append(buf, name...)
	buf = appendInt32(buf, h.FileType)
	buf = appendInt64(buf, h.Size)
	buf = appendInt32(buf, h.Flag)
	return w.write(buf)
}

// WriteFileBody copies exactly size bytes from r.
func (w *Writer) WriteFileBody(r io.Reader, size int64, progress func(written int64)) error {
	const chunk = 256 * 1024
	buf := make([]byte, chunk)
	var written int64
	for written < size {
		n := int64(chunk)
		if left := size - written; left < n {
			n = left
		}
		read, err := io.ReadFull(r, buf[:n])
		if read > 0 {
			if werr := w.write(buf[:read]); werr != nil {
				return werr
			}
			written += int64(read)
			if progress != nil {
				progress(written)
			}
		}
		if err != nil {
			if errors.Is(err, io.ErrUnexpectedEOF) || errors.Is(err, io.EOF) {
				return fmt.Errorf("oshare: short file body (%d/%d bytes)", written, size)
			}
			return err
		}
	}
	return nil
}

// SendFile emits a complete file frame.
func (w *Writer) SendFile(h FileHeader, r io.Reader, progress func(int64)) error {
	if err := w.WriteFileHeader(h); err != nil {
		return err
	}
	return w.WriteFileBody(r, h.Size, progress)
}

func (w *Writer) write(b []byte) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	_, err := w.w.Write(b)
	return err
}

// Reader decodes frames from a stream.
type Reader struct {
	r *bufio.Reader
}

// NewReader wraps r.
func NewReader(r io.Reader) *Reader {
	if br, ok := r.(*bufio.Reader); ok {
		return &Reader{r: br}
	}
	return &Reader{r: bufio.NewReaderSize(r, 64*1024)}
}

// Next reads the next non-file-body frame. When a file frame is returned the
// caller must consume the payload with CopyFileBody before calling Next again.
// Keepalive frames are returned as TypeHeartbeat so the caller can answer them.
func (r *Reader) Next() (*Frame, error) {
	var head [16]byte
	if _, err := io.ReadFull(r.r, head[:]); err != nil {
		return nil, err
	}
	magic := int32(binary.BigEndian.Uint32(head[0:4]))
	if magic != Magic {
		return nil, fmt.Errorf("%w: 0x%08x", ErrBadMagic, uint32(magic))
	}
	typ := int32(binary.BigEndian.Uint32(head[4:8]))
	length := int64(binary.BigEndian.Uint64(head[8:16]))

	switch typ {
	case TypeCommand:
		if length < 12 || length > MaxNameLen+12 {
			return nil, fmt.Errorf("%w: command length %d", ErrTooLarge, length)
		}
		body := make([]byte, length)
		if _, err := io.ReadFull(r.r, body); err != nil {
			return nil, err
		}
		cmd := int32(binary.BigEndian.Uint32(body[0:4]))
		nameLen := int32(binary.BigEndian.Uint32(body[4:8]))
		if nameLen < 0 || int64(nameLen) > length-12 {
			return nil, fmt.Errorf("%w: command name length %d", ErrTooLarge, nameLen)
		}
		payload := string(body[8 : 8+nameLen])
		flag := int32(binary.BigEndian.Uint32(body[8+nameLen : 12+nameLen]))
		return &Frame{Type: typ, Command: &Command{Cmd: cmd, Payload: payload, Flag: flag}}, nil

	case TypeHeartbeat:
		if length < 4 || length > 4096 {
			return nil, fmt.Errorf("%w: heartbeat length %d", ErrTooLarge, length)
		}
		body := make([]byte, length)
		if _, err := io.ReadFull(r.r, body); err != nil {
			return nil, err
		}
		return &Frame{Type: typ, Heartbeat: int32(binary.BigEndian.Uint32(body[0:4]))}, nil

	case TypeFile:
		var lenBuf [8]byte
		if _, err := io.ReadFull(r.r, lenBuf[:]); err != nil {
			return nil, err
		}
		bodyLen := int64(binary.BigEndian.Uint64(lenBuf[:]))
		if bodyLen < 28 || bodyLen > MaxNameLen+28 {
			return nil, fmt.Errorf("%w: file body length %d", ErrTooLarge, bodyLen)
		}
		body := make([]byte, bodyLen-8)
		if _, err := io.ReadFull(r.r, body); err != nil {
			return nil, err
		}
		nameLen := int32(binary.BigEndian.Uint32(body[0:4]))
		if nameLen < 0 || int64(nameLen) > bodyLen-28 {
			return nil, fmt.Errorf("%w: file name length %d", ErrTooLarge, nameLen)
		}
		h := &FileHeader{
			Name:     string(body[4 : 4+nameLen]),
			FileType: int32(binary.BigEndian.Uint32(body[4+nameLen : 8+nameLen])),
			Size:     int64(binary.BigEndian.Uint64(body[8+nameLen : 16+nameLen])),
			Flag:     int32(binary.BigEndian.Uint32(body[16+nameLen : 20+nameLen])),
		}
		return &Frame{Type: typ, File: h}, nil
	}
	return nil, fmt.Errorf("%w: %d", ErrBadType, typ)
}

// CopyFileBody streams the raw payload of a file frame into dst.
func (r *Reader) CopyFileBody(h *FileHeader, dst io.Writer, progress func(int64)) (int64, error) {
	if h.Size <= 0 {
		return 0, nil
	}
	buf := make([]byte, 256*1024)
	var written int64
	for written < h.Size {
		n := int64(len(buf))
		if left := h.Size - written; left < n {
			n = left
		}
		read, err := io.ReadFull(r.r, buf[:n])
		if read > 0 {
			if _, werr := dst.Write(buf[:read]); werr != nil {
				return written, werr
			}
			written += int64(read)
			if progress != nil {
				progress(written)
			}
		}
		if err != nil {
			return written, err
		}
	}
	return written, nil
}

// DiscardFileBody consumes and drops the file payload.
func (r *Reader) DiscardFileBody(h *FileHeader) error {
	_, err := r.CopyFileBody(h, io.Discard, nil)
	return err
}

func appendInt32(b []byte, v int32) []byte {
	var tmp [4]byte
	binary.BigEndian.PutUint32(tmp[:], uint32(v))
	return append(b, tmp[:]...)
}

func appendInt64(b []byte, v int64) []byte {
	var tmp [8]byte
	binary.BigEndian.PutUint64(tmp[:], uint64(v))
	return append(b, tmp[:]...)
}
