//go:build !linux

package tdb

// ensureDebugger on non-Linux systems: the hook relies on /proc and
// PR_SET_PTRACER, so it warns once and never traps. Caller holds mu.
func ensureDebugger() bool {
	if !warnedOS {
		warnedOS = true
		warnf("supported on Linux only; continuing without a debugger")
	}
	return false
}
