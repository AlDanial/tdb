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
