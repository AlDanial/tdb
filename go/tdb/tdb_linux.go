//go:build linux

package tdb

import (
	"bufio"
	"errors"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const (
	// How long to wait for a freshly spawned tdb's Delve to attach.
	attachTimeout = 60 * time.Second
	// After tdb detaches (Ctrl+q) its TUI takes a moment to exit; wait
	// this long for it before starting the next tdb on the same terminal.
	lingerTimeout = 3 * time.Second
	pollInterval  = 50 * time.Millisecond

	prSetPtracer    = 0x59616d61 // PR_SET_PTRACER ("Yama")
	prSetPtracerAny = ^uintptr(0)
)

var (
	tdbDone       chan struct{} // closed when the last tdb we spawned exits
	tracerAllowed bool
)

// ensureDebugger returns true when a debugger is ptrace-attached and it is
// safe to trap. Caller holds mu.
func ensureDebugger() bool {
	if !isTerminal(os.Stdin) || !isTerminal(os.Stdout) {
		return false
	}
	if !tracerAllowed {
		// Let a non-ancestor process (tdb's dlv) attach under Yama.
		syscall.Syscall(syscall.SYS_PRCTL, prSetPtracer, prSetPtracerAny, 0)
		tracerAllowed = true
	}
	if tracerPid() != 0 {
		// tdb (or another debugger) is attached: trapping is safe.
		return true
	}
	// Not traced. A tdb we spawned earlier may still be shutting down
	// after detaching; let it leave the terminal before spawning again.
	waitTdbExit(lingerTimeout)
	done, err := spawnTdb()
	if err != nil {
		warnf("%v; continuing without a debugger", err)
		return false
	}
	deadline := time.Now().Add(attachTimeout)
	for tracerPid() == 0 {
		select {
		case <-done:
			warnf("tdb exited before attaching; continuing")
			return false
		default:
		}
		if time.Now().After(deadline) {
			warnf("tdb did not attach within %v; continuing", attachTimeout)
			return false
		}
		time.Sleep(pollInterval)
	}
	return true
}

// spawnTdb starts tdb on this program's terminal, attaching to this pid,
// and returns a channel closed when it exits.
func spawnTdb() (chan struct{}, error) {
	name := os.Getenv("TDB")
	if name == "" {
		name = "tdb"
	}
	exe, err := exec.LookPath(name)
	if err != nil {
		return nil, errors.New("cannot find tdb (" + err.Error() +
			"); install it or point $TDB at it")
	}
	cmd := exec.Command(exe, "--lang", "go", "-a", strconv.Itoa(os.Getpid()),
		"--no-pause-on-attach")
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	done := make(chan struct{})
	go func() {
		cmd.Wait()
		close(done)
	}()
	tdbDone = done
	return done, nil
}

func waitTdbExit(d time.Duration) {
	if tdbDone == nil {
		return
	}
	select {
	case <-tdbDone:
	case <-time.After(d):
	}
}

// tracerPid is the pid of the process ptrace-attached to the calling OS
// thread, 0 if none. Per thread, not per process: /proc/self/status
// describes the main thread, which may still be traced (or already
// detached) while this one is not.
func tracerPid() int {
	f, err := os.Open("/proc/self/task/" + strconv.Itoa(syscall.Gettid()) + "/status")
	if err != nil {
		return 0
	}
	defer f.Close()
	return parseTracerPid(f)
}

func parseTracerPid(r io.Reader) int {
	sc := bufio.NewScanner(r)
	for sc.Scan() {
		if v, ok := strings.CutPrefix(sc.Text(), "TracerPid:"); ok {
			pid, err := strconv.Atoi(strings.TrimSpace(v))
			if err != nil {
				return 0
			}
			return pid
		}
	}
	return 0
}

func isTerminal(f *os.File) bool {
	var t syscall.Termios
	_, _, errno := syscall.Syscall(syscall.SYS_IOCTL, f.Fd(),
		syscall.TCGETS, uintptr(unsafe.Pointer(&t)))
	return errno == 0
}
