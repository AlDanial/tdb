//! Live breakpoint hook for the [tdb](https://github.com/AlDanial/tdb)
//! debugger, the Rust counterpart of Python's `tdb.breakpoint()`.
//!
//! ```no_run
//! fn compute(n: u64) -> u64 {
//!     let total: u64 = (0..n).sum();
//!     tdb::breakpoint(); // tdb opens here, paused on the next line
//!     total
//! }
//! ```
//!
//! Run the program directly, not under tdb. When `breakpoint` is reached
//! it starts `tdb --lang rust -a <pid> --no-pause-on-attach` on the
//! program's terminal (the `tdb` on `PATH`, or the one named by `$TDB`),
//! waits for tdb's gdb or lldb to attach, and stops in
//! `tdb_breakpoint_stop`; tdb steps out to the line after the call. Later
//! calls reuse the running tdb. Quitting tdb (Ctrl+q) detaches and the
//! program runs on; the next call opens a fresh tdb.
//!
//! `breakpoint` is a no-op when stdin or stdout is not a terminal, and it
//! warns on stderr and continues when tdb cannot be found, exits before
//! attaching, or does not attach within 60 seconds. Build a debug profile
//! (`opt-level = 0`, `debug = true`) so locals stay inspectable.
//!
//! Linux only: attaching needs ptrace, granted to tdb's debugger with
//! `PR_SET_PTRACER` (Yama `ptrace_scope=1`). Elsewhere `breakpoint`
//! warns once and returns. The crate has no dependencies.

/// The function tdb's hidden breakpoint lands in. Shared, by name, with
/// the C/C++ header and the OCaml library; never inlined, never mangled.
#[no_mangle]
#[inline(never)]
pub extern "C" fn tdb_breakpoint_stop() {
    std::hint::black_box(());
}

/// Stop the program in tdb on the line following the call. See the crate
/// documentation for what happens when no tdb is attached yet.
#[inline(never)]
pub fn breakpoint() {
    if platform::ensure_debugger() {
        tdb_breakpoint_stop();
    }
}

fn warn(msg: &str) {
    eprintln!("tdb::breakpoint: {msg}; continuing without a debugger");
}

#[cfg(target_os = "linux")]
mod platform {
    use super::warn;
    use std::ffi::{c_int, c_ulong};
    use std::io::IsTerminal;
    use std::process::{Child, Command};
    use std::sync::Mutex;
    use std::time::{Duration, Instant};

    const ATTACH_TIMEOUT: Duration = Duration::from_secs(60);
    const LINGER_TIMEOUT: Duration = Duration::from_secs(3);
    const POLL: Duration = Duration::from_millis(50);
    const PR_SET_PTRACER: c_int = 0x5961_6d61; // "Yama"
    const PR_SET_PTRACER_ANY: c_ulong = c_ulong::MAX;

    extern "C" {
        // libc is always linked on Linux; declaring the one prototype we
        // need keeps the crate dependency-free.
        fn prctl(option: c_int, ...) -> c_int;
    }

    #[derive(Default)]
    struct State {
        child: Option<Child>,
        tracer_allowed: bool,
    }

    static STATE: Mutex<State> = Mutex::new(State { child: None, tracer_allowed: false });

    /// Pid of the tracer attached to this thread (gdb/lldb attach thread by
    /// thread), 0 if none.
    fn tracer_pid() -> u32 {
        let Ok(status) = std::fs::read_to_string("/proc/thread-self/status") else {
            return 0;
        };
        status
            .lines()
            .find_map(|l| l.strip_prefix("TracerPid:"))
            .and_then(|v| v.trim().parse().ok())
            .unwrap_or(0)
    }

    fn child_exited(state: &mut State) -> bool {
        match state.child.as_mut() {
            None => true,
            Some(c) => match c.try_wait() {
                Ok(Some(_)) | Err(_) => {
                    state.child = None;
                    true
                }
                Ok(None) => false,
            },
        }
    }

    fn wait_child_exit(state: &mut State, timeout: Duration) {
        let deadline = Instant::now() + timeout;
        while !child_exited(state) && Instant::now() < deadline {
            std::thread::sleep(POLL);
        }
    }

    fn spawn(state: &mut State) -> bool {
        let name = std::env::var_os("TDB")
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| "tdb".into());
        match Command::new(&name)
            .args(["--lang", "rust", "-a"])
            .arg(std::process::id().to_string())
            .arg("--no-pause-on-attach")
            .spawn()
        {
            Ok(child) => {
                state.child = Some(child);
                true
            }
            Err(e) => {
                warn(&format!(
                    "cannot start {} ({e}); install tdb or point $TDB at it",
                    name.to_string_lossy()
                ));
                false
            }
        }
    }

    pub(super) fn ensure_debugger() -> bool {
        if !std::io::stdin().is_terminal() || !std::io::stdout().is_terminal() {
            return false;
        }
        let mut state = STATE.lock().unwrap_or_else(|p| p.into_inner());
        if !state.tracer_allowed {
            // Let a non-ancestor (tdb's gdb/lldb-dap) attach under Yama.
            // SAFETY: prctl with these constants only sets a per-process flag.
            unsafe {
                prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY, 0 as c_ulong, 0 as c_ulong, 0 as c_ulong);
            }
            state.tracer_allowed = true;
        }
        if tracer_pid() != 0 {
            return true;
        }
        wait_child_exit(&mut state, LINGER_TIMEOUT);
        if !spawn(&mut state) {
            return false;
        }
        let deadline = Instant::now() + ATTACH_TIMEOUT;
        while tracer_pid() == 0 {
            if child_exited(&mut state) {
                warn("tdb exited before attaching");
                return false;
            }
            if Instant::now() >= deadline {
                warn("tdb did not attach in time (is ptrace allowed? see /proc/sys/kernel/yama/ptrace_scope)");
                return false;
            }
            std::thread::sleep(POLL);
        }
        true
    }
}

#[cfg(not(target_os = "linux"))]
mod platform {
    use super::warn;
    use std::sync::atomic::{AtomicBool, Ordering};

    static WARNED: AtomicBool = AtomicBool::new(false);

    pub(super) fn ensure_debugger() -> bool {
        if !WARNED.swap(true, Ordering::Relaxed) {
            warn("live attach is supported on Linux only");
        }
        false
    }
}
