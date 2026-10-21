// Package transfer implements the OShare LAN transfer handshake on top of the
// recovered wire format (see package protocol).
//
// Roles, as observed in com.coloros.oshare 17.2.36:
//
//	sender   (r9/g.java, "P2pServer"): owns the files, sends CmdAnnounce,
//	        streams file frames, then CmdTransferOver.
//	receiver (r9/b.java, "P2pClient"): connects, sends CmdClientInfo, decides
//	        with CmdAckAllow / CmdAckReject / CmdNoSpace, writes the files and
//	        answers CmdTransferOver.
//
// Either side can dial or listen; the protocol only depends on the roles, so
// Push works on a dialled connection and Receive on an accepted one, or the
// other way round (that is what the Android app does too).
package transfer

import (
	"errors"
	"io"
	"net"
	"sync"
	"time"

	"github.com/Become-ILLUSORY/oshare-go/protocol"
)

// Defaults taken from the Android implementation.
const (
	DefaultHeartbeatInterval = 10 * time.Second // mb/n.java: KeepAliveFilter interval
	DefaultHeartbeatTimeout  = 30 * time.Second // mb/n.java: requestTimeout
	DefaultReadTimeout       = 30 * time.Second
	DefaultHandshakeTimeout  = 3 * time.Second
	DefaultOverTimeout       = 30 * time.Second
	defaultProtocolVersion   = "17"
)

// Device describes the local endpoint.
type Device struct {
	Name    string // shown on the peer
	ID      string // 64 bit device identifier, hex/ascii
	Version string // protocol version string, "17" by default
}

// Event is a notification about an atypical frame.
type Event struct {
	Kind    string // "client-info", "server-info", "thumbnail", "text", "progress", "log", "cancel", "noop"
	Payload string
	Value   int
}

// Options tunes a session.
type Options struct {
	Device            Device
	HeartbeatInterval time.Duration // 0 disables keepalives
	ReadTimeout       time.Duration
	OnEvent           func(Event)
}

func (o *Options) withDefaults() {
	if o.Device.Version == "" {
		o.Device.Version = defaultProtocolVersion
	}
	if o.Device.Name == "" {
		o.Device.Name = "oshare-go"
	}
	if o.Device.ID == "" {
		o.Device.ID = "0000000000000000"
	}
	if o.HeartbeatInterval == 0 {
		o.HeartbeatInterval = DefaultHeartbeatInterval
	}
	if o.ReadTimeout == 0 {
		o.ReadTimeout = DefaultReadTimeout
	}
}

// Session is a framed OShare connection.
type Session struct {
	conn net.Conn
	rd   *protocol.Reader
	wr   *protocol.Writer
	opt  Options

	stopOnce sync.Once
	stop     chan struct{}
	closeOne sync.Once
}

// NewSession wraps conn and starts the keepalive loop.
func NewSession(conn net.Conn, opt Options) *Session {
	opt.withDefaults()
	s := &Session{
		conn: conn,
		rd:   protocol.NewReader(conn),
		wr:   protocol.NewWriter(conn),
		opt:  opt,
		stop: make(chan struct{}),
	}
	if opt.HeartbeatInterval > 0 {
		go s.keepAlive()
	}
	return s
}

// Device returns the local device description.
func (s *Session) Device() Device { return s.opt.Device }

// RemoteAddr reports the peer address.
func (s *Session) RemoteAddr() net.Addr { return s.conn.RemoteAddr() }

func (s *Session) keepAlive() {
	t := time.NewTicker(s.opt.HeartbeatInterval)
	defer t.Stop()
	for {
		select {
		case <-s.stop:
			return
		case <-t.C:
			if err := s.wr.WriteHeartbeat(1); err != nil {
				return
			}
		}
	}
}

// Next returns the next application frame, answering keepalives transparently.
func (s *Session) Next() (*protocol.Frame, error) {
	for {
		if s.opt.ReadTimeout > 0 {
			_ = s.conn.SetReadDeadline(time.Now().Add(s.opt.ReadTimeout))
		}
		f, err := s.rd.Next()
		if err != nil {
			return nil, err
		}
		if f.Type == protocol.TypeHeartbeat {
			if f.Heartbeat == 1 {
				if err := s.wr.WriteHeartbeat(2); err != nil {
					return nil, err
				}
			}
			continue
		}
		if f.Type == protocol.TypeCommand && f.Command.Cmd == protocol.CmdKeepAlive {
			continue
		}
		return f, nil
	}
}

// SendCommand writes a command frame with flag 1 (the value the Android app
// uses for every command it builds through pb/c.java).
func (s *Session) SendCommand(cmd int32, payload string) error {
	return s.wr.WriteCommand(cmd, payload, 1)
}

// SendHeartbeat writes a keepalive frame.
func (s *Session) SendHeartbeat(v int32) error { return s.wr.WriteHeartbeat(v) }

// SendFile streams one file frame.
func (s *Session) SendFile(h protocol.FileHeader, r io.Reader, progress func(int64)) error {
	return s.wr.SendFile(h, r, progress)
}

// DiscardFileBody drops the payload of the file frame most recently returned.
func (s *Session) DiscardFileBody(h *protocol.FileHeader) error { return s.rd.DiscardFileBody(h) }

// ConsumeFileBody streams the payload of the file frame into dst.
func (s *Session) ConsumeFileBody(h *protocol.FileHeader, dst io.Writer, progress func(int64)) (int64, error) {
	return s.rd.CopyFileBody(h, dst, progress)
}

// Close shuts the session down.
func (s *Session) Close() error {
	s.stopOnce.Do(func() { close(s.stop) })
	var err error
	s.closeOne.Do(func() { err = s.conn.Close() })
	return err
}

// emit reports an event without letting a slow callback break the protocol.
func (s *Session) emit(e Event) {
	if s.opt.OnEvent != nil {
		s.opt.OnEvent(e)
	}
}

// DefaultWait bounds how long Push/Receive wait for the peer's next frame when
// the caller's context has no deadline.
func DefaultWait(d time.Duration) time.Time { return time.Now().Add(d) }

var (
	// ErrRejected is returned when the peer refuses the transfer.
	ErrRejected = errors.New("oshare: transfer rejected by peer")
	// ErrNoSpace is returned when the peer reports insufficient storage.
	ErrNoSpace = errors.New("oshare: peer has not enough space")
	// ErrCancelled is returned when either side cancels.
	ErrCancelled = errors.New("oshare: transfer cancelled")
	// ErrTimeout is returned when the peer reports a user timeout.
	ErrTimeout = errors.New("oshare: peer timed out waiting for the user")
	// ErrProtocol is returned for unexpected frames.
	ErrProtocol = errors.New("oshare: unexpected frame")
)
