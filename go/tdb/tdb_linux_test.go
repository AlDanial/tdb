//go:build linux

package tdb

import (
	"strings"
	"testing"
)

// `go test` runs without a terminal, so Breakpoint must return instead of
// trapping (an untraced runtime.Breakpoint would kill the test binary
// with SIGTRAP) and must not try to spawn anything.
func TestBreakpointIsNoopWithoutTerminal(t *testing.T) {
	t.Setenv("TDB", "/nonexistent/tdb")
	Breakpoint()
}

func TestParseTracerPid(t *testing.T) {
	status := "Name:\tprog\nTracerPid:\t4321\nUid:\t1000\n"
	if got := parseTracerPid(strings.NewReader(status)); got != 4321 {
		t.Fatalf("parseTracerPid = %d, want 4321", got)
	}
	if got := parseTracerPid(strings.NewReader("Name:\tprog\n")); got != 0 {
		t.Fatalf("parseTracerPid without a TracerPid line = %d, want 0", got)
	}
}
