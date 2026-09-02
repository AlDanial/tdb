"""tests/unit/test_go_errors.py"""

from tdb.languages.base import ErrorFrame
from tdb.languages.errors import parse_go_error

PANIC = """\
some program output
panic: runtime error: integer divide by zero

goroutine 1 [running]:
main.divide(...)
\t/w/main.go:7 +0x11
main.main()
\t/w/main.go:12 +0x1d
exit status 2
"""


def test_parse_go_panic():
    err = parse_go_error(PANIC, 2)
    assert err is not None
    assert err.header == "panic: runtime error: integer divide by zero"
    assert err.message == "runtime error: integer divide by zero"
    # ParsedError.frames are OUTERMOST-first; Go prints innermost-first.
    assert [(f.func, f.path, f.line) for f in err.frames] == [
        ("main.main", "/w/main.go", 12),
        ("main.divide", "/w/main.go", 7),
    ]
    assert "goroutine 1 [running]:" in err.detail


def test_parse_only_first_goroutine_block():
    text = PANIC.replace(
        "exit status 2",
        "goroutine 18 [chan receive]:\nmain.worker()\n\t/w/main.go:3 +0x1\nexit status 2",
    )
    err = parse_go_error(text, 2)
    assert all(f.func != "main.worker" for f in err.frames)


def test_no_panic_returns_none():
    assert parse_go_error("all fine\n", 0) is None
    assert parse_go_error("", None) is None


def test_goexit_frames_skipped():
    text = PANIC.replace(
        "exit status 2",
        "runtime.goexit()\n\t/usr/local/go/src/runtime/asm_amd64.s:1650 +0x1\nexit status 2",
    )
    err = parse_go_error(text, 2)
    assert all("runtime." not in f.func for f in err.frames)


def test_pointer_receiver_methods():
    """Pointer-receiver methods like (*Server).ServeHTTP must be captured in full."""
    text = PANIC.replace(
        "main.divide(...)\n\t/w/main.go:7 +0x11\nmain.main()",
        "main.(*Server).ServeHTTP(0xc0000123, 0xc0000456)\n\t/w/srv.go:44 +0x1a\nmain.main()",
    )
    err = parse_go_error(text, 2)
    assert err is not None
    # Verify the pointer-receiver method is captured with full qualified name
    server_frame = [f for f in err.frames if "Server" in f.func]
    assert len(server_frame) == 1
    assert server_frame[0].func == "main.(*Server).ServeHTTP"
    assert server_frame[0].path == "/w/srv.go"
    assert server_frame[0].line == 44


# --- compile-failure shapes -------------------------------------------------
#
# LIVE-VERIFIED shape (see final-fix-wave-report.md): captured by connecting
# directly to a real `dlv dap` server (v1.27.1) and sending a `debug`-mode
# launch for a single-file program with a syntax error. Delve does NOT put
# this text in the failed launch response's `message` (that's just "Failed
# to launch"); it arrives as a `stderr`-category `output` event BEFORE the
# failed launch response, shaped exactly like `go build`'s own stderr with a
# `Build Error: <the go build invocation>` line prepended:
#
#   Build Error: go build -o /tmp/x/__debug_bin123 -gcflags all=-N -l /tmp/x/broken.go
#   # command-line-arguments
#   ./broken.go:6:21: syntax error: unexpected newline in argument list; possibly missing comma or ) (exit status 1)

GO_BUILD_ERROR_LIVE = """\
Build Error: go build -o /tmp/x/__debug_bin123 -gcflags all=-N -l /tmp/x/broken.go
# command-line-arguments
./broken.go:6:21: syntax error: unexpected newline in argument list; possibly missing comma or )  (exit status 1)
"""


def test_parse_go_compile_error_live_shape():
    err = parse_go_error(GO_BUILD_ERROR_LIVE, None)
    assert err is not None
    assert err.frames == [ErrorFrame(path="broken.go", line=6, func="")]
    assert "syntax error" in err.message
    assert "# command-line-arguments" in err.detail


def test_parse_go_compile_error_multi_file_package():
    # ASSUMED shape (spec-described): a package inside a module, no `Build
    # Error:` wrapper line, no column number — `file.go:line: message`.
    text = (
        "# example.com/mod/pkg\n"
        "./server.go:12: undefined: fmt.Prontln\n"
        "./server.go:20: missing return\n"
    )
    err = parse_go_error(text, None)
    assert err is not None
    # ParsedError.frames are OUTERMOST-first; the first-reported error is
    # the more useful one to land on, so (mirroring the panic branch) it
    # ends up LAST here -- `_check_stderr_traceback` reverses again when
    # building synthetic DAP frames, putting it back at index 0.
    assert [(f.path, f.line) for f in err.frames] == [
        ("server.go", 20),
        ("server.go", 12),
    ]
    assert "undefined: fmt.Prontln" in err.message


def test_compile_error_requires_package_header():
    # A bare `file.go:NN: msg` line with no `# package` header is not
    # confidently a compile failure (could be arbitrary program output) —
    # must not be misparsed.
    assert parse_go_error("./server.go:12: some unrelated log line\n", None) is None


def test_panic_takes_priority_over_compile_shape():
    # Runtime panics must never be mistaken for compile failures even if a
    # `#`-prefixed comment-looking line happens to precede them.
    text = "# not a package header, just output\n" + PANIC
    err = parse_go_error(text, 2)
    assert err is not None
    assert err.header.startswith("panic:")
