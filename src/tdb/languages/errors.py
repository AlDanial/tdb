"""Language-specific fatal-error parsers, behind Presentation.parse_error.

Each parser turns a debuggee's raw stderr text into a ParsedError (or
None if no fatal error is present), so app_handlers/dap_events.py can
build the traceback modal and synthetic stack frames without knowing
which language produced the output.
"""

from __future__ import annotations

import re

from tdb.languages.base import ErrorFrame, ParsedError

# Pre-compiled at module level so repeated traceback parses don't
# re-build the regex each time.
_TB_FILE_RE = re.compile(
    r'^\s*File "(.+)", line (\d+)(?:, in (.+))?',
    re.MULTILINE,
)


def parse_python_error(stderr: str, exit_code: int | None = None) -> ParsedError | None:
    """Parse a Python traceback out of raw stderr text.

    Bails (returns None) unless the standard traceback header is
    present. Chained tracebacks ("The above exception was the direct
    cause..." / "During handling of the above exception...") are
    split into blocks; the LAST block is used, since that is the
    exception that actually terminated the process (Python prints
    cause/context first, final exception last).

    ``exit_code`` is accepted for signature parity with
    ``Presentation.parse_error`` (and with ``parse_perl_error``, which
    DOES consult it) but is intentionally IGNORED here: the traceback
    header is already an unambiguous fatal-error signal, and a program
    can legitimately print a caught traceback (e.g. via
    ``traceback.print_exc()``) and still exit 0 -- gating on exit code
    would misclassify that as "no error".
    """
    tb_header = "Traceback (most recent call last):"
    if tb_header not in stderr:
        return None

    # Capture from the FIRST traceback header to the end, so chained
    # exceptions ("The above exception was the direct cause..." /
    # "During handling of the above exception...") are preserved in full.
    tb_start = stderr.find(tb_header)
    tb_text = stderr[tb_start:].rstrip()

    # Split into individual traceback blocks (one per chained exception).
    block_starts = [m.start() for m in re.finditer(re.escape(tb_header), tb_text)]
    blocks: list[str] = []
    for i, s in enumerate(block_starts):
        e = block_starts[i + 1] if i + 1 < len(block_starts) else len(tb_text)
        blocks.append(tb_text[s:e].rstrip())

    # Synthetic stack frames come from the LAST block — that is the
    # exception that actually terminated the process (Python prints
    # cause/context first, final exception last).
    final_block = blocks[-1] if blocks else tb_text
    matches = list(_TB_FILE_RE.finditer(final_block))

    # The exception line is the last non-empty, non-indented line of the
    # final block (Python prints it after all "File" frames).
    lines = final_block.split("\n")
    exception_text = ""
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("File ") or stripped.startswith("Traceback "):
            break
        if line.startswith("    ") or line.startswith("\t"):
            continue
        exception_text = stripped
        break

    frames = [
        ErrorFrame(path=m.group(1), line=int(m.group(2)), func=m.group(3) or "<module>")
        for m in matches
    ]

    # detail: everything after the FIRST header line, verbatim, to the
    # end of stderr -- preserves source-snippet lines and, for chained
    # exceptions, the "The above exception was the direct cause..." /
    # "During handling of the above exception..." separator sentences
    # and the repeated inner header line. This is intentionally NOT
    # rebuilt from `frames` (which only carries structured File/line/func
    # data from the LAST block) -- it is the same raw-text slice the
    # pre-refactor inline code used for the modal body.
    first_header_end = tb_text.index("\n") + 1 if "\n" in tb_text else len(tb_text)
    detail = tb_text[first_header_end:].rstrip()

    return ParsedError(
        header=tb_header,
        message=exception_text,
        frames=frames,
        detail=detail,
    )


# First line of a fatal perl die/error, e.g.
#   "Illegal division by zero at /w/has_begin.pl line 10."
# Also matches plain warnings, which have the identical trailing-location
# shape (see _PERL_WARNING_PREFIXES below for how those are told apart).
_PERL_LOC_RE = re.compile(r"^(.*) at (\S+) line (\d+)\.\s*$")

# A `\t<SUB>() called at <FILE> line <N>` call-frame line, printed by perl
# when a die/error propagates out of nested subs or a BEGIN block. No
# trailing period (unlike the message's own location line).
_PERL_FRAME_RE = re.compile(r"^\t(.+?) called at (\S+) line (\d+)\s*$")

