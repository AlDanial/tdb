// Package tdb lets a running Go program open the tdb debugger on itself,
// the Go counterpart of Python's `import tdb; tdb.breakpoint()`.
//
//	import "github.com/AlDanial/tdb/go/tdb"
//
//	func compute(n int) int {
//		total := 0
//		for i := 0; i < n; i++ {
//			total += i
//		}
//		tdb.Breakpoint() // tdb opens here, paused on the next line
//		return total
//	}
//
// Run the program directly, not under tdb. When Breakpoint is reached it
// spawns `tdb --lang go -a <pid> --no-pause-on-attach` on the program's
// terminal (the `tdb` on PATH, or the one named by $TDB), waits for tdb's
// Delve to attach, and traps with runtime.Breakpoint; tdb steps out of the
// trap so the stop lands on the line after the call. Later calls reuse the
// running tdb. Quitting tdb (Ctrl+q) detaches and the program runs on; the
// next Breakpoint spawns a fresh tdb.
//
// Breakpoint is a no-op when stdin and stdout are not a terminal, so a
// program can keep its Breakpoint calls when run from a pipe or a
// service. It also gives up, with a warning on stderr, when tdb cannot be
// found or exits before attaching. Build with `-gcflags=all=-N -l` to keep
// local variables inspectable.
//
// Linux only: attaching needs ptrace, and the hook grants it to tdb's
// Delve with PR_SET_PTRACER (Yama ptrace_scope=1). On other systems
// Breakpoint warns once and returns.
package tdb

import (
	"fmt"
	"os"
	"runtime"
	"sync"
)

var (
	mu       sync.Mutex
	warnedOS bool
)

// Breakpoint stops the program in tdb on the line following the call. See
// the package documentation for what it does when no tdb is attached yet.
func Breakpoint() {
	// Stay on one OS thread from the "is this thread traced?" check to the
	// trap itself: Delve attaches and detaches thread by thread, and an
	// INT3 on an untraced thread would kill the program with SIGTRAP.
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	mu.Lock()
	ok := ensureDebugger()
	mu.Unlock()
	if !ok {
		return
	}
	// Must be called directly from Breakpoint: tdb recognizes the stop by
	// this function's name on top of the stack (dlv hides the
	// runtime.Breakpoint frame) and steps out of it.
	runtime.Breakpoint()
}

func warnf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "tdb.Breakpoint: "+format+"\n", args...)
}
