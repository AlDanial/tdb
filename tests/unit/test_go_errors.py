"""tests/unit/test_go_errors.py"""

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
