// Standalone fixture for the -a/--attach (local pid attach) integration
// test only (tests/integration/test_go_session.py). Deliberately NOT
// go_blocked/main.go: that fixture is launched by dlv itself (single-file
// "go build <file>.go" mode, per DelveAdapter.launch_body), so it must stay
// buildable with exactly the stdlib imports it already has, on every
// platform the other (non-attach) Go tests might run on. This fixture is
// instead built into a real binary with plain `go build` and started as an
// ordinary subprocess *before* tdb ever touches it, then attached to by
// pid -- so it needs one thing go_blocked doesn't: permission for a
// non-ancestor process (dlv, spawned as a sibling of this binary, not a
// parent) to ptrace-attach, which most Linux distros restrict by default
// (Yama `ptrace_scope=1`). PR_SET_PTRACER_ANY self-grants that. It's a raw
// Linux syscall, so this fixture (and the test that builds it) is
// Linux-only -- matching the already-documented Linux-only scope of -a
// itself (see README).
package main

import (
	"fmt"
	"sync"
	"syscall"
	"time"
)

var mu sync.Mutex

func recvWorker(id int, ch chan int) {
	v := <-ch // parks: nothing ever sends
	fmt.Println(id, v)
}

func lockWorker() {
	mu.Lock() // parks: main holds mu
	defer mu.Unlock()
}

func main() {
	// PR_SET_PTRACER (0x59616d61, "Yama"), PR_SET_PTRACER_ANY (-1): let
	// any process attach, not just a direct parent.
	syscall.Syscall(syscall.SYS_PRCTL, 0x59616d61, ^uintptr(0), 0)

	ch := make(chan int)
	mu.Lock()
	for i := 0; i < 3; i++ {
		go recvWorker(i, ch)
	}
	go lockWorker()
	time.Sleep(200 * time.Millisecond) // let workers park
	marker := 42
	fmt.Println("marker =", marker)
	time.Sleep(10 * time.Second) // window for inspection; test kills earlier
}
