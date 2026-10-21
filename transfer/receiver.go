package transfer

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"strings"
	"time"

	"github.com/Become-ILLUSORY/oshare-go/protocol"
)

// Decision is what the receiver answers to an announcement.
type Decision int

const (
	// Allow accepts the transfer.
	Allow Decision = iota
	// Reject refuses it (CmdAckReject with "reject").
	Reject
	// NoSpace reports insufficient storage (CmdNoSpace).
	NoSpace
)

// ReceivedFile describes one written entry.
type ReceivedFile struct {
	Name string // protocol name, directories included
	Path string // absolute path on disk
	Size int64
}

// ReceiveOptions configures the receiver role.
type ReceiveOptions struct {
	Dir      string                                         // destination root, required
	Decide   func(ann protocol.Announce) (Decision, string) // nil: allow everything
	Progress func(name string, recv, total int64)
	// Overwrite replaces existing files instead of picking a free name.
	Overwrite bool
}

// ReceiveResult reports what was received.
type ReceiveResult struct {
	Files    int
	Bytes    int64
	Entries  []ReceivedFile
	Text     string
	DataType string
	Dir      string
	Duration time.Duration
	Rejected bool
}

// Receive performs the receiver role: announce capabilities, answer the
// sender's announcement, write files and confirm the end of the transfer.
func Receive(ctx context.Context, s *Session, opt ReceiveOptions) (*ReceiveResult, error) {
	if opt.Dir == "" {
		return nil, errors.New("oshare: ReceiveOptions.Dir is required")
	}
	start := time.Now()
	dir, err := filepath.Abs(opt.Dir)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	res := &ReceiveResult{Dir: dir}

	// Open the session the way the Android receiver does (r9/b.java:244).
	if err := s.SendCommand(protocol.CmdClientInfo, protocol.ClientInfo{
		DeviceID: s.Device().ID,
		Unknown:  "",
		Version:  s.Device().Version,
	}.Encode()); err != nil {
		return nil, err
	}

	var announced bool
	for {
		select {
		case <-ctx.Done():
			res.Duration = time.Since(start)
			return res, ctx.Err()
		default:
		}
		f, err := s.Next()
		if err != nil {
			res.Duration = time.Since(start)
			return res, err
		}
		switch f.Type {
		case protocol.TypeFile:
			if !announced {
				_ = s.DiscardFileBody(f.File)
				continue
			}
			if err := s.receiveOne(f.File, dir, opt, res); err != nil {
				return res, err
			}
			continue

		case protocol.TypeCommand:
			// handled below
		default:
			continue
		}

		cmd := f.Command
		switch cmd.Cmd {
		case protocol.CmdAnnounce:
			ann := protocol.ParseAnnounce(cmd.Payload)
			res.DataType = ann.DataType
			s.emit(Event{Kind: "announce", Payload: cmd.Payload})
			decision, dirOverride := Allow, ""
			if opt.Decide != nil {
				decision, dirOverride = opt.Decide(ann)
			}
			if dirOverride != "" {
				abs, err := filepath.Abs(dirOverride)
				if err == nil {
					if err := os.MkdirAll(abs, 0o755); err == nil {
						dir = abs
						res.Dir = abs
					}
				}
			}
			switch decision {
			case Reject:
				_ = s.SendCommand(protocol.CmdAckReject, "reject")
				res.Rejected = true
				res.Duration = time.Since(start)
				return res, ErrRejected
			case NoSpace:
				_ = s.SendCommand(protocol.CmdNoSpace, protocol.DummyPayload)
				res.Duration = time.Since(start)
				return res, ErrNoSpace
			default:
				answer := ann.DataType
				if answer == "" {
					answer = protocol.DataTypeFile
				}
				if err := s.SendCommand(protocol.CmdAckAllow, answer); err != nil {
					return res, err
				}
				announced = true
			}

		case protocol.CmdClientInfo:
			info := protocol.ParseClientInfo(cmd.Payload)
			s.emit(Event{Kind: "client-info", Payload: info.DeviceID})
			_ = s.SendCommand(protocol.CmdServerInfo, protocol.JoinFields(s.Device().ID))

		case protocol.CmdTransferOver:
			over := protocol.ParseOver(cmd.Payload)
			if over.DataType != "" {
				res.DataType = over.DataType
			}
			// Confirm with the receiver-side variant: payload is the data type.
			_ = s.SendCommand(protocol.CmdTransferOver, res.DataType)
			res.Duration = time.Since(start)
			return res, nil

		case protocol.CmdCancel:
			_ = s.SendCommand(protocol.CmdCancelOK, protocol.DummyPayload)
			res.Duration = time.Since(start)
			return res, ErrCancelled

		case protocol.CmdShortText:
			res.Text = protocol.SplitFields(cmd.Payload)[0]
			if res.Text == "" {
				res.Text = cmd.Payload
			}
			s.emit(Event{Kind: "text", Payload: res.Text})

		case protocol.CmdThumbnail:
			s.emit(Event{Kind: "thumbnail", Payload: cmd.Payload})

		case protocol.CmdProgress:
			s.emit(Event{Kind: "progress", Value: protocol.ParsePercent(cmd.Payload)})

		case protocol.CmdLogInstruction:
			s.emit(Event{Kind: "log", Payload: cmd.Payload})

		case protocol.CmdNothing:
			// keepalive-ish no-op

		case protocol.CmdAckAllow, protocol.CmdAckReject, protocol.CmdAckTimeout,
			protocol.CmdNoSpace, protocol.CmdCancelOK, protocol.CmdServerInfo:
			// acks meant for the sender role, ignore
		}
	}
}

