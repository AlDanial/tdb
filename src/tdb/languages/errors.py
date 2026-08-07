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


def parse_python_error(stderr: str) -> ParsedError | None:
    """Parse a Python traceback out of raw stderr text.

    Bails (returns None) unless the standard traceback header is
    present. Chained tracebacks ("The above exception was the direct
    cause..." / "During handling of the above exception...") are
    split into blocks; the LAST block is used, since that is the
    exception that actually terminated the process (Python prints
    cause/context first, final exception last).
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

    return ParsedError(
        header=tb_header,
        message=exception_text,
        frames=frames,
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

# Known non-fatal perl warning openers. Only consulted for a "lone" first
# line (nothing else follows) -- see the module-level note in
# parse_perl_error for why that case is ambiguous.
_PERL_WARNING_PREFIXES = (
    "Use of uninitialized value",
    "Use of each",
    'Argument "',
    "Possible unintended interpolation",
    "Odd number of elements",
)


def parse_perl_error(stderr: str) -> ParsedError | None:
    """Parse a fatal perl die/error out of raw stderr text.

    A lone `... at FILE line N.` line is structurally identical whether
    perl is reporting a fatal die or a non-fatal warning (e.g. "Use of
    uninitialized value ... at x.pl line 10."), so a bare regex match on
    the first line is not enough to call it fatal. This function treats
    stderr as fatal when it finds a "died"-shaped terminator:

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
            continue
        if _PERL_TERMINATOR_RE.match(ln):
            has_terminator = True

    if non_empty_rest:
        fatal = has_terminator or bool(call_frames)
    else:
        fatal = not message.startswith(_PERL_WARNING_PREFIXES)

    if not fatal:
        return None

    frames = list(reversed(call_frames)) + [
        ErrorFrame(path=inner_path, line=inner_line, func="")
    ]

    return ParsedError(header="Perl error:", message=message, frames=frames)