# perl's own compile-phase terminators: printed after a BEGIN block or a
# `require`d module dies at compile time. Their presence is unambiguous
# proof the process is dying, even when frame lines are also present.
_PERL_TERMINATOR_RE = re.compile(
    r"^(BEGIN failed--compilation aborted|Compilation failed in require)\b"
)

# Known non-fatal perl warning openers. Fragile by nature (any warning
# opener not on this list is misclassified as fatal) -- only used as the
# LAST-RESORT fallback in parse_perl_error when the real exit code isn't
# available (exit_code is None: attach mode, or the `exited` DAP event
# never arrived). When exit_code IS available, fatality is decided
# directly from it instead and this list is not consulted at all.
_PERL_WARNING_PREFIXES = (
    "Use of uninitialized value",
    "Use of each",
    'Argument "',
    "Possible unintended interpolation",
    "Odd number of elements",
)


def parse_perl_error(stderr: str, exit_code: int | None = None) -> ParsedError | None:
    """Parse a fatal perl die/error out of raw stderr text.

    A lone `... at FILE line N.` line is structurally identical whether
    perl is reporting a fatal die or a non-fatal warning (e.g. "Use of
    uninitialized value ... at x.pl line 10."), so a bare regex match on
    the first line is not enough to call it fatal on its own.

    The primary fatality signal is the real process exit code: when
    ``exit_code`` is not None, stderr shaped like a perl error/warning is
    fatal exactly when ``exit_code != 0`` -- perl warnings never set a
    non-zero exit code, so this is deterministic and needs no text
    heuristics. When ``exit_code`` is None (attach mode has no owned
    child to report one for, or the `exited` DAP event hasn't arrived by
    parse time), fall back to the old shape-based heuristic:

      - the first line is the ONLY content (no frames, no scaffolding)
        and its message does not open with a known warning phrase, or
      - a `\\t<SUB> called at ...` call-frame line follows (die
        propagated out of a sub/BEGIN block), or
      - a `BEGIN failed--compilation aborted` / `Compilation failed in
        require` terminator line is present.

    Frames are built from every call-frame line (skipping perl's own
    `eval {...} called at` compile scaffolding) plus the innermost
    location from the first line, reordered to OUTERMOST-first to match
    Python's convention: perl prints call frames innermost-caller-first,
    so they are reversed, then the innermost/failing frame is appended
    last.
    """
    lines = stderr.splitlines()
    if not lines:
        return None

    loc_match = _PERL_LOC_RE.match(lines[0])
    if not loc_match:
        return None

    message = loc_match.group(1)
    inner_path = loc_match.group(2)
    inner_line = int(loc_match.group(3))

    rest = lines[1:]
    non_empty_rest = [ln for ln in rest if ln.strip()]

    call_frames: list[ErrorFrame] = []
    # Raw call-frame lines (verbatim, in the order perl printed them --
    # innermost-caller-first), for `detail`. Excludes perl's own `eval
    # {...} called at` scaffolding, same as `call_frames`.
    detail_frame_lines: list[str] = []
    has_terminator = False
    for ln in rest:
        frame_match = _PERL_FRAME_RE.match(ln)
        if frame_match:
            func_raw = frame_match.group(1)
            if func_raw == "eval {...}" or func_raw.startswith("eval "):
                continue
            func = func_raw[:-2] if func_raw.endswith("()") else func_raw
            call_frames.append(
                ErrorFrame(
                    path=frame_match.group(2),
                    line=int(frame_match.group(3)),
                    func=func,
                )
            )
            detail_frame_lines.append(ln)
            continue
        if _PERL_TERMINATOR_RE.match(ln):
            has_terminator = True

    if exit_code is not None:
        # Deterministic: perl warnings never produce a non-zero exit, so
        # the real exit code alone settles fatality -- no text heuristics
        # needed, and none of the fragile-prefix false negatives from the
        # old denylist are possible. Note the `loc_match` check above still
        # gates everything: a nonzero exit with stderr that doesn't even
        # look like a perl error/warning line correctly returns None here
        # rather than fabricating a message from unrelated text.
        fatal = exit_code != 0
    elif non_empty_rest:
        fatal = has_terminator or bool(call_frames)
    else:
        # exit_code is None: last-resort fallback (see _PERL_WARNING_PREFIXES
        # comment above).
        fatal = not message.startswith(_PERL_WARNING_PREFIXES)

    if not fatal:
        return None

    frames = list(reversed(call_frames)) + [
        ErrorFrame(path=inner_path, line=inner_line, func="")
    ]

    # detail: the die message plus its raw `\t... called at ...` frame
    # lines (verbatim, scaffolding excluded), for the modal body.
    detail = "\n".join([lines[0], *detail_frame_lines])

    return ParsedError(
        header="Perl error:", message=message, frames=frames, detail=detail
    )


