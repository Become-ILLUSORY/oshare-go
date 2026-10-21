// Command oshare is a small OShare (ColorOS 互传) compatible sender, receiver
// and discovery tool for Linux desktops, NAS boxes and other non-OPPO hosts.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/Become-ILLUSORY/oshare-go/discovery"
	"github.com/Become-ILLUSORY/oshare-go/protocol"
	"github.com/Become-ILLUSORY/oshare-go/transfer"
)

const version = "0.1.0"

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "recv":
		err = runRecv(os.Args[2:])
	case "send":
		err = runSend(os.Args[2:])
	case "discover":
		err = runDiscover(os.Args[2:])
	case "version", "-v", "--version":
		fmt.Println("oshare", version)
	case "help", "-h", "--help":
		usage()
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "oshare: %v\n", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Printf(`oshare %s - ColorOS/OPPO 互传 compatible file transfer

usage:
  oshare recv     [options]                 receive files
  oshare send     -to ADDR [options] FILES  send files to an address
  oshare send     -peer NAME [options] FILES  send to a discovered peer
  oshare discover [options]                 list OShare devices on the LAN
  oshare version

receive options:
  -dir DIR         destination directory (default ".")
  -port PORT       control port to listen on (default 8959)
  -name NAME       device name shown to the peer (default hostname)
  -yes             accept every transfer without asking
  -advertise       announce on mDNS (_oshare._tcp) (default true)

send options:
  -to ADDR         host:port of the receiver
  -peer TEXT       pick a discovered peer whose name/instance contains TEXT
  -dir HINT        save-directory hint shown on the receiver
  -type TYPE       force the announced data type (file/*, image/*, video/*, ...)
  -text TEXT       send a text snippet instead of files
  -timeout DUR     discovery / dial timeout (default 30s)
`, version)
}

func logf(format string, args ...any) {
	fmt.Printf("%s  %s\n", time.Now().Format("15:04:05"), fmt.Sprintf(format, args...))
}

