package transfer

import (
	"context"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"strings"
	"time"

	"github.com/Become-ILLUSORY/oshare-go/protocol"
)

// FileItem is one entry to push. Either Path is set (the file is opened on
// demand) or Reader is set (Size is then mandatory).
type FileItem struct {
	Path   string
	Name   string // name announced to the peer, defaults to filepath.Base(Path)
	Size   int64  // 0 means "stat the path"
	Type   int32  // file type field, 0 is what the Android app sends for plain files
	Flag   int32  // protocol.FileFlag* bits
	Reader io.Reader
}

// PushRequest describes a transfer initiated by the sender.
type PushRequest struct {
	Items    []FileItem
	DataType string // "file/*", "image/*", ... derived from the items when empty
	DirHint  string // shown on the receiver, the Android app sends its own save dir
	Entrance int64  // UI entrance code, 0 is fine
	Text     string // when set and Items is empty, a shared text is pushed instead
	Progress func(sent, total int64)
}

// PushResult reports what happened.
type PushResult struct {
	Files     int
	Bytes     int64
	DataType  string
	Duration  time.Duration
	Confirmed bool // the receiver acknowledged the final CmdTransferOver
}

// Push performs the sender role: announce, wait for the go-ahead, stream the
// files and close with CmdTransferOver.
func Push(ctx context.Context, s *Session, req PushRequest) (*PushResult, error) {
	start := time.Now()
	res := &PushResult{}

	items := make([]FileItem, 0, len(req.Items))
	var total int64
	for _, it := range req.Items {
		if it.Reader == nil {
			st, err := os.Stat(it.Path)
			if err != nil {
				return nil, fmt.Errorf("oshare: %s: %w", it.Path, err)
			}
			if it.Size == 0 {
				it.Size = st.Size()
			}
			if it.Name == "" {
				it.Name = filepath.Base(it.Path)
			}
		} else if it.Size <= 0 {
			return nil, fmt.Errorf("oshare: item %q needs a size when Reader is used", it.Name)
		}
		items = append(items, it)
		total += it.Size
	}

	dataType := req.DataType
	if dataType == "" {
		dataType = detectDataType(items, req.Text)
	}
	res.DataType = dataType

	// Handshake: the receiver opens with CmdClientInfo; answer CmdServerInfo.
	if err := handshakeAsSender(ctx, s); err != nil {
		return nil, err
	}

	ann := protocol.Announce{
		Count:      int64(len(items)),
		DeviceName: s.Device().Name,
		TotalSize:  total,
		DirHint:    req.DirHint,
		DataType:   dataType,
		Version:    s.opt.Device.Version,
	}
	if err := s.SendCommand(protocol.CmdAnnounce, ann.Encode()); err != nil {
		return nil, err
	}
	if err := waitForAllowance(ctx, s, 3*time.Minute); err != nil {
		return nil, err
	}

	// Text-only transfer: CmdShortText followed by CmdTransferOver.
	if len(items) == 0 && req.Text != "" {
		if err := s.SendCommand(protocol.CmdShortText, protocol.JoinFields(req.Text)); err != nil {
			return nil, err
		}
		return finishPush(ctx, s, res, start, req, dataType, 0, 0)
	}

	// Stream the files.
	var sent int64
	for _, it := range items {
		h := protocol.FileHeader{Name: it.Name, FileType: it.Type, Size: it.Size, Flag: it.Flag}
		var rc io.ReadCloser
		var r io.Reader
		if it.Reader != nil {
			r = it.Reader
		} else {
			f, err := os.Open(it.Path)
			if err != nil {
				return nil, fmt.Errorf("oshare: open %s: %w", it.Path, err)
			}
			rc = f
			r = f
		}
		base := sent
		progress := func(n int64) {
			if req.Progress != nil {
				req.Progress(base+n, total)
			}
		}
		err := s.SendFile(h, r, progress)
		if rc != nil {
			_ = rc.Close()
		}
		if err != nil {
			return nil, fmt.Errorf("oshare: send %s: %w", it.Name, err)
		}
		sent += it.Size
		res.Files++
		res.Bytes += it.Size
	}
	return finishPush(ctx, s, res, start, req, dataType, sent, total)
}

