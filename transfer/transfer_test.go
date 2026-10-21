package transfer

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/Become-ILLUSORY/oshare-go/protocol"
)

// runPair starts a listener, accepts one connection and runs the receiver on
// it while the caller drives the sender on the dialled connection.
func runPair(t *testing.T, recvOpt ReceiveOptions, recvOpts Options) (addr string, resultCh chan *ReceiveResult, errCh chan error) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() { _ = ln.Close() })

	resultCh = make(chan *ReceiveResult, 1)
	errCh = make(chan error, 1)
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			errCh <- err
			return
		}
		defer conn.Close()
		sess := NewSession(conn, recvOpts)
		defer sess.Close()
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		res, err := Receive(ctx, sess, recvOpt)
		resultCh <- res
		errCh <- err
	}()
	return ln.Addr().String(), resultCh, errCh
}

func dialSession(t *testing.T, addr string, opt Options) *Session {
	t.Helper()
	conn, err := net.DialTimeout("tcp", addr, 5*time.Second)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return NewSession(conn, opt)
}

func TestPushReceiveFiles(t *testing.T) {
	src := t.TempDir()
	dst := t.TempDir()

	files := map[string][]byte{
		"hello.txt":     []byte("hello oshare\n"),
		"blob.bin":      bytes.Repeat([]byte{0xde, 0xad, 0xbe, 0xef}, 512*1024), // 2 MiB
		"empty.dat":     {},
		"sub/nested.md": []byte("# nested\n"),
	}
	var items []FileItem
	for name, body := range files {
		p := filepath.Join(src, name)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, body, 0o644); err != nil {
			t.Fatal(err)
		}
		items = append(items, FileItem{Path: p, Name: name})
	}

	addr, resCh, errCh := runPair(t,
		ReceiveOptions{Dir: dst},
		Options{Device: Device{Name: "receiver", ID: "rcv-1"}})

	sess := dialSession(t, addr, Options{Device: Device{Name: "sender", ID: "snd-1"}})
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pr, err := Push(ctx, sess, PushRequest{Items: items})
	if err != nil {
		t.Fatalf("Push: %v", err)
	}
	if pr.Files != len(files) || !pr.Confirmed {
		t.Fatalf("push result = %+v", pr)
	}

	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("Receive: %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("receiver did not finish")
	}
	res := <-resCh
	if res.Files != len(files) {
		t.Fatalf("received %d files, want %d", res.Files, len(files))
	}

	for name, want := range files {
		got, err := os.ReadFile(filepath.Join(dst, name))
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		if sha256.Sum256(got) != sha256.Sum256(want) {
			t.Fatalf("%s content mismatch (%d vs %d bytes)", name, len(got), len(want))
		}
	}
}

func TestReceiveReject(t *testing.T) {
	src := t.TempDir()
	dst := t.TempDir()
	p := filepath.Join(src, "x.txt")
	if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}

	addr, _, _ := runPair(t,
		ReceiveOptions{Dir: dst, Decide: func(protocol.Announce) (Decision, string) { return Reject, "" }},
		Options{})

	sess := dialSession(t, addr, Options{})
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	_, err := Push(ctx, sess, PushRequest{Items: []FileItem{{Path: p}}})
	if !errors.Is(err, ErrRejected) {
		t.Fatalf("err = %v, want ErrRejected", err)
	}
}

func TestReceiveNoSpace(t *testing.T) {
	src := t.TempDir()
	dst := t.TempDir()
	p := filepath.Join(src, "big.bin")
	if err := os.WriteFile(p, bytes.Repeat([]byte("z"), 4096), 0o644); err != nil {
		t.Fatal(err)
	}

	addr, _, _ := runPair(t,
		ReceiveOptions{Dir: dst, Decide: func(protocol.Announce) (Decision, string) { return NoSpace, "" }},
		Options{})

	sess := dialSession(t, addr, Options{})
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	_, err := Push(ctx, sess, PushRequest{Items: []FileItem{{Path: p}}})
	if !errors.Is(err, ErrNoSpace) {
		t.Fatalf("err = %v, want ErrNoSpace", err)
	}
}

func TestPushText(t *testing.T) {
	dst := t.TempDir()
	addr, resCh, errCh := runPair(t, ReceiveOptions{Dir: dst}, Options{})

	sess := dialSession(t, addr, Options{})
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if _, err := Push(ctx, sess, PushRequest{Text: "https://example.com"}); err != nil {
		t.Fatalf("Push text: %v", err)
	}
	if err := <-errCh; err != nil {
		t.Fatalf("Receive: %v", err)
	}
	res := <-resCh
	if res.Text != "https://example.com" {
		t.Fatalf("text = %q", res.Text)
	}
}

func TestKeepaliveIsAnswered(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()

	errc := make(chan error, 1)
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			errc <- err
			return
		}
		defer conn.Close()
		stop := make(chan struct{})
		defer close(stop)
		go func() {
			// Probe until the peer answers, as KeepAliveFilter does.
			for i := 0; i < 20; i++ {
				select {
				case <-stop:
					return
				case <-time.After(50 * time.Millisecond):
					_ = conn.SetWriteDeadline(time.Now().Add(time.Second))
					var buf bytes.Buffer
					if err := protocol.NewWriter(&buf).WriteHeartbeat(1); err != nil {
						return
					}
					if _, err := conn.Write(buf.Bytes()); err != nil {
						return
					}
				}
			}
		}()
		br := protocol.NewReader(conn)
		for {
			f, err := br.Next()
			if err != nil {
				errc <- err
				return
			}
			if f.Type == protocol.TypeHeartbeat && f.Heartbeat == 2 {
				errc <- nil
				return
			}
		}
	}()

	conn, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	sess := NewSession(conn, Options{HeartbeatInterval: 0})
	defer sess.Close()

	// Next() must swallow the request and answer with value 2.
	done := make(chan error, 1)
	go func() {
		_ = conn.SetReadDeadline(time.Now().Add(3 * time.Second))
		_, err := sess.Next()
		done <- err
	}()

	select {
	case err := <-errc:
		if err != nil {
			t.Fatalf("probe: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("keepalive was not answered")
	}
	<-done
}

func TestTargetPathRejectsTraversal(t *testing.T) {
	root := t.TempDir()
	cases := map[string]bool{
		"a.txt":            true,
		"sub/a.txt":        true,
		"/abs/a.txt":       true,
		"../../etc/passwd": false,
		"sub/../../etc/x":  false,
		"..":               false,
		"":                 false,
	}
	for name, ok := range cases {
		_, err := targetPath(root, name)
		if ok && err != nil {
			t.Errorf("%q: unexpected error %v", name, err)
		}
		if !ok && err == nil {
			t.Errorf("%q: expected an error", name)
		}
	}
}

func TestFreeName(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "a.txt")
	seen := map[string]bool{}
	for i := 0; i < 3; i++ {
		if seen[p] {
			t.Fatalf("freeName returned the same path twice: %s", p)
		}
		seen[p] = true
		if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
		p = freeName(p)
	}
	if _, err := os.Stat(p); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("freeName returned an existing path: %s", p)
	}
}
