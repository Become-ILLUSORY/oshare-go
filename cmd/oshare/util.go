package main

import (
	"context"
	"net"
	"os"
	"time"
)

func netListen(addr string) (net.Listener, error) {
	lc := net.ListenConfig{}
	return lc.Listen(context.Background(), "tcp", addr)
}

func netDial(ctx context.Context, addr string) (net.Conn, error) {
	d := net.Dialer{Timeout: 15 * time.Second}
	return d.DialContext(ctx, "tcp", addr)
}

func deviceName(override string) string {
	if override != "" {
		return override
	}
	if h, err := os.Hostname(); err == nil && h != "" {
		return h
	}
	return "oshare-go"
}

func isTerminal() bool {
	st, err := os.Stdin.Stat()
	if err != nil {
		return false
	}
	return st.Mode()&os.ModeCharDevice != 0
}