func finishPush(ctx context.Context, s *Session, res *PushResult, start time.Time, req PushRequest, dataType string, sent, total int64) (*PushResult, error) {
	over := protocol.Over{Count: int64(res.Files), DataType: dataType, Dir: req.DirHint, Entrance: req.Entrance}
	if err := s.SendCommand(protocol.CmdTransferOver, over.Encode()); err != nil {
		return nil, err
	}
	// The receiver confirms with its own CmdTransferOver once everything is on
	// disk; a timeout there is not fatal, the bytes were already delivered.
	wait := 30 * time.Second
	if d, ok := ctx.Deadline(); ok && time.Until(d) < wait {
		wait = time.Until(d)
	}
	deadline := time.Now().Add(wait)
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			res.Duration = time.Since(start)
			return res, ctx.Err()
		default:
		}
		_ = s.conn.SetReadDeadline(deadline)
		f, err := s.Next()
		if err != nil {
			break
		}
		if f.Type == protocol.TypeCommand {
			switch f.Command.Cmd {
			case protocol.CmdTransferOver:
				res.Confirmed = true
			case protocol.CmdCancel:
				_ = s.SendCommand(protocol.CmdCancelOK, protocol.DummyPayload)
				res.Duration = time.Since(start)
				return res, ErrCancelled
			case protocol.CmdProgress:
				s.emit(Event{Kind: "progress", Value: protocol.ParsePercent(f.Command.Payload)})
			}
		}
		if res.Confirmed {
			break
		}
	}
	res.Duration = time.Since(start)
	return res, nil
}

// handshakeAsSender waits briefly for the receiver's CmdClientInfo and answers
// it. Peers that stay silent (older builds, this library's own receiver when
// configured so) are tolerated.
func handshakeAsSender(ctx context.Context, s *Session) error {
	deadline := time.Now().Add(DefaultHandshakeTimeout)
	for time.Now().Before(deadline) {
		if d, ok := ctx.Deadline(); ok && time.Now().After(d) {
			return ctx.Err()
		}
		_ = s.conn.SetReadDeadline(deadline)
		f, err := s.Next()
		if err != nil {
			return nil // nothing to handshake with, carry on
		}
		if f.Type != protocol.TypeCommand {
			// A file frame here would be a peer pushing to us; we cannot consume
			// it in the sender role, so drop the payload to stay in sync.
			if f.File != nil {
				_ = s.DiscardFileBody(f.File)
			}
			continue
		}
		switch f.Command.Cmd {
		case protocol.CmdClientInfo:
			info := protocol.ParseClientInfo(f.Command.Payload)
			s.emit(Event{Kind: "client-info", Payload: info.DeviceID})
			if err := s.SendCommand(protocol.CmdServerInfo, protocol.JoinFields(s.Device().ID)); err != nil {
				return err
			}
			return nil
		case protocol.CmdLogInstruction:
			s.emit(Event{Kind: "log", Payload: f.Command.Payload})
		}
	}
	return nil
}

// waitForAllowance blocks until the receiver allows, rejects or reports a
// problem.
func waitForAllowance(ctx context.Context, s *Session, wait time.Duration) error {
	if d, ok := ctx.Deadline(); ok && time.Until(d) < wait {
		wait = time.Until(d)
	}
	deadline := time.Now().Add(wait)
	for time.Now().Before(deadline) {
		_ = s.conn.SetReadDeadline(deadline)
		f, err := s.Next()
		if err != nil {
			return err
		}
		if f.Type != protocol.TypeCommand {
			if f.File != nil {
				_ = s.DiscardFileBody(f.File)
			}
			continue
		}
		switch f.Command.Cmd {
		case protocol.CmdAckAllow:
			return nil
		case protocol.CmdAckReject:
			return ErrRejected
		case protocol.CmdAckTimeout:
			return ErrTimeout
		case protocol.CmdNoSpace:
			return ErrNoSpace
		case protocol.CmdCancel:
			_ = s.SendCommand(protocol.CmdCancelOK, protocol.DummyPayload)
			return ErrCancelled
		case protocol.CmdClientInfo:
			_ = s.SendCommand(protocol.CmdServerInfo, protocol.JoinFields(s.Device().ID))
		case protocol.CmdThumbnail:
			s.emit(Event{Kind: "thumbnail", Payload: f.Command.Payload})
		case protocol.CmdProgress:
			s.emit(Event{Kind: "progress", Value: protocol.ParsePercent(f.Command.Payload)})
		case protocol.CmdLogInstruction:
			s.emit(Event{Kind: "log", Payload: f.Command.Payload})
		case protocol.CmdNothing:
			// keepalive-ish, ignore
		}
	}
	return context.DeadlineExceeded
}

func detectDataType(items []FileItem, text string) string {
	if text != "" && len(items) == 0 {
		return protocol.DataTypeText
	}
	if len(items) == 0 {
		return protocol.DataTypeFile
	}
	videos, images := 0, 0
	for _, it := range items {
		ext := strings.ToLower(path.Ext(it.Name))
		switch ext {
		case ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif", ".dng":
			images++
		case ".mp4", ".mov", ".mkv", ".avi", ".3gp", ".webm", ".ts":
			videos++
		case ".vcf":
			if len(items) == 1 {
				return protocol.DataTypeVCard
			}
		}
	}
	switch {
	case images == len(items):
		return protocol.DataTypeImage
	case videos == len(items):
		return protocol.DataTypeVideo
	}
	return protocol.DataTypeFile
}