# First line of a fatal ruby exception (pipe/non-tty output is always
# bottom-up), e.g.
#   /w/boom.rb:2:in `inner': divided by 0 (ZeroDivisionError)
# Ruby <= 3.3 quotes the method as `inner'; >= 3.4 as 'Object#inner'.
_RUBY_HEAD_RE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):in [`'](?P<func>[^`']+)'"
    r": (?P<msg>.+) \((?P<cls>[A-Z]\w*(?:::\w+)*)\)\s*$"
)

# A "\tfrom FILE:LINE:in `func'" backtrace frame (innermost-caller-first).
_RUBY_FRAME_RE = re.compile(
    r"^\s*from (?P<path>.+?):(?P<line>\d+):in [`'](?P<func>[^`']+)'\s*$"
)

# Syntax errors have no exception-style head line:
#   /w/bad.rb:3: syntax error, unexpected end-of-input        (<= 3.3)
#   /w/bad.rb:2: syntax error found (SyntaxError)             (>= 3.4)
_RUBY_SYNTAX_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+): (?P<msg>syntax error.*)$")


def _ruby_func(name: str) -> str:
    # "" lets Presentation.frame_placeholder ("<main>") label the frame.
    return "" if name == "<main>" else name


def parse_ruby_error(stderr: str, exit_code: int | None = None) -> ParsedError | None:
    """Parse a fatal Ruby exception or syntax error out of raw stderr.

    ``exit_code`` is accepted for signature parity with the other
    parsers but ignored: the ``FILE:LINE:in `meth': msg (Class)`` head
    line is an unambiguous fatal-error signal on its own (Ruby prints
    it only for exceptions that terminate the process; rescued
    exceptions produce no such stderr line unless the program prints
    one itself, which is the same accepted ambiguity Python's parser
    has with `traceback.print_exc()`).
    """
    lines = stderr.splitlines()
    head = None
    head_idx = 0
    for i, ln in enumerate(lines):
        m = _RUBY_HEAD_RE.match(ln)
        if m:
            head, head_idx = m, i
            break
    if head is None:
        for i, ln in enumerate(lines):
            m = _RUBY_SYNTAX_RE.match(ln)
            if m:
                return ParsedError(
                    header="Ruby error:",
                    message=m.group("msg"),
                    frames=[
                        ErrorFrame(
                            path=m.group("path"),
                            line=int(m.group("line")),
                            func="",
                        )
                    ],
                    # keep the caret/source context lines that follow
                    detail="\n".join(lines[i:]).rstrip(),
                )
        return None

    call_frames: list[ErrorFrame] = []
    detail_lines = [lines[head_idx]]
    for ln in lines[head_idx + 1 :]:
        fm = _RUBY_FRAME_RE.match(ln)
        if not fm:
            break  # e.g. a "... N levels..." truncation marker ends frames
        call_frames.append(
            ErrorFrame(
                path=fm.group("path"),
                line=int(fm.group("line")),
                func=_ruby_func(fm.group("func")),
            )
        )
        detail_lines.append(ln)

    # Ruby prints innermost-first; ParsedError wants OUTERMOST-first
    # with the failing frame last (same reordering as perl's parser).
    frames = list(reversed(call_frames)) + [
        ErrorFrame(
            path=head.group("path"),
            line=int(head.group("line")),
            func=_ruby_func(head.group("func")),
        )
    ]
    return ParsedError(
        header="Ruby error:",
        message=f"{head.group('msg')} ({head.group('cls')})",
        frames=frames,
        detail="\n".join(detail_lines),
    )