func (s *Session) receiveOne(h *protocol.FileHeader, dir string, opt ReceiveOptions, res *ReceiveResult) error {
	target, err := targetPath(dir, h.Name)
	if err != nil {
		// Path traversal attempt or unusable name: swallow the payload but do
		// not touch the filesystem.
		if derr := s.DiscardFileBody(h); derr != nil {
			return derr
		}
		s.emit(Event{Kind: "rejected-name", Payload: h.Name})
		return nil
	}
	if h.IsDir() {
		_ = s.DiscardFileBody(h)
		announced := path.Dir(target)
		if announced != "." {
			_ = os.MkdirAll(target, 0o755)
		}
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		_ = s.DiscardFileBody(h)
		return err
	}
	if !opt.Overwrite {
		target = freeName(target)
	}
	f, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		_ = s.DiscardFileBody(h)
		return err
	}
	defer f.Close()

	// A large file may legitimately take a while; widen the socket deadline
	// while its payload streams in.
	_ = s.conn.SetReadDeadline(time.Now().Add(10 * time.Minute))
	base := res.Bytes
	n, err := s.ConsumeFileBody(h, f, func(written int64) {
		if opt.Progress != nil {
			opt.Progress(h.Name, base+written, h.Size)
		}
	})
	if err != nil {
		return err
	}
	res.Files++
	res.Bytes += n
	res.Entries = append(res.Entries, ReceivedFile{Name: h.Name, Path: target, Size: n})
	return nil
}

// targetPath maps a protocol name onto a path inside root, refusing anything
// that would escape it.
func targetPath(root, name string) (string, error) {
	cleaned := strings.ReplaceAll(name, "\\", "/")
	cleaned = strings.TrimPrefix(cleaned, "/")
	if len(cleaned) > 1 && cleaned[1] == ':' { // windows drive letter
		cleaned = cleaned[2:]
	}
	cleaned = strings.TrimLeft(cleaned, "/")
	if cleaned == "" {
		return "", fmt.Errorf("oshare: empty file name")
	}
	for _, part := range strings.Split(cleaned, "/") {
		if part == ".." {
			return "", fmt.Errorf("oshare: refusing path %q", name)
		}
	}
	target := filepath.Join(root, filepath.FromSlash(cleaned))
	rel, err := filepath.Rel(root, target)
	if err != nil || strings.HasPrefix(rel, "..") {
		return "", fmt.Errorf("oshare: refusing path %q", name)
	}
	return target, nil
}

// freeName appends "-1", "-2", ... until the path does not exist.
func freeName(p string) string {
	if _, err := os.Stat(p); errors.Is(err, os.ErrNotExist) {
		return p
	}
	ext := filepath.Ext(p)
	base := strings.TrimSuffix(p, ext)
	for i := 1; i < 10000; i++ {
		candidate := fmt.Sprintf("%s-%d%s", base, i, ext)
		if _, err := os.Stat(candidate); errors.Is(err, os.ErrNotExist) {
			return candidate
		}
	}
	return p
}