func runRecv(args []string) error {
	fs := flag.NewFlagSet("recv", flag.ExitOnError)
	dir := fs.String("dir", ".", "destination directory")
	port := fs.Int("port", 8959, "control port")
	name := fs.String("name", "", "device name")
	id := fs.String("id", "", "device id")
	assumeYes := fs.Bool("yes", false, "accept every transfer")
	advertise := fs.Bool("advertise", true, "announce on mDNS")
	mac := fs.String("mac", "", "MAC address announced in the TXT record")
	if err := fs.Parse(args); err != nil {
		return err
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if *advertise {
		go func() {
			conn, err := discovery.Open()
			if err != nil {
				logf("mDNS unavailable: %v", err)
				return
			}
			defer conn.Close()
			adv := discovery.NewAdvertiser(*port, *mac)
			adv.TXT["SERVICE_NAME"] = "oshare"
			logf("advertising as %s on port %d", adv.Instance, *port)
			if err := adv.Serve(ctx, conn); err != nil && ctx.Err() == nil {
				logf("mDNS stopped: %v", err)
			}
		}()
	}

	addr := fmt.Sprintf(":%d", *port)
	ln, err := netListen(addr)
	if err != nil {
		return err
	}
	defer ln.Close()
	logf("listening on %s, saving to %s", addr, *dir)

	for {
		conn, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		go func() {
			defer conn.Close()
			dev := transfer.Device{Name: deviceName(*name), ID: *id}
			sess := transfer.NewSession(conn, transfer.Options{Device: dev, OnEvent: printEvent})
			defer sess.Close()
			logf("connection from %s", conn.RemoteAddr())

			res, err := transfer.Receive(ctx, sess, transfer.ReceiveOptions{
				Dir:    *dir,
				Decide: decideFunc(*assumeYes),
				Progress: func(name string, recv, total int64) {
					if total > 0 {
						fmt.Printf("\r  %-40s %3d%%  %s / %s\033[K", truncate(name, 40),
							recv*100/total, human(recv), human(total))
					}
				},
			})
			fmt.Print("\r\033[K")
			if err != nil {
				logf("transfer failed: %v", err)
				return
			}
			logf("done: %d file(s), %s in %s -> %s", res.Files, human(res.Bytes),
				res.Duration.Round(time.Millisecond), res.Dir)
			if res.Text != "" {
				logf("shared text: %q", res.Text)
			}
		}()
	}
}

func runSend(args []string) error {
	fs := flag.NewFlagSet("send", flag.ExitOnError)
	to := fs.String("to", "", "host:port of the receiver")
	peer := fs.String("peer", "", "substring of a discovered peer")
	dirHint := fs.String("dir", "", "save directory hint")
	dataType := fs.String("type", "", "announced data type")
	text := fs.String("text", "", "send this text instead of files")
	name := fs.String("name", "", "device name")
	id := fs.String("id", "", "device id")
	timeout := fs.Duration("timeout", 30*time.Second, "discovery / dial timeout")
	if err := fs.Parse(args); err != nil {
		return err
	}
	files := fs.Args()
	if *text == "" && len(files) == 0 {
		return fmt.Errorf("nothing to send: give files or -text")
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	target := *to
	if target == "" {
		if *peer == "" {
			return fmt.Errorf("either -to or -peer is required")
		}
		found, err := findPeer(ctx, *peer, *timeout)
		if err != nil {
			return err
		}
		target = found
		logf("resolved %q to %s", *peer, target)
	}

	dctx, cancel := context.WithTimeout(ctx, *timeout)
	defer cancel()
	conn, err := netDial(dctx, target)
	if err != nil {
		return err
	}
	defer conn.Close()

	dev := transfer.Device{Name: deviceName(*name), ID: *id}
	sess := transfer.NewSession(conn, transfer.Options{Device: dev, OnEvent: printEvent})
	defer sess.Close()

	items := make([]transfer.FileItem, 0, len(files))
	for _, f := range files {
		abs, err := filepath.Abs(f)
		if err != nil {
			return err
		}
		items = append(items, transfer.FileItem{Path: abs})
	}

	var last time.Time
	res, err := transfer.Push(ctx, sess, transfer.PushRequest{
		Items:    items,
		DataType: *dataType,
		DirHint:  *dirHint,
		Text:     *text,
		Progress: func(sent, total int64) {
			if total == 0 || time.Since(last) < 100*time.Millisecond {
				return
			}
			last = time.Now()
			fmt.Printf("\r  sending %3d%%  %s / %s\033[K", sent*100/total, human(sent), human(total))
		},
	})
	fmt.Print("\r\033[K")
	if err != nil {
		return err
	}
	logf("sent %d file(s), %s in %s (confirmed=%v)", res.Files, human(res.Bytes),
		res.Duration.Round(time.Millisecond), res.Confirmed)
	return nil
}

func runDiscover(args []string) error {
	fs := flag.NewFlagSet("discover", flag.ExitOnError)
	timeout := fs.Duration("timeout", 5*time.Second, "how long to listen")
	if err := fs.Parse(args); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()

	conn, err := discovery.Open()
	if err != nil {
		return err
	}
	defer conn.Close()

	peers, err := discovery.Browse(ctx, conn)
	if err != nil {
		return err
	}
	seen := map[string]bool{}
	for p := range peers {
		if seen[p.Address()] {
			continue
		}
		seen[p.Address()] = true
		fmt.Printf("%-24s %-20s mac=%s\n", discovery.InstanceLabel(p.Instance), p.Address(), p.MAC)
	}
	if len(seen) == 0 {
		logf("no OShare device answered within %s", *timeout)
	}
	return nil
}

func decideFunc(assumeYes bool) func(protocol.Announce) (transfer.Decision, string) {
	return func(ann protocol.Announce) (transfer.Decision, string) {
		logf("incoming: %d entr(y/ies), %s, type=%s, from %q, dir hint %q",
			ann.Count, human(ann.TotalSize), ann.DataType, ann.DeviceName, ann.DirHint)
		if assumeYes || !isTerminal() {
			return transfer.Allow, ""
		}
		fmt.Print("accept? [Y/n] ")
		var answer string
		_, _ = fmt.Scanln(&answer)
		switch strings.ToLower(strings.TrimSpace(answer)) {
		case "n", "no":
			return transfer.Reject, ""
		}
		return transfer.Allow, ""
	}
}

func printEvent(e transfer.Event) {
	switch e.Kind {
	case "text":
		logf("peer text: %q", e.Payload)
	case "log", "thumbnail", "client-info", "server-info":
		// The Android app sends diagnostics here; nothing to do.
	case "rejected-name":
		logf("skipped suspicious entry %q", e.Payload)
	}
}

func findPeer(ctx context.Context, match string, timeout time.Duration) (string, error) {
	conn, err := discovery.Open()
	if err != nil {
		return "", err
	}
	defer conn.Close()
	dctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	peers, err := discovery.Browse(dctx, conn)
	if err != nil {
		return "", err
	}
	for p := range peers {
		if strings.Contains(strings.ToLower(p.Instance), strings.ToLower(match)) ||
			strings.Contains(strings.ToLower(p.Name), strings.ToLower(match)) {
			return p.Address(), nil
		}
	}
	return "", fmt.Errorf("no peer matching %q found in %s", match, timeout)
}

func human(n int64) string {
	const unit = 1024
	if n < unit {
		return fmt.Sprintf("%d B", n)
	}
	div, exp := int64(unit), 0
	for m := n / unit; m >= unit; m /= unit {
		div *= unit
		exp++
	}
	return fmt.Sprintf("%.1f %ciB", float64(n)/float64(div), "KMGTPE"[exp])
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return "…" + s[len(s)-n+1:]
}
